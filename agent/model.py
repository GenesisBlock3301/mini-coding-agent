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


def call_model(
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    max_tokens: int | None = None,
    connect_timeout: float | None = None,
    read_timeout: float | None = None,
) -> ModelResponse:
    """Send one chat-completions request and return a ModelResponse.

    Raises ModelError for every failure. Never raises httpx.
    """
    api_key = api_key or os.getenv("DEEPSEEK_APIKEY")
    if not api_key:
        raise ModelError(
            "auth",
            "No API key. Set DEEPSEEK_APIKEY in .env.",
            retryable=False,
            safe_to_retry=True,
        )

    base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = model or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL
    connect_timeout = connect_timeout or float(os.getenv("REQUEST_CONNECT_TIMEOUT_S", "5"))
    read_timeout = read_timeout or float(os.getenv("REQUEST_READ_TIMEOUT_S", "60"))

    payload: dict[str, Any] = {"model": model, "messages": messages}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    timeout = httpx.Timeout(
        connect=connect_timeout, read=read_timeout, write=10.0, pool=5.0
    )

    # Measured around the HTTP call only, on a monotonic clock.
    started = time.monotonic()
    try:
        response = httpx.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=timeout,
        )
    except httpx.ConnectTimeout as exc:
        raise ModelError(
            "timeout",
            f"Could not reach DeepSeek within {connect_timeout}s. Check your network.",
            retryable=True,
            safe_to_retry=True,  # nothing reached the server
            detail=repr(exc),
        ) from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
        raise ModelError(
            "timeout",
            f"DeepSeek did not respond within {read_timeout}s. "
            "The request may still have been processed and billed.",
            retryable=True,
            safe_to_retry=False,  # <- the distinction that matters in Session 6
            detail=repr(exc),
        ) from exc
    except httpx.ConnectError as exc:
        raise ModelError(
            "network",
            f"Cannot connect to {base_url}. Is DEEPSEEK_BASE_URL correct?",
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
    duration_ms = (time.monotonic() - started) * 1000

    if response.status_code != 200:
        raise _classify_status(response)

    try:
        data = response.json()
    except ValueError as exc:
        raise ModelError(
            "bad_response",
            "DeepSeek returned a 200 that was not JSON (proxy or captive portal?).",
            retryable=True,
            safe_to_retry=True,
            detail=response.text[:500],
        ) from exc

    choices = data.get("choices") or []
    if not choices:
        raise ModelError(
            "bad_response",
            "DeepSeek returned 200 with no choices.",
            retryable=True,
            safe_to_retry=True,
            detail=str(data)[:500],
        )

    message = choices[0].get("message") or {}
    usage = data.get("usage") or {}

    return ModelResponse(
        text=message.get("content"),            # may legitimately be None
        model=data.get("model", model),         # the model that ANSWERED
        finish_reason=choices[0].get("finish_reason") or "unknown",
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        duration_ms=duration_ms,
        tool_calls=message.get("tool_calls") or [],  # absent -> [], not None
        prompt_cache_hit_tokens=usage.get("prompt_cache_hit_tokens", 0),
        prompt_cache_miss_tokens=usage.get("prompt_cache_miss_tokens", 0),
        raw=data,
    )
