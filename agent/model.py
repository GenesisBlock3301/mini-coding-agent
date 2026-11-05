"""DeepSeek API adapter.

The ONLY module that knows DeepSeek exists. Takes a message list, returns your
own response type. Classifies failures as retryable / fatal / caller-bug.
Never lets a provider exception or a raw response dict escape.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_READ_TIMEOUT_S = 60.0
WRITE_TIMEOUT_S = 10.0
POOL_TIMEOUT_S = 5.0


@dataclass
class ModelResponse:
    text: str | None
    model: str
    finish_reason: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: float
    tool_calls: list[dict] = field(default_factory=list)
    # Input is billed at two rates; hit + miss == prompt_tokens. An agent loop
    # resends its whole prefix each step, so hits are where the cost model lives.
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    # Debugging escape hatch: everything the provider sent. Callers should not
    # read this - it exists so an unexpected response is still inspectable.
    raw: dict = field(default_factory=dict)


class ModelError(Exception):
    """Every failure leaving this module. No httpx exception escapes.

    kind          what went wrong, for code to branch on
    user_message  the one line a human reads
    retryable     should Session 8's backoff try again?
    safe_to_retry did the request definitely NOT reach the server?
    """

    def __init__(
        self,
        kind: str,
        user_message: str,
        *,
        retryable: bool,
        safe_to_retry: bool,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message
        self.retryable = retryable
        self.safe_to_retry = safe_to_retry
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class ModelConfig:
    """Where to send a request and how long to wait, resolved once.

    Grouped into one object so call_model keeps a short signature and so a
    test (or Session 13's concurrent runs) can point at a different endpoint
    without threading five keyword arguments through every layer.
    """

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S
    read_timeout: float = DEFAULT_READ_TIMEOUT_S

    @classmethod
    def from_env(cls) -> ModelConfig:
        """Read .env / the environment. Raises ModelError when the key is missing."""
        api_key = os.getenv("DEEPSEEK_APIKEY")
        if not api_key:
            raise ModelError(
                "auth",
                "No API key. Set DEEPSEEK_APIKEY in .env.",
                retryable=False,
                safe_to_retry=True,
            )
        return cls(
            api_key=api_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL,
            model=os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL,
            connect_timeout=float(
                os.getenv("REQUEST_CONNECT_TIMEOUT_S", str(DEFAULT_CONNECT_TIMEOUT_S))
            ),
            read_timeout=float(
                os.getenv("REQUEST_READ_TIMEOUT_S", str(DEFAULT_READ_TIMEOUT_S))
            ),
        )

    @property
    def completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=WRITE_TIMEOUT_S,
            pool=POOL_TIMEOUT_S,
        )


def _provider_detail(response: httpx.Response) -> str:
    """The provider's own error text, or the raw body if it is not JSON."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if body.get("message"):
            return str(body["message"])
    return str(body)[:500]


def _classify_status(response: httpx.Response) -> ModelError:
    """Map a non-200 response onto one of your failure kinds."""
    status = response.status_code
    detail = _provider_detail(response)

    if status == 401:
        return ModelError(
            "auth",
            "Key rejected. Check DEEPSEEK_APIKEY in .env.",
            retryable=False,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
        )
    if status == 402:
        return ModelError(
            "billing",
            "DeepSeek account is out of balance. Top up to continue.",
            retryable=False,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
        )
    if status in (400, 422):
        return ModelError(
            "bad_request",
            f"Your request was malformed (HTTP {status}): {detail}",
            retryable=False,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
        )
    if status == 429:
        return ModelError(
            "rate_limit",
            "Rate limited by DeepSeek. Back off and retry.",
            retryable=True,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
        )
    if status >= 500:
        return ModelError(
            "server",
            f"DeepSeek server error (HTTP {status}). Retry.",
            retryable=True,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
        )
    # A code you have not met yet (403 tomorrow). Degrade by range, never crash.
    return ModelError(
        "unexpected_status",
        f"Unexpected HTTP {status} from DeepSeek: {detail}",
        retryable=False,
        safe_to_retry=True,
        status_code=status,
        detail=detail,
    )


def _build_payload(
    messages: list[dict[str, Any]],
    *,
    model: str,
    tools: list[dict[str, Any]] | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """The request body. Omits optional keys entirely rather than sending null."""
    payload: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        # Passed through verbatim. This module knows nothing about list_files
        # or read_file and must not learn: it serializes whatever it is given,
        # which is what keeps the adapter swappable in Session 14.
        payload["tools"] = tools
    return payload


def _post(config: ModelConfig, payload: dict[str, Any]) -> tuple[httpx.Response, float]:
    """POST once. Returns (response, duration_ms). Translates every httpx error.

    safe_to_retry is the distinction that matters in Session 6: it answers
    "did this request definitely NOT reach the server", which is what makes a
    retry free rather than a second billing.
    """
    started = time.monotonic()  # monotonic: wall-clock jumps must not show up as latency
    try:
        response = httpx.post(
            config.completions_url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload,
            timeout=config.timeout,
        )
    except httpx.ConnectTimeout as exc:
        raise ModelError(
            "timeout",
            f"Could not reach DeepSeek within {config.connect_timeout}s. Check your network.",
            retryable=True,
            safe_to_retry=True,  # nothing reached the server
            detail=repr(exc),
        ) from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
        raise ModelError(
            "timeout",
            f"DeepSeek did not respond within {config.read_timeout}s. "
            "The request may still have been processed and billed.",
            retryable=True,
            safe_to_retry=False,
            detail=repr(exc),
        ) from exc
    except httpx.ConnectError as exc:
        raise ModelError(
            "network",
            f"Cannot connect to {config.base_url}. Is DEEPSEEK_BASE_URL correct?",
            retryable=False,
            safe_to_retry=True,
            detail=repr(exc),
        ) from exc
    except httpx.TransportError as exc:
        raise ModelError(
            "network",
            f"Transport failure talking to DeepSeek: {exc!r}",
            retryable=True,
            safe_to_retry=False,
            detail=repr(exc),
        ) from exc
    return response, (time.monotonic() - started) * 1000


def _decode_json(response: httpx.Response) -> dict[str, Any]:
    """A 200 is not a promise of JSON - a captive portal returns HTML with 200."""
    try:
        return response.json()
    except ValueError as exc:
        raise ModelError(
            "bad_response",
            "DeepSeek returned a 200 that was not JSON (proxy or captive portal?).",
            retryable=True,
            safe_to_retry=True,
            detail=response.text[:500],
        ) from exc


def _build_response(
    data: dict[str, Any], *, duration_ms: float, requested_model: str
) -> ModelResponse:
    """Map the provider's shape onto yours. Every field is defaulted."""
    choices = data.get("choices") or []
    if not choices:
        raise ModelError(
            "bad_response",
            "DeepSeek returned 200 with no choices.",
            retryable=True,
            safe_to_retry=True,
            detail=str(data)[:500],
        )

    choice = choices[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}

    return ModelResponse(
        text=message.get("content"),                 # may legitimately be None
        model=data.get("model", requested_model),    # the model that ANSWERED
        finish_reason=choice.get("finish_reason") or "unknown",
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        duration_ms=duration_ms,
        tool_calls=message.get("tool_calls") or [],  # absent -> [], not None
        prompt_cache_hit_tokens=usage.get("prompt_cache_hit_tokens", 0),
        prompt_cache_miss_tokens=usage.get("prompt_cache_miss_tokens", 0),
        raw=data,
    )


def call_model(
    messages: list[dict[str, Any]],
    *,
    config: ModelConfig | None = None,
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int | None = None,
) -> ModelResponse:
    """Send one chat-completions request and return a ModelResponse.

    config defaults to ModelConfig.from_env(). Raises ModelError for every
    failure - never httpx, never a raw provider dict.
    """
    config = config or ModelConfig.from_env()
    payload = _build_payload(messages, model=config.model, tools=tools, max_tokens=max_tokens)

    response, duration_ms = _post(config, payload)
    if response.status_code != 200:
        raise _classify_status(response)

    return _build_response(
        _decode_json(response), duration_ms=duration_ms, requested_model=config.model
    )
