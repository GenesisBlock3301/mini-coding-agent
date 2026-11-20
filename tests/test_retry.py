"""Tests for the retry policy and the loop's backoff. (Session 8)

No API key, no network - and, the new rule this session adds, **no sleeping**.
Every test here injects the clock. A suite that really waits 0.5s per attempt
is a suite that costs a minute to tell you something it knew instantly, and
the first thing anyone does with a slow suite is stop running it.

Two through-lines:

  1. RETRYING IS AN ACTION. `retryable` answers "should we?" and
     `safe_to_retry` answers "may we?", and the tests that matter most are the
     ones where those two disagree.
  2. A RETRY IS NOT A STEP. Attempts must not quietly consume the step and
     tool-call budgets that Session 5 built, and the only way to see that is
     to count both before and after a run that retried.
"""

from __future__ import annotations

import json

import httpx
import pytest

from agent.budget import STOP_ANSWER, Budget
from agent.loop import run_loop
from agent.model import ModelError, ModelResponse, _classify_status, _retry_after_seconds
from agent.retry import (
    Plan,
    RetryPolicy,
    delay_for,
    explain,
    plan_retry,
)


def err(
    kind: str = "server",
    *,
    retryable: bool = True,
    safe_to_retry: bool = True,
    retry_after: float | None = None,
) -> ModelError:
    return ModelError(
        kind,
        f"scripted {kind}",
        retryable=retryable,
        safe_to_retry=safe_to_retry,
        retry_after=retry_after,
    )


def says(text: str) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="scripted",
        finish_reason="stop",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        duration_ms=0.0,
    )


class Flaky:
    """Raises `failures` in order, then answers. Counts every call."""

    def __init__(self, *failures: ModelError, then: ModelResponse | None = None) -> None:
        self.failures = list(failures)
        self.then = then or says("done")
        self.calls = 0

    def __call__(self, messages, *, tools=None):
        self.calls += 1
        if self.calls <= len(self.failures):
            raise self.failures[self.calls - 1]
        return self.then


class Clock:
    """An injected sleep that records instead of waiting."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


FIXED = {"rng": lambda: 1.0}  # jitter multiplier pinned, so delays are the curve


# --------------------------------------------------------------------------
# The curve (D66)
# --------------------------------------------------------------------------


def test_backoff_doubles_and_then_stops_doubling():
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=8.0, jitter=False)
    assert [delay_for(policy, n) for n in range(1, 7)] == [0.5, 1, 2, 4, 8, 8]


def test_jitter_scales_the_delay_and_never_exceeds_it():
    policy = RetryPolicy(base_delay_s=1.0, jitter=True)
    assert delay_for(policy, 1, rng=lambda: 0.0) == 0.0
    assert delay_for(policy, 1, rng=lambda: 0.5) == 0.5
    assert delay_for(policy, 1, rng=lambda: 1.0) == 1.0


def test_jitter_off_is_the_raw_curve():
    policy = RetryPolicy(base_delay_s=1.0, jitter=False)
    assert delay_for(policy, 2, rng=lambda: 0.0) == 2.0


# --------------------------------------------------------------------------
# The decision (D68, D69, D67, D70)
# --------------------------------------------------------------------------


def test_a_retryable_error_with_attempts_left_backs_off():
    plan = plan_retry(err(), RetryPolicy(jitter=False), attempt=1, writes_done=False)
    assert plan == Plan(True, 0.5, "backoff")


def test_the_last_attempt_does_not_schedule_another():
    policy = RetryPolicy(max_attempts=3)
    assert plan_retry(err(), policy, attempt=3, writes_done=False).reason == (
        "attempts_exhausted"
    )


def test_off_never_retries_anything():
    plan = plan_retry(err(), RetryPolicy.off(), attempt=1, writes_done=False)
    assert plan.retry is False


@pytest.mark.parametrize("kind", ["auth", "bad_request", "billing"])
def test_an_unretryable_error_is_never_resent(kind):
    """Retrying the unretryable is not caution, it is latency."""
    plan = plan_retry(
        err(kind, retryable=False), RetryPolicy(), attempt=1, writes_done=False
    )
    assert plan == Plan(False, 0.0, "not_retryable")


def test_an_unsafe_error_is_resent_while_nothing_has_been_written():
    """A read timeout costs money to repeat. That is an acceptable worst case."""
    plan = plan_retry(
        err("timeout", safe_to_retry=False),
        RetryPolicy(jitter=False),
        attempt=1,
        writes_done=False,
    )
    assert plan.retry is True


def test_an_unsafe_error_is_not_resent_once_a_tool_has_written():
    """D69, the whole reason the policy needs to know about the workspace.

    The request may have arrived. If it did, the edit it carried may have been
    applied. Sending it again is a second edit, and nothing in this project
    can make that idempotent.
    """
    plan = plan_retry(
        err("timeout", safe_to_retry=False),
        RetryPolicy(),
        attempt=1,
        writes_done=True,
    )
    assert plan == Plan(False, 0.0, "unsafe_after_write")
    assert "twice" in explain(plan)


def test_a_safe_error_is_resent_even_after_a_write():
    """`writes_done` gates only the UNSAFE case. A connect timeout never
    reached the server, so what the run has already done is irrelevant."""
    plan = plan_retry(
        err("timeout", safe_to_retry=True),
        RetryPolicy(jitter=False),
        attempt=1,
        writes_done=True,
    )
    assert plan.retry is True


def test_retry_after_replaces_the_curve():
    plan = plan_retry(
        err("rate_limit", retry_after=2.0),
        RetryPolicy(base_delay_s=0.5, jitter=False),
        attempt=1,
        writes_done=False,
    )
    assert plan == Plan(True, 2.0, "retry_after")


def test_a_retry_after_longer_than_the_cap_ends_the_run():
    """A five-minute wait is not a retry, it is an instruction to come back."""
    plan = plan_retry(
        err("rate_limit", retry_after=300.0),
        RetryPolicy(retry_after_cap_s=30.0),
        attempt=1,
        writes_done=False,
    )
    assert plan == Plan(False, 0.0, "retry_after_too_long")


def test_a_delay_that_would_not_fit_in_the_time_limit_is_not_taken():
    plan = plan_retry(
        err(),
        RetryPolicy(base_delay_s=4.0, jitter=False),
        attempt=1,
        writes_done=False,
        seconds_left=1.0,
    )
    assert plan == Plan(False, 0.0, "no_time_left")


def test_every_reason_has_a_sentence():
    """A trace word with no English is a trace word nobody can act on."""
    reasons = {
        "attempts_exhausted", "not_retryable", "unsafe_after_write",
        "retry_after_too_long", "no_time_left", "backoff", "retry_after",
    }
    for reason in reasons:
        assert explain(Plan(False, 0.0, reason)) != reason


def test_policy_from_env(monkeypatch):
    monkeypatch.setenv("MAX_ATTEMPTS", "5")
    monkeypatch.setenv("RETRY_BASE_DELAY_S", "0.25")
    monkeypatch.setenv("RETRY_JITTER", "0")
    policy = RetryPolicy.from_env()
    assert (policy.max_attempts, policy.base_delay_s, policy.jitter) == (5, 0.25, False)


def test_describe_says_no_retries_when_there_are_none():
    assert RetryPolicy.off().describe() == "no retries"


# --------------------------------------------------------------------------
# Retry-After, as the provider actually sends it
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header,expected",
    [
        ("2", 2.0),
        ("2.5", 2.5),
        ("0", 0.0),
        (" 3 ", 3.0),
        ("-1", None),
        ("soon", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # legal, deliberately ignored
        (None, None),
    ],
)
def test_retry_after_header_parsing(header, expected):
    headers = {"retry-after": header} if header is not None else {}
    response = httpx.Response(429, headers=headers, json={})
    assert _retry_after_seconds(response) == expected


@pytest.mark.parametrize("status", [429, 503])
def test_a_throttling_status_carries_its_retry_after(status):
    response = httpx.Response(status, headers={"retry-after": "7"}, json={})
    assert _classify_status(response).retry_after == 7.0


def test_an_error_without_the_header_has_no_retry_after():
    assert _classify_status(httpx.Response(500, json={})).retry_after is None


# --------------------------------------------------------------------------
# The mechanism, through the loop
# --------------------------------------------------------------------------


def start(task: str = "do it") -> list[dict]:
    return [{"role": "system", "content": "s"}, {"role": "user", "content": task}]


def test_two_failures_then_an_answer(tmp_path):
    model = Flaky(err("server"), err("server"))
    clock = Clock()
    result = run_loop(
        start(),
        root=tmp_path,
        tools=None,
        model_fn=model,
        retry=RetryPolicy(base_delay_s=0.5, jitter=False),
        sleep=clock,
    )
    assert result.stop_reason == STOP_ANSWER
    assert model.calls == 3
    assert clock.slept == [0.5, 1.0]


def test_a_retried_run_spends_no_extra_steps_or_tool_calls(tmp_path):
    """Session 5's budgets count work, not attempts (D70)."""
    model = Flaky(err("server"), err("server"))
    result = run_loop(
        start(),
        root=tmp_path,
        tools=None,
        model_fn=model,
        budget=Budget(max_steps=1),
        retry=RetryPolicy(jitter=False),
        sleep=Clock(),
    )
    assert result.stop_reason == STOP_ANSWER
    assert result.steps == 1
    assert result.tool_calls == 0


def test_exhausted_retries_raise_the_last_error_with_a_count(tmp_path):
    model = Flaky(*[err("server") for _ in range(5)])
    clock = Clock()
    with pytest.raises(ModelError) as caught:
        run_loop(
            start(),
            root=tmp_path,
            tools=None,
            model_fn=model,
            retry=RetryPolicy(max_attempts=3, jitter=False),
            sleep=clock,
        )
    assert model.calls == 3
    assert caught.value.attempts == 3
    assert caught.value.gave_up_because == "attempts_exhausted"
    assert len(clock.slept) == 2


def test_an_unretryable_error_is_raised_on_the_first_attempt(tmp_path):
    model = Flaky(err("auth", retryable=False))
    clock = Clock()
    with pytest.raises(ModelError):
        run_loop(
            start(),
            root=tmp_path,
            tools=None,
            model_fn=model,
            retry=RetryPolicy(),
            sleep=clock,
        )
    assert model.calls == 1
    assert clock.slept == []


def test_the_default_loop_still_does_not_retry(tmp_path):
    """Every suite written before this session must measure what it did then."""
    model = Flaky(err("server"))
    with pytest.raises(ModelError):
        run_loop(start(), root=tmp_path, tools=None, model_fn=model, sleep=Clock())
    assert model.calls == 1


def test_the_journal_records_every_attempt(tmp_path):
    from agent.journal import Journal, new_run_id

    journal = Journal.create(tmp_path / "runs", new_run_id())
    model = Flaky(err("server"), err("rate_limit", retry_after=2.0))
    run_loop(
        start(),
        root=tmp_path,
        tools=None,
        model_fn=model,
        journal=journal,
        retry=RetryPolicy(jitter=False),
        sleep=Clock(),
    )
    retries = [e for e in journal.events() if e["t"] == "model_retry"]
    assert [e["attempt"] for e in retries] == [1, 2]
    assert [e["reason"] for e in retries] == ["backoff", "retry_after"]
    assert [e["delay_s"] for e in retries] == [0.5, 2.0]
    assert all(e["retrying"] for e in retries)


def test_the_journal_records_the_decision_to_give_up(tmp_path):
    from agent.journal import Journal, new_run_id

    journal = Journal.create(tmp_path / "runs", new_run_id())
    model = Flaky(err("auth", retryable=False))
    with pytest.raises(ModelError):
        run_loop(
            start(),
            root=tmp_path,
            tools=None,
            model_fn=model,
            journal=journal,
            retry=RetryPolicy(),
            sleep=Clock(),
        )
    last = [e for e in journal.events() if e["t"] == "model_retry"][-1]
    assert last["retrying"] is False
    assert last["reason"] == "not_retryable"
    assert last["kind"] == "auth"


def test_a_resumable_journal_survives_a_retry(tmp_path):
    """model_retry is a new event type. rebuild() must ignore what it does not
    know rather than refusing the journal."""
    from agent.journal import Journal, new_run_id, rebuild

    journal = Journal.create(tmp_path / "runs", new_run_id())
    model = Flaky(err("server"), then=says("fine"))
    run_loop(
        start(),
        root=tmp_path,
        tools=None,
        model_fn=model,
        journal=journal,
        retry=RetryPolicy(jitter=False),
        sleep=Clock(),
    )
    state = rebuild(journal.events())
    assert state.answer_ready is True
    assert state.answer_text == "fine"


# --------------------------------------------------------------------------
# The one that needed Session 6 and Session 7 to be askable
# --------------------------------------------------------------------------


def test_an_unsafe_failure_after_a_real_edit_is_not_resent(tmp_path):
    """End to end: edit a file, then have the next request time out.

    The loop must refuse to resend, because the timed-out request may have
    arrived and applied something. This is the one case where a retry could
    cost a file rather than money.
    """
    target = tmp_path / "f.py"
    target.write_text("value = 1\n")

    edit = {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "replace_text",
            "arguments": json.dumps(
                {"path": "f.py", "old": "value = 1", "new": "value = 2"}
            ),
        },
    }
    wants_edit = ModelResponse(
        text=None,
        model="scripted",
        finish_reason="tool_calls",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        duration_ms=0.0,
        tool_calls=[edit],
    )

    class EditThenTimeout:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, messages, *, tools=None):
            self.calls += 1
            if self.calls == 1:
                return wants_edit
            raise err("timeout", safe_to_retry=False)

    model = EditThenTimeout()
    clock = Clock()
    with pytest.raises(ModelError) as caught:
        run_loop(
            start(),
            root=tmp_path,
            model_fn=model,
            retry=RetryPolicy(max_attempts=3),
            sleep=clock,
        )

    assert model.calls == 2, "the timed-out request must not be sent again"
    assert caught.value.gave_up_because == "unsafe_after_write"
    assert clock.slept == []
    assert target.read_text() == "value = 2\n", "the edit that landed must stand"
