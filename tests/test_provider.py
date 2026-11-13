"""Two providers, one code path.

Nothing here talks to a network. The point of these tests is that selecting a
provider changes DEFAULTS and nothing else - if a change ever makes the two
take different code paths, several of these stop making sense, which is the
signal to split the adapter.
"""

from __future__ import annotations

import pytest

from agent.budget import Ledger, Pricing
from agent.model import (
    DEFAULT_MODEL,
    MIN_USEFUL_CONTEXT,
    ModelConfig,
    ModelError,
    PROVIDERS,
    _build_response,
    active_provider,
    context_warning,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "PROVIDER", "DEEPSEEK_APIKEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
        "OLLAMA_APIKEY", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
        "REQUEST_READ_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)


# ---- selection ----------------------------------------------------------


def test_no_provider_set_means_deepseek():
    assert active_provider().name == "deepseek"


def test_an_unknown_provider_is_a_caller_bug(monkeypatch):
    monkeypatch.setenv("PROVIDER", "gpt-9")
    with pytest.raises(ModelError) as caught:
        active_provider()
    assert caught.value.kind == "bad_request"
    assert "deepseek" in str(caught.value) and "ollama" in str(caught.value)


def test_provider_name_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("PROVIDER", "  OLLAMA ")
    assert active_provider().name == "ollama"


# ---- the existing DeepSeek setup must not move --------------------------


def test_deepseek_still_reads_its_own_variables(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_APIKEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-reasoner")

    config = ModelConfig.from_env()

    assert config.api_key == "sk-test"
    assert config.model == "deepseek-reasoner"
    assert config.base_url == "https://api.deepseek.com"
    assert config.completions_url.endswith("/chat/completions")


def test_deepseek_without_a_key_is_still_an_auth_error():
    with pytest.raises(ModelError) as caught:
        ModelConfig.from_env()
    assert caught.value.kind == "auth"
    assert "DEEPSEEK_APIKEY" in str(caught.value)


def test_deepseek_defaults_are_unchanged():
    assert PROVIDERS["deepseek"].model == DEFAULT_MODEL
    assert PROVIDERS["deepseek"].requires_key is True
    assert PROVIDERS["deepseek"].free is False


# ---- ollama -------------------------------------------------------------


def test_ollama_needs_no_key(monkeypatch):
    monkeypatch.setenv("PROVIDER", "ollama")

    config = ModelConfig.from_env()

    assert config.base_url == "http://localhost:11434/v1"
    assert config.model == "qwen3.5:9b"
    assert config.completions_url == "http://localhost:11434/v1/chat/completions"
    assert config.api_key == "none"      # httpx needs a string in the header


def test_ollama_gets_a_long_read_timeout(monkeypatch):
    """A 9B model with thinking took 155s for one call. 60s would fail it."""
    monkeypatch.setenv("PROVIDER", "ollama")
    assert ModelConfig.from_env().read_timeout >= 300


def test_an_explicit_timeout_still_wins(monkeypatch):
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.setenv("REQUEST_READ_TIMEOUT_S", "30")
    assert ModelConfig.from_env().read_timeout == 30.0


def test_ollama_model_is_overridable(monkeypatch):
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3.5:4b")
    assert ModelConfig.from_env().model == "qwen3.5:4b"


# ---- free is a price, not a missing price (D38) -------------------------


def test_free_pricing_costs_zero_and_is_not_none():
    pricing = Pricing.free()
    assert pricing is not None
    assert pricing.cost(cache_hit=1000, cache_miss=5000, output=2000) == 0.0


def test_an_unset_price_list_is_still_none(monkeypatch):
    for name in ("PRICE_INPUT_CACHE_HIT_PER_M", "PRICE_INPUT_CACHE_MISS_PER_M",
                 "PRICE_OUTPUT_PER_M"):
        monkeypatch.delenv(name, raising=False)
    assert Pricing.from_env() is None


def test_a_free_run_reports_zero_rather_than_unpriced():
    ledger = Ledger()
    ledger.record_usage(prompt_tokens=5000, completion_tokens=800)
    assert ledger.spend(Pricing.free()) == 0.0
    assert ledger.spend(None) is None


# ---- usage parsing: two spellings of the cache split --------------------


def response_with(usage: dict) -> dict:
    return {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": usage,
        "model": "whatever",
    }


def test_deepseek_cache_fields_are_read():
    built = _build_response(
        response_with({
            "prompt_tokens": 500, "completion_tokens": 10, "total_tokens": 510,
            "prompt_cache_hit_tokens": 448, "prompt_cache_miss_tokens": 52,
        }),
        duration_ms=1.0, requested_model="deepseek-chat",
    )
    assert built.prompt_cache_hit_tokens == 448
    assert built.prompt_cache_miss_tokens == 52


def test_openai_style_cached_tokens_are_read():
    """Ollama's spelling. Unread, every cached token would look uncached."""
    built = _build_response(
        response_with({
            "prompt_tokens": 275, "completion_tokens": 56, "total_tokens": 331,
            "prompt_tokens_details": {"cached_tokens": 200},
        }),
        duration_ms=1.0, requested_model="qwen3.5:9b",
    )
    assert built.prompt_cache_hit_tokens == 200
    assert built.prompt_cache_miss_tokens == 75


def test_a_provider_reporting_no_split_is_billed_at_the_expensive_rate():
    """Ledger's rule, reached through a real response with no cache fields."""
    built = _build_response(
        response_with({"prompt_tokens": 100, "completion_tokens": 5}),
        duration_ms=1.0, requested_model="qwen3.5:9b",
    )
    assert built.prompt_cache_hit_tokens == 0
    assert built.prompt_cache_miss_tokens == 0

    ledger = Ledger()
    ledger.record_response(built)
    assert ledger.cache_miss_tokens == 100   # all of it, at the dearer price
    assert ledger.cache_hit_tokens == 0


# ---- the context tripwire -----------------------------------------------


def test_context_warning_is_silent_for_a_hosted_provider():
    config = ModelConfig(api_key="k", provider=PROVIDERS["deepseek"])
    assert context_warning(config) is None


def test_context_warning_never_raises_when_the_server_is_absent():
    """A diagnostic that can fail a run is worse than no diagnostic."""
    config = ModelConfig(
        api_key="none",
        base_url="http://127.0.0.1:1/v1",   # nothing listens here
        model="qwen3.5:9b",
        provider=PROVIDERS["ollama"],
    )
    assert context_warning(config) is None


def test_min_useful_context_is_above_one_truncated_read():
    """MAX_READ_BYTES is 64_000 bytes; a 4096-token window cannot hold it."""
    assert MIN_USEFUL_CONTEXT >= 32_768
