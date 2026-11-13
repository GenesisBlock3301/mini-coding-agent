"""Tests for agent.budget.

Arithmetic and one clock. No API key, no network, no loop - if a limit is
wrong here, every test in test_loop.py is testing the wrong rule.
"""

import pytest

from agent.budget import (
    STOP_SPEND_LIMIT,
    STOP_STEP_LIMIT,
    STOP_TIME_LIMIT,
    STOP_TOKEN_LIMIT,
    STOP_TOOL_LIMIT,
    Budget,
    Ledger,
    Pricing,
    check_before_step,
    check_before_tool_call,
    refusal_message,
)

PRICES = Pricing(
    input_cache_hit_per_m=0.07,
    input_cache_miss_per_m=0.27,
    output_per_m=1.10,
)


# --------------------------------------------------------------------------
# Step 8: pricing. 0.0 and "I don't know" are different numbers.
# --------------------------------------------------------------------------


def test_unset_prices_are_none_not_zero(monkeypatch):
    for name in (
        "PRICE_INPUT_CACHE_HIT_PER_M",
        "PRICE_INPUT_CACHE_MISS_PER_M",
        "PRICE_OUTPUT_PER_M",
    ):
        monkeypatch.delenv(name, raising=False)

    assert Pricing.from_env() is None


def test_placeholder_zero_prices_count_as_unconfigured(monkeypatch):
    """.env.example ships 0.00. A run priced at 0.00 is not free, it is unpriced."""
    monkeypatch.setenv("PRICE_INPUT_CACHE_HIT_PER_M", "0.00")
    monkeypatch.setenv("PRICE_INPUT_CACHE_MISS_PER_M", "0.00")
    monkeypatch.setenv("PRICE_OUTPUT_PER_M", "0.00")

    assert Pricing.from_env() is None


def test_cache_hit_and_miss_are_priced_separately():
    cost = PRICES.cost(cache_hit=1_000_000, cache_miss=1_000_000, output=1_000_000)

    assert cost == pytest.approx(0.07 + 0.27 + 1.10)


def test_the_reserve_is_priced_at_the_dearest_rate():
    """A reserve priced at the cheap rate under-reserves exactly when the next
    response is long - the one case it exists for."""
    assert PRICES.worst_per_token == pytest.approx(1.10 / 1_000_000)


def test_a_provider_that_reports_no_cache_split_is_billed_as_all_miss():
    ledger = Ledger()
    ledger.record_usage(prompt_tokens=1000, completion_tokens=0)

    assert ledger.cache_miss_tokens == 1000
    assert ledger.cache_hit_tokens == 0
    assert ledger.spend(PRICES) == pytest.approx(1000 * 0.27 / 1_000_000)


def test_spend_is_none_when_unpriced():
    ledger = Ledger()
    ledger.record_usage(prompt_tokens=10_000, completion_tokens=10_000)

    assert ledger.spend(None) is None


# --------------------------------------------------------------------------
# Step 6: the gate, and what it reports when two limits blow at once.
# --------------------------------------------------------------------------


def test_a_fresh_run_may_proceed():
    assert check_before_step(Budget(), Ledger()) is None


def test_step_limit_fires_at_exactly_max_steps():
    budget = Budget(max_steps=3)
    ledger = Ledger(steps=2)
    assert check_before_step(budget, ledger) is None

    ledger.steps = 3
    assert check_before_step(budget, ledger) == STOP_STEP_LIMIT


def test_token_limit_fires_a_reserve_early():
    """480 spent of 500 with a 100-token reserve: the call is never made.

    Checked after the fact, the same limit is a receipt (D35).
    """
    budget = Budget(max_total_tokens=500, token_reserve=100)
    ledger = Ledger(prompt_tokens=300, completion_tokens=180)

    assert check_before_step(budget, ledger) == STOP_TOKEN_LIMIT


def test_token_limit_allows_a_call_that_fits_with_its_reserve():
    budget = Budget(max_total_tokens=500, token_reserve=100)
    ledger = Ledger(prompt_tokens=300, completion_tokens=100)

    assert check_before_step(budget, ledger) is None


def test_spend_limit_counts_the_reserve_too():
    budget = Budget(max_spend=0.001, token_reserve=1000, pricing=PRICES)
    ledger = Ledger(prompt_tokens=0, completion_tokens=0)
    ledger.record_usage(prompt_tokens=0, completion_tokens=0)
    # Spent nothing; the reserve alone is 1000 * $1.10/M = $0.0011 > $0.001.

    assert check_before_step(budget, ledger) == STOP_SPEND_LIMIT


def test_an_unpriced_spend_limit_cannot_stop_anything_so_it_is_a_usage_error():
    """D38. The gate stays silent; the caller must refuse the run instead."""
    budget = Budget(max_spend=0.01, pricing=None)

    assert budget.needs_pricing is True
    assert check_before_step(budget, Ledger()) is None


def test_steps_are_reported_before_time_when_both_are_blown():
    """Deterministic cause first: step_limit reproduces on a re-run, time_limit
    depends on how busy the machine was. Session 12 compares these reasons."""
    budget = Budget(max_steps=1, max_seconds=0.0)
    ledger = Ledger(steps=5)

    assert check_before_step(budget, ledger) == STOP_STEP_LIMIT


def test_time_limit_fires_when_steps_remain():
    budget = Budget(max_steps=5, max_seconds=0.0)

    assert check_before_step(budget, Ledger()) == STOP_TIME_LIMIT


# --------------------------------------------------------------------------
# Step 5/7: the tool-call gate. Tokens are not spent by a tool.
# --------------------------------------------------------------------------


def test_tool_gate_ignores_token_budgets():
    budget = Budget(max_total_tokens=1, token_reserve=1000, max_tool_calls=5)
    ledger = Ledger(prompt_tokens=10_000)

    assert check_before_tool_call(budget, ledger) is None


def test_tool_gate_fires_at_exactly_max_tool_calls():
    budget = Budget(max_tool_calls=2)
    ledger = Ledger(tool_calls=2)

    assert check_before_tool_call(budget, ledger) == STOP_TOOL_LIMIT


def test_refusal_text_names_the_limit_and_denies_any_side_effect():
    """It is written for a model that may read it on --resume. 'Cancelled'
    invites a retry; naming the spent budget does not."""
    text = refusal_message(STOP_TOOL_LIMIT, Budget(max_tool_calls=2))

    assert "tool-call budget (2)" in text
    assert "No file was read or changed" in text


# --------------------------------------------------------------------------
# Step 4: configuration. Flags beat .env beats defaults.
# --------------------------------------------------------------------------


def test_env_configures_every_limit(monkeypatch):
    monkeypatch.setenv("MAX_STEPS", "9")
    monkeypatch.setenv("MAX_TOOL_CALLS", "11")
    monkeypatch.setenv("MAX_RUN_SECONDS", "42")
    monkeypatch.setenv("MAX_TOTAL_TOKENS", "123456")
    monkeypatch.setenv("MAX_SPEND", "0.25")
    monkeypatch.setenv("TOKEN_RESERVE", "777")

    budget = Budget.from_env(pricing=PRICES)

    assert (budget.max_steps, budget.max_tool_calls) == (9, 11)
    assert (budget.max_seconds, budget.max_total_tokens) == (42.0, 123456)
    assert (budget.max_spend, budget.token_reserve) == (0.25, 777)


def test_describe_shows_every_active_limit():
    budget = Budget(
        max_steps=3,
        max_tool_calls=4,
        max_seconds=30,
        max_total_tokens=1000,
        max_spend=0.01,
        token_reserve=200,
        pricing=PRICES,
    )

    assert budget.describe() == (
        "steps<=3 tools<=4 30s tokens<=1000(+200 held) spend<=$0.0100"
    )
