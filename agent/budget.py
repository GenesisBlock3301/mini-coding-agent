"""Budgets: the rules a run stops by, and the reading of what it spent.

Owns: the limit values (`Budget`), the running totals (`Ledger`), the price
list (`Pricing`), and the single gate that decides whether another model call
is allowed (`check_before_step`).

Three types, deliberately not one:

    Budget   the rule.    Frozen. Session 12 changes it between runs.
    Ledger   the reading. Mutable. Session 11 reports it.
    Pricing  the exchange rate between the two. Frozen, and optional.

Knows nothing about httpx, the loop, or argv. It is arithmetic plus one
clock, which is what makes every limit in this file testable without a key.

Decisions locked here (see docs/session-05.md A):
  D31 one frozen Budget object, not six keyword arguments
  D32 the tool-call cap counts the whole RUN, not one turn
  D35 the gate runs BEFORE the POST - after is a bill you already paid
  D36 a fixed token reserve, priced at the most expensive rate
  D37 prices come from env; nothing is hard-coded
  D38 a spend limit with no prices is a usage error, never "unlimited"
  D39 tokens and tool calls survive a resume; the wall clock does not
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from agent.repetition import (
    DEFAULT_MAX_REPEATED_CALLS,
    DEFAULT_MAX_REPEATED_FAILURES,
)

DEFAULT_MAX_STEPS = 5

# Why a default at all: five steps of a 60s read timeout is a five-minute run
# that nobody capped. These are deliberately loose - they are a backstop
# against a hang, not a tuning knob. The flags are the tuning knob.
DEFAULT_MAX_TOOL_CALLS = 20
DEFAULT_MAX_SECONDS = 300.0

# D36. What one more model call might cost before its usage is known. Not a
# prediction - a reserve. Overshoot is bounded by one response, and that bound
# is the entire argument for checking before the call instead of after it.
DEFAULT_TOKEN_RESERVE = 2_000

# Session 9, D86. The window the HISTORY has to fit in, which is a different
# quantity from max_total_tokens: the wallet counts every token the run has
# ever spent, the window counts the ones travelling in the next request. A
# run can be well inside its budget and still too big to send.
#
# The default is DeepSeek's, because that is the provider this was built
# against; `CONTEXT_WINDOW` in .env overrides it and the CLI passes the
# selected provider's own figure. Not a guess that silently goes stale - a
# configured default with two ways to correct it.
DEFAULT_CONTEXT_WINDOW = 65_536

# Stop reasons. Strings, not an enum: they are written to JSONL journals that
# outlive this process and are read by Session 11's evaluator, which should
# not have to import this module to understand a run.
STOP_ANSWER = "answer"
STOP_STEP_LIMIT = "step_limit"
STOP_TOOL_LIMIT = "tool_limit"
STOP_TIME_LIMIT = "time_limit"
STOP_TOKEN_LIMIT = "token_limit"
STOP_SPEND_LIMIT = "spend_limit"
# Session 9, D81 and D88. Two new reasons rather than reuse of an old one.
# `tool_limit` would have been the cheap option and it lies in a specific
# way: Session 12 compares arms by stop reason, and "the agent went in
# circles" and "the agent ran out of tool calls" are the two outcomes that
# session exists to tell apart. Both are LIMIT stops - an incomplete run with
# an exit code (D88), not an answer - because a run that stopped repeating
# itself has not finished the task, and reporting it as a normal answer is
# the "claimed success" failure Session 7 built a verifier to catch.
STOP_REPETITION = "repetition"
STOP_CONTEXT_LIMIT = "context_limit"
STOP_CANCELLED = "cancelled"

LIMIT_STOPS = (
    STOP_STEP_LIMIT,
    STOP_TOOL_LIMIT,
    STOP_TIME_LIMIT,
    STOP_TOKEN_LIMIT,
    STOP_SPEND_LIMIT,
    STOP_REPETITION,
    STOP_CONTEXT_LIMIT,
)

# The error code a tool call gets when the budget died before its turn. It is
# a real tool result carrying a real id (E1): refusing to run is an answer,
# refusing to reply is a corrupted conversation.
REFUSED_TOOL_ERROR = "budget_exhausted"


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _env_int(name: str) -> int | None:
    value = _env_float(name)
    return None if value is None else int(value)


@dataclass(frozen=True)
class Pricing:
    """USD per 1M tokens, as configured - never as guessed.

    Input is billed at two rates and an agent loop resends its whole prefix
    every step, so the cache-hit rate is where the cost model actually lives.
    Pricing a loop at one blended input rate is how a 10x error hides.

    D37: `.env.example` has reserved these three names since Session 1. A
    hard-coded price is a number that goes stale silently and is then quoted
    with confidence in a Session 12 report.
    """

    input_cache_hit_per_m: float
    input_cache_miss_per_m: float
    output_per_m: float

    @classmethod
    def from_env(cls) -> Pricing | None:
        """The configured price list, or None when there isn't one.

        All-zero counts as unconfigured. `.env.example` ships 0.00 placeholders,
        and 0.0 is not a price - it is "I don't know" wearing a number's
        clothes. The difference is load-bearing: see D38.
        """
        hit = _env_float("PRICE_INPUT_CACHE_HIT_PER_M") or 0.0
        miss = _env_float("PRICE_INPUT_CACHE_MISS_PER_M") or 0.0
        out = _env_float("PRICE_OUTPUT_PER_M") or 0.0
        if hit == 0.0 and miss == 0.0 and out == 0.0:
            return None
        return cls(
            input_cache_hit_per_m=hit,
            input_cache_miss_per_m=miss,
            output_per_m=out,
        )

    @classmethod
    def free(cls) -> Pricing:
        """Zero, meaning KNOWN to be zero - a model running on your own machine.

        `from_env` reads an all-zero price list as "unconfigured" (D38: 0.0 is
        not a price, it is "I don't know" wearing a number's clothes). That
        rule is right for a hosted provider and wrong for a local one, where
        zero is the actual price. The difference is which question was asked:
        from_env asks the environment and may get silence; this is a caller
        stating a fact about the provider it chose.
        """
        return cls(
            input_cache_hit_per_m=0.0,
            input_cache_miss_per_m=0.0,
            output_per_m=0.0,
        )

    def cost(self, *, cache_hit: int, cache_miss: int, output: int) -> float:
        return (
            cache_hit * self.input_cache_hit_per_m
            + cache_miss * self.input_cache_miss_per_m
            + output * self.output_per_m
        ) / 1_000_000

    @property
    def worst_per_token(self) -> float:
        """The dearest rate on the list. The reserve is priced at this.

        A reserve priced at the cheapest rate is a reserve that under-reserves
        exactly when the next response is long - which is the case it exists
        for.
        """
        return max(
            self.input_cache_hit_per_m,
            self.input_cache_miss_per_m,
            self.output_per_m,
        ) / 1_000_000


@dataclass(frozen=True)
class Budget:
    """Every way this run is allowed to stop, in one value.

    D31. One object rather than six keyword arguments threaded through the
    loop, because Session 11 has to record the configuration that produced a
    result and Session 13 needs a different one per concurrent run. A limit
    that lives in a function signature cannot be written to a trace.

    None means "no limit" for the optional four. max_steps is never None:
    a loop with no step limit is not an agent, it is a bill.
    """

    max_steps: int = DEFAULT_MAX_STEPS
    max_tool_calls: int | None = DEFAULT_MAX_TOOL_CALLS
    max_seconds: float | None = DEFAULT_MAX_SECONDS
    max_total_tokens: int | None = None
    max_spend: float | None = None
    token_reserve: int = DEFAULT_TOKEN_RESERVE
    pricing: Pricing | None = None
    # Session 9. The repetition rule lives HERE, on the budget, and that is
    # an answer to a question the session doc left open: is repetition a rule
    # you set or an invariant of a correct run? It is a rule. `describe()`
    # prints every rule a run is under, Session 11 records that line with the
    # result, and Session 12 changes one of them per arm - none of which can
    # be done with an invariant hard-coded in the loop.
    #
    # Two thresholds, because a repeated failure and a repeated success are
    # different events (D78). None on either disables that half: the control
    # arm still COUNTS, it just never stops.
    max_repeated_failures: int | None = DEFAULT_MAX_REPEATED_FAILURES
    max_repeated_calls: int | None = DEFAULT_MAX_REPEATED_CALLS
    # D86. None means "do not check", which is the honest setting for a
    # provider whose window you do not know.
    context_window: int | None = DEFAULT_CONTEXT_WINDOW

    @classmethod
    def from_env(cls, *, pricing: Pricing | None = None) -> Budget:
        """Defaults from .env. CLI flags override this, not the other way round."""
        return cls(
            max_steps=_env_int("MAX_STEPS") or DEFAULT_MAX_STEPS,
            max_tool_calls=_env_int("MAX_TOOL_CALLS") or DEFAULT_MAX_TOOL_CALLS,
            max_seconds=_env_float("MAX_RUN_SECONDS") or DEFAULT_MAX_SECONDS,
            max_total_tokens=_env_int("MAX_TOTAL_TOKENS"),
            max_spend=_env_float("MAX_SPEND"),
            token_reserve=_env_int("TOKEN_RESERVE") or DEFAULT_TOKEN_RESERVE,
            pricing=pricing if pricing is not None else Pricing.from_env(),
            max_repeated_failures=_env_int("MAX_REPEATED_FAILURES")
            or DEFAULT_MAX_REPEATED_FAILURES,
            max_repeated_calls=_env_int("MAX_REPEATED_CALLS")
            or DEFAULT_MAX_REPEATED_CALLS,
            context_window=_env_int("CONTEXT_WINDOW") or DEFAULT_CONTEXT_WINDOW,
        )

    @property
    def needs_pricing(self) -> bool:
        """D38: a spend limit without a price list cannot be enforced.

        Treating it as unlimited is the dangerous reading - the flag is on
        screen, the user believes it, and nothing is enforcing it. The caller
        turns this into a usage error before the run starts.
        """
        return self.max_spend is not None and self.pricing is None

    def describe(self) -> str:
        """One line for the trace. A limit you cannot see was not applied."""
        parts = [f"steps<={self.max_steps}"]
        if self.max_tool_calls is not None:
            parts.append(f"tools<={self.max_tool_calls}")
        if self.max_seconds is not None:
            parts.append(f"{self.max_seconds:g}s")
        if self.max_total_tokens is not None:
            parts.append(f"tokens<={self.max_total_tokens}(+{self.token_reserve} held)")
        if self.max_spend is not None:
            parts.append(f"spend<=${self.max_spend:.4f}")
        # Session 9. A repetition rule nobody can see is a run that stopped
        # for no visible reason; the same argument that put the retry policy
        # in the banner. "off" is printed rather than omitted, because the
        # control arm of Session 12's comparison has to be legible in a trace
        # as a DECISION rather than as a missing word.
        repeats = "/".join(
            "off" if limit is None else str(limit)
            for limit in (self.max_repeated_failures, self.max_repeated_calls)
        )
        parts.append(f"repeat<={repeats}")
        if self.context_window is not None:
            parts.append(f"ctx<={self.context_window}")
        if self.pricing is None:
            parts.append("unpriced")
        return " ".join(parts)


@dataclass
class Ledger:
    """What this run has actually spent. The reading, not the rule.

    Separate from Budget so the two can never be confused at a call site, and
    so a resumed run can start with the dead process's totals already in it
    (D39) without pretending its limits changed.
    """

    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    # monotonic, not time.time(): an NTP correction mid-run must not be able
    # to move a deadline, in either direction.
    started_at: float = field(default_factory=time.monotonic)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def record_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        """Add one response's usage. The step counter is bumped separately,
        because a step that fails before returning usage still happened."""
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        # A provider that does not report the cache split (or an older journal
        # that did not record it) is billed at the EXPENSIVE rate here, not
        # the cheap one. An estimate that drifts low is worse than useless:
        # it is a budget that silently does not hold.
        if cache_hit_tokens == 0 and cache_miss_tokens == 0:
            self.cache_miss_tokens += prompt_tokens
        else:
            self.cache_hit_tokens += cache_hit_tokens
            self.cache_miss_tokens += cache_miss_tokens

    def record_response(self, response: Any) -> None:
        """Convenience for a ModelResponse. Typed loosely on purpose: this
        module must not import the provider adapter."""
        self.steps += 1
        self.record_usage(
            prompt_tokens=getattr(response, "prompt_tokens", 0),
            completion_tokens=getattr(response, "completion_tokens", 0),
            cache_hit_tokens=getattr(response, "prompt_cache_hit_tokens", 0),
            cache_miss_tokens=getattr(response, "prompt_cache_miss_tokens", 0),
        )

    def spend(self, pricing: Pricing | None) -> float | None:
        """Estimated USD, or None when unpriced. Never 0.0 as a stand-in."""
        if pricing is None:
            return None
        return pricing.cost(
            cache_hit=self.cache_hit_tokens,
            cache_miss=self.cache_miss_tokens,
            output=self.completion_tokens,
        )

    def snapshot(self) -> dict[str, Any]:
        """For the journal and the final report."""
        return {
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "elapsed_s": round(self.elapsed_s, 3),
        }


def check_before_step(
    budget: Budget, ledger: Ledger, *, history_tokens: int | None = None
) -> str | None:
    """May the loop make another model call? Returns a stop reason, or None.

    D35. This runs BEFORE the POST, because usage arrives with the response:
    the only moment a token or spend limit can refuse anything is while the
    money is still unspent. Checked afterwards, the same limit is a receipt.

    D36. Token and spend limits are therefore checked against what is already
    spent PLUS a reserve for the response that has not happened yet. The cost
    of being wrong is stopping one step early. The cost of not reserving is a
    limit that is always discovered one response too late.

    Check order is fixed and deliberate, because when two limits blow at once
    the one reported is the one Session 12's report will blame:

        steps -> tool calls -> context -> time -> tokens -> spend

    Deterministic limits first. `step_limit` reproduces on a re-run of the
    same trace; `time_limit` depends on how busy the machine was. Reporting
    the reproducible cause keeps an eval report comparable between runs.
    `context` is deterministic - it is a function of the history and nothing
    else - which is why it goes above the clock rather than below it.

    `history_tokens` is Session 9's D86: an ESTIMATE of what the next request
    will carry, from agent/context.py, supplied by the caller because this
    module does not know what a message looks like. None means the caller did
    not measure, and an unmeasured context is never a reason to stop.
    """
    if ledger.steps >= budget.max_steps:
        return STOP_STEP_LIMIT

    if budget.max_tool_calls is not None and ledger.tool_calls >= budget.max_tool_calls:
        # Not strictly necessary - the cap also fires inside the batch - but a
        # run with no tool budget left can still only produce more tool calls.
        # Paying for that discovery is pointless.
        return STOP_TOOL_LIMIT

    # D86. The same shape as the token reserve one rule below, against the
    # context window rather than the wallet: refuse to START a step whose
    # request would not fit, instead of discovering it as a provider 400 -
    # or, on Ollama, as a silently truncated history that drops the assistant
    # turn whose tool_call ids are still being answered. The reserve is the
    # same number for the same reason: the response has to fit too.
    if (
        budget.context_window is not None
        and history_tokens is not None
        and history_tokens + budget.token_reserve > budget.context_window
    ):
        return STOP_CONTEXT_LIMIT

    if budget.max_seconds is not None and ledger.elapsed_s >= budget.max_seconds:
        return STOP_TIME_LIMIT

    if (
        budget.max_total_tokens is not None
        and ledger.total_tokens + budget.token_reserve > budget.max_total_tokens
    ):
        return STOP_TOKEN_LIMIT

    if budget.max_spend is not None and budget.pricing is not None:
        spent = ledger.spend(budget.pricing) or 0.0
        reserve = budget.token_reserve * budget.pricing.worst_per_token
        if spent + reserve > budget.max_spend:
            return STOP_SPEND_LIMIT

    return None


def check_before_tool_call(budget: Budget, ledger: Ledger) -> str | None:
    """May one more tool run? Returns a stop reason, or None.

    Tool calls and seconds only. Tokens are not spent by a tool - the history
    they grow is billed on the next model call, which `check_before_step`
    already gates.

    The caller must still ANSWER a call it refuses (E1). This function says
    "do not run it", never "do not reply to it".
    """
    if budget.max_tool_calls is not None and ledger.tool_calls >= budget.max_tool_calls:
        return STOP_TOOL_LIMIT
    if budget.max_seconds is not None and ledger.elapsed_s >= budget.max_seconds:
        return STOP_TIME_LIMIT
    return None


def refusal_message(stop_reason: str, budget: Budget) -> str:
    """What a refused tool call tells the model.

    Written as if it will be read, because on a --resume it is exactly what
    the model wakes up to: an observation that this call never ran. Naming the
    limit matters - "cancelled" invites a retry, "the run's tool budget is
    spent" does not.
    """
    if stop_reason == STOP_CANCELLED:
        return "Not run: this run was cancelled. No file was read or changed by this call."
    if stop_reason == STOP_TOOL_LIMIT:
        return (
            f"Not run: this run's tool-call budget ({budget.max_tool_calls}) is "
            "spent. No file was read or changed by this call."
        )
    if stop_reason == STOP_TIME_LIMIT:
        return (
            f"Not run: this run passed its time limit ({budget.max_seconds:g}s). "
            "No file was read or changed by this call."
        )
    if stop_reason == STOP_REPETITION:
        # The one refusal in this file that names a mistake rather than a
        # resource. It is still written to be read: on a --resume this is
        # what the model wakes up to, and "you repeated yourself" with no
        # instruction attached is an observation it cannot act on.
        return (
            "Not run: this run stopped because the same call was made too many "
            "times without making progress. No file was read or changed by this "
            "call."
        )
    if stop_reason == STOP_CONTEXT_LIMIT:
        return (
            "Not run: this conversation no longer fits in the model's context "
            "window. No file was read or changed by this call."
        )
    return "Not run: this run is out of budget. No file was read or changed by this call."
