"""The model adapter: the ONLY module that knows a provider exists.

Takes a message list, returns your own response type. Classifies failures as
retryable / fatal / caller-bug. Never lets a provider exception or a raw
response dict escape.

Two providers, one code path. DeepSeek (hosted) and Ollama (local) both speak
the OpenAI chat-completions shape - same request body, same tool_calls
structure, same finish_reason - so `Provider` below is a table of DEFAULTS,
not an abstraction over two protocols. The day a provider needs a different
request body is the day to split this file, and not before.

Select with PROVIDER=deepseek|ollama. Unset means deepseek, and every
DEEPSEEK_* variable keeps working exactly as it did.
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

DEFAULT_PROVIDER = "deepseek"

# Below this, a single read_file can evict the system prompt. Not a tuned
# number - a floor under which an agent loop is not safe to run at all.
MIN_USEFUL_CONTEXT = 32_768


@dataclass(frozen=True)
class Provider:
    """Where a model lives and what is different about talking to it.

    Both providers speak the same OpenAI-shaped wire format - the request
    body, the tool_calls structure and finish_reason are identical - so this
    is NOT an abstraction over two protocols. It is a table of defaults, and
    that is the whole reason there is one adapter here instead of two.

    `free` is load-bearing. budget.Pricing treats an all-zero price list as
    "unconfigured" on purpose (D38: 0.0 is not a price, it is "I don't know"
    wearing a number's clothes). A model running on your own machine is the
    one case where zero IS the price, known rather than unknown - so the
    caller asks the provider, not the environment.
    """

    name: str
    base_url: str
    model: str
    requires_key: bool
    read_timeout: float
    free: bool
    # Session 9, D86. How much history this provider will accept in one
    # request. A default, not a measurement: `CONTEXT_WINDOW` in .env
    # overrides it, and for Ollama the number actually SERVED can be far
    # lower than the model's - which is what context_warning() below exists
    # to shout about, because the server truncates instead of refusing.
    context_window: int


PROVIDERS = {
    "deepseek": Provider(
        name="deepseek",
        base_url=DEFAULT_BASE_URL,
        model=DEFAULT_MODEL,
        requires_key=True,
        read_timeout=DEFAULT_READ_TIMEOUT_S,
        free=False,
        context_window=65_536,
    ),
    # Ollama's OpenAI-compatible endpoint. The read timeout is not a typo:
    # a 9B model with thinking enabled took 155s for its first call on an
    # M-series laptop, against 1.2s for a hosted request. A timeout tuned for
    # a datacentre is a guaranteed failure locally.
    "ollama": Provider(
        name="ollama",
        base_url="http://localhost:11434/v1",
        model="qwen3.5:9b",
        requires_key=False,
        read_timeout=600.0,
        free=True,
        # What we TELL people to serve, not what a default `ollama serve`
        # gives you (4096). The gap between the two is the whole reason
        # context_warning exists.
        context_window=MIN_USEFUL_CONTEXT,
    ),
}


def active_provider() -> Provider:
    """The provider this process is configured for. PROVIDER in .env."""
    name = (os.getenv("PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    provider = PROVIDERS.get(name)
    if provider is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ModelError(
            "bad_request",
            f"Unknown PROVIDER {name!r}. Known providers: {known}.",
            retryable=False,
            safe_to_retry=True,
        )
    return provider


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
    retryable     should the backoff try again? A fact about the ERROR.
    safe_to_retry did the request definitely NOT reach the server? A fact
                  about the WORLD - the only flag here that can cost a file,
                  because resending a request that already arrived can apply
                  an edit twice. See agent/retry.py, D69.
    retry_after   seconds the provider asked us to wait, when it said so.
    attempts      how many times this request was actually sent. Set by the
                  loop when it gives up, so the CLI can say "after 3
                  attempts" instead of reporting the last failure as if it
                  were the only one.
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
        retry_after: float | None = None,
    ) -> None:
        super().__init__(user_message)
        self.kind = kind
        self.user_message = user_message
        self.retryable = retryable
        self.safe_to_retry = safe_to_retry
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after
        # Mutable, and the only mutable thing on this exception. The number is
        # not known where the error is built - it is known by the code that
        # decided to stop sending.
        self.attempts = 1


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
    provider: Provider = PROVIDERS[DEFAULT_PROVIDER]

    @classmethod
    def from_env(cls) -> ModelConfig:
        """Read .env / the environment for whichever PROVIDER is selected.

        Each provider reads its own `<NAME>_APIKEY`, `<NAME>_BASE_URL` and
        `<NAME>_MODEL`. DeepSeek's existing DEEPSEEK_* variables are exactly
        that scheme already, so nothing configured before this existed had to
        change - and PROVIDER unset still means DeepSeek.

        Raises ModelError only when a provider that NEEDS a key has none.
        Ollama serves anyone who asks, and demanding a key for it would be a
        rule invented here rather than one the server enforces.
        """
        provider = active_provider()
        prefix = provider.name.upper()

        api_key = os.getenv(f"{prefix}_APIKEY")
        if provider.requires_key and not api_key:
            raise ModelError(
                "auth",
                f"No API key. Set {prefix}_APIKEY in .env.",
                retryable=False,
                safe_to_retry=True,
            )

        return cls(
            # Ollama ignores the header but httpx must send a string.
            api_key=api_key or "none",
            base_url=os.getenv(f"{prefix}_BASE_URL") or provider.base_url,
            model=os.getenv(f"{prefix}_MODEL") or provider.model,
            connect_timeout=float(
                os.getenv("REQUEST_CONNECT_TIMEOUT_S", str(DEFAULT_CONNECT_TIMEOUT_S))
            ),
            read_timeout=float(
                os.getenv("REQUEST_READ_TIMEOUT_S", str(provider.read_timeout))
            ),
            provider=provider,
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


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """The Retry-After header as seconds, or None.

    Only the numeric form is read. The HTTP-date form is legal and rare, and
    parsing it means trusting the provider's clock against ours - a skewed
    date would produce a wait of hours or of zero, both worse than falling
    back to our own backoff. A header we cannot read is not an error here.
    """
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _classify_status(response: httpx.Response, provider: str = "the provider") -> ModelError:
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
            f"{provider} reports the account is out of balance. Top up to continue.",
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
            f"Rate limited by {provider}. Back off and retry.",
            retryable=True,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
            retry_after=_retry_after_seconds(response),
        )
    if status >= 500:
        return ModelError(
            "server",
            f"{provider} server error (HTTP {status}). Retry.",
            retryable=True,
            safe_to_retry=True,
            status_code=status,
            detail=detail,
            # 503 carries one as often as 429 does, and a server that names a
            # wait knows more about its own recovery than our curve does.
            retry_after=_retry_after_seconds(response),
        )
    # A code you have not met yet (403 tomorrow). Degrade by range, never crash.
    return ModelError(
        "unexpected_status",
        f"Unexpected HTTP {status} from {provider}: {detail}",
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
            f"Could not reach {config.provider.name} at {config.base_url} within "
            f"{config.connect_timeout}s. Check your network.",
            retryable=True,
            safe_to_retry=True,  # nothing reached the server
            detail=repr(exc),
        ) from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout) as exc:
        raise ModelError(
            "timeout",
            f"{config.provider.name} did not respond within {config.read_timeout}s. "
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
            f"Transport failure talking to {config.provider.name}: {exc!r}",
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
            "The provider returned a 200 that was not JSON (proxy or captive portal?).",
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
            f"{requested_model} returned 200 with no choices.",
            retryable=True,
            safe_to_retry=True,
            detail=str(data)[:500],
        )

    choice = choices[0]
    message = choice.get("message") or {}
    usage = data.get("usage") or {}

    # Two spellings of the same fact. DeepSeek splits the prompt into
    # hit/miss fields; OpenAI-compatible servers (Ollama among them) report
    # prompt_tokens_details.cached_tokens instead. Read whichever is present.
    #
    # The difference is not cosmetic: Ledger.record_usage bills a response
    # with NEITHER field at the expensive rate, deliberately. Leaving the
    # OpenAI spelling unread would make every cached token look uncached.
    prompt_tokens = usage.get("prompt_tokens", 0)
    cache_hit = usage.get("prompt_cache_hit_tokens")
    cache_miss = usage.get("prompt_cache_miss_tokens")
    if cache_hit is None and cache_miss is None:
        details = usage.get("prompt_tokens_details") or {}
        if "cached_tokens" in details:
            cache_hit = details["cached_tokens"] or 0
            cache_miss = max(prompt_tokens - cache_hit, 0)

    return ModelResponse(
        text=message.get("content"),                 # may legitimately be None
        model=data.get("model", requested_model),    # the model that ANSWERED
        finish_reason=choice.get("finish_reason") or "unknown",
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        total_tokens=usage.get("total_tokens", 0),
        duration_ms=duration_ms,
        tool_calls=message.get("tool_calls") or [],  # absent -> [], not None
        prompt_cache_hit_tokens=cache_hit or 0,
        prompt_cache_miss_tokens=cache_miss or 0,
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
        raise _classify_status(response, config.provider.name)

    return _build_response(
        _decode_json(response), duration_ms=duration_ms, requested_model=config.model
    )


def context_warning(config: ModelConfig) -> str | None:
    """Warn when a local server is serving far less context than it could.

    Ollama's default num_ctx is 4096 regardless of what the model supports -
    qwen3.5:9b advertises 262144 and gets 4096. Overflow is NOT an error
    there: the server silently drops the oldest messages. For a chat that
    degrades politely. For an agent it is corruption, because the dropped
    messages include the system prompt and, worse, an assistant turn carrying
    tool_call ids whose results are still in the history. The next request
    then fails with a provider error about unanswered tool calls - one step
    removed from the cause, and nothing in the trace points at truncation.

    Best effort by design. /api/ps only lists models currently LOADED, so a
    cold server reports nothing and this returns None. It is a tripwire, not
    a guarantee, and it never fails a run: any error means no warning.
    """
    if config.provider.name != "ollama":
        return None

    try:
        host = config.base_url.rstrip("/").removesuffix("/v1")
        response = httpx.get(f"{host}/api/ps", timeout=2.0)
        loaded = response.json().get("models") or []
    except Exception:  # noqa: BLE001 - a diagnostic must never break a run
        return None

    for entry in loaded:
        if entry.get("name") != config.model:
            continue
        served = entry.get("context_length")
        if served and served < MIN_USEFUL_CONTEXT:
            return (
                f"{config.model} is loaded with only {served} tokens of context. "
                "One read_file can exceed that, and Ollama drops the OLDEST "
                "messages silently rather than erroring - which corrupts the "
                "tool-call history. Restart the server with "
                f"OLLAMA_CONTEXT_LENGTH={MIN_USEFUL_CONTEXT} ollama serve"
            )
    return None
