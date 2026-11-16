"""When to send a failed request again, and how long to wait. (Session 8)

Pure policy. Nothing here sleeps, posts, or reads a journal - it answers one
question and returns a value:

    plan_retry(err, policy, attempt=2, writes_done=False) -> Plan

The mechanism lives in `loop.py`, which is where the journal, the budget and
the knowledge of whether a tool has already written are (D65). Putting the
policy in a wrapper around `model_fn` was the alternative, and it fails on
that third thing: a wrapper cannot know that step 2 edited a file, which is
exactly what decides D69.

The two flags this module reads have been on `ModelError` since Session 1 and
have had no consumer until now:

    retryable     should we try again? A fact about the ERROR.
    safe_to_retry did the request definitely not reach the server? A fact
                  about the WORLD, and the only one that can cost a file.

They are easy to collapse into one and must not be. A read timeout is
retryable and NOT safe to retry: the request went out, the answer never came
back, and nothing you can do will tell you which of those two happened.
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Callable

# Three attempts, not five. The failures a fourth attempt fixes are rare
# enough that the latency it adds to every exhausted run costs more.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY_S = 0.5
DEFAULT_MAX_DELAY_S = 8.0
# A provider that asks for a five-minute wait is not asking for a retry, it is
# telling you to come back later. Sleeping that long inside a run burns a wall
# clock the user set for a reason (D67).
DEFAULT_RETRY_AFTER_CAP_S = 30.0


@dataclass(frozen=True)
class RetryPolicy:
    """Every retry rule for one run, in one value - as Budget is for limits."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_s: float = DEFAULT_BASE_DELAY_S
    max_delay_s: float = DEFAULT_MAX_DELAY_S
    jitter: bool = True
    retry_after_cap_s: float = DEFAULT_RETRY_AFTER_CAP_S

    @classmethod
    def off(cls) -> RetryPolicy:
        """One attempt: the Session 7 behaviour, kept reachable on purpose.

        `run_loop` defaults to this. A loop that silently acquired retries
        would change what every existing test measures, and a test that
        retries three times before failing is a test that takes 10s to tell
        you the same thing.
        """
        return cls(max_attempts=1)

    @classmethod
    def from_env(cls) -> RetryPolicy:
        return cls(
            max_attempts=_env_int("MAX_ATTEMPTS") or DEFAULT_MAX_ATTEMPTS,
            base_delay_s=_env_float("RETRY_BASE_DELAY_S") or DEFAULT_BASE_DELAY_S,
            max_delay_s=_env_float("RETRY_MAX_DELAY_S") or DEFAULT_MAX_DELAY_S,
            jitter=os.getenv("RETRY_JITTER", "1") not in ("0", "false", "no"),
            retry_after_cap_s=(
                _env_float("RETRY_AFTER_CAP_S") or DEFAULT_RETRY_AFTER_CAP_S
            ),
        )

    def describe(self) -> str:
        """One printable line, so the banner can state the rules in force."""
        if self.max_attempts <= 1:
            return "no retries"
        return (
            f"attempts<={self.max_attempts} "
            f"backoff={self.base_delay_s:g}-{self.max_delay_s:g}s"
            + (" jittered" if self.jitter else "")
            + f" retry_after<={self.retry_after_cap_s:g}s"
        )


@dataclass(frozen=True)
class Plan:
    """What to do after one failed attempt.

    `reason` is carried on BOTH outcomes. A run that gave up owes the trace a
    word for why - "the provider said no" and "I had already edited a file"
    are the same exception and different decisions.
    """

    retry: bool
    delay_s: float
    reason: str


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def delay_for(
    policy: RetryPolicy, attempt: int, *, rng: Callable[[], float] = random.random
) -> float:
    """Exponential backoff with full jitter, capped (D66).

    attempt 1 -> base, 2 -> 2*base, 3 -> 4*base, each capped at max_delay_s
    and then multiplied by a uniform [0, 1).

    Full jitter rather than a fixed delay because synchronised clients retry
    in lockstep and rebuild the spike that rate-limited them. With one process
    that is worth nothing today - it is here for Session 13, where runs are
    concurrent, and `jitter=False` exists so a test can read the raw curve.

    `rng` is injected for the same reason the clock is: a test that asserts a
    random delay is a test that fails on a Tuesday.
    """
    raw = min(policy.base_delay_s * (2 ** (attempt - 1)), policy.max_delay_s)
    return raw * rng() if policy.jitter else raw


def plan_retry(
    err,
    policy: RetryPolicy,
    *,
    attempt: int,
    writes_done: bool,
    seconds_left: float | None = None,
    rng: Callable[[], float] = random.random,
) -> Plan:
    """Decide what happens after attempt number `attempt` failed.

    The order of these checks is the design, and it runs cheapest-and-most-
    final first, exactly as `resolve_in_workspace` does:

      1. attempts exhausted      - no judgement needed
      2. the error says no       - `retryable` is the single source of truth,
                                   set where the error is translated (D68)
      3. the world says no       - not safe to retry AND this run has already
                                   written. Resending might apply an edit the
                                   first attempt already applied (D69)
      4. the provider named a wait - honour it, unless it is longer than the
                                   cap, in which case give up rather than
                                   sleep through the run's clock (D67)
      5. the clock says no       - a delay we cannot finish inside the time
                                   limit buys nothing (D70)

    Step 3 is the one worth staring at. `writes_done` is false for a run that
    has only read, which is most runs, so a read timeout IS resent there - the
    worst case is paying twice for an answer. Once a tool has written, the
    worst case is a second edit, and the project has no idempotency key to
    make that safe.
    """
    if attempt >= policy.max_attempts:
        return Plan(False, 0.0, "attempts_exhausted")

    if not getattr(err, "retryable", False):
        return Plan(False, 0.0, "not_retryable")

    if not getattr(err, "safe_to_retry", False) and writes_done:
        return Plan(False, 0.0, "unsafe_after_write")

    retry_after = getattr(err, "retry_after", None)
    if retry_after is not None:
        if retry_after > policy.retry_after_cap_s:
            return Plan(False, 0.0, "retry_after_too_long")
        delay, reason = float(retry_after), "retry_after"
    else:
        delay, reason = delay_for(policy, attempt, rng=rng), "backoff"

    if seconds_left is not None and delay >= seconds_left:
        return Plan(False, 0.0, "no_time_left")

    return Plan(True, delay, reason)


# The sentence each reason becomes for a human. Kept beside the reasons
# themselves so a new one cannot be added without a way to say it out loud.
REASON_TEXT = {
    "attempts_exhausted": "every attempt failed",
    "not_retryable": "this failure does not get a second attempt",
    "unsafe_after_write": (
        "the request may have reached the server and this run has already "
        "edited a file, so resending could apply an edit twice"
    ),
    "retry_after_too_long": "the provider asked for a longer wait than this run allows",
    "no_time_left": "the backoff would not fit inside the time limit",
    "backoff": "backing off",
    "retry_after": "the provider asked for a specific wait",
}


def explain(plan: Plan) -> str:
    return REASON_TEXT.get(plan.reason, plan.reason)
