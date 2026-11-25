"""The agent execution loop. (Session 4+)

Owns: conversation history, the step counter, dispatching tool calls, and
matching each tool result to its tool-call ID.

Knows nothing about argparse, printing, or what list_files actually does. It
is handed a message list and a tool catalogue, and returns either a final
answer or an honest statement of which limit stopped it.

The model is INJECTED (model_fn). That is what makes the whole loop testable
with a scripted model - no API key, no network, no sockets. A loop you can
only test live is a loop you test twice and then stop testing.

Decisions locked here (see docs/session-04.md A and docs/session-05.md A):
  D26 the tool message carries the error code inside `content`
  D27 one step == one model call
  D28 the step limit is an explicit failure, never a quiet partial answer
  D30 history keeps everything; truncation happens in tools.py
  D32 the tool-call cap counts the whole run
  D33 a batch is always answered in full, then the run stops
  D34 a duplicate tool-call id is answered and reported, not dropped
  D35 every budget is checked before the call it would authorise

Session 9 adds two stops to the same machinery (see docs/session-09.md A):
  D80 repetition is counted cumulatively per canonical call, not consecutively
  D82 the model is warned once, in the conversation, before the stop
  D86 a step whose request would not fit the context window is never started
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent.budget import (
    DEFAULT_MAX_STEPS,
    LIMIT_STOPS,
    REFUSED_TOOL_ERROR,
    STOP_ANSWER,
    STOP_CANCELLED,
    STOP_CONTEXT_LIMIT,
    STOP_REPETITION,
    STOP_SPEND_LIMIT,
    STOP_STEP_LIMIT,
    STOP_TIME_LIMIT,
    STOP_TOKEN_LIMIT,
    STOP_TOOL_LIMIT,
    Budget,
    Ledger,
    check_before_step,
    check_before_tool_call,
    refusal_message,
)
from agent.context import ContextSize, measure
from agent.journal import (
    MODEL_PENDING,
    SCHEMA,
    MODEL_RETRY,
    MODEL_RESULT,
    RUN_FINISHED,
    RUN_STARTED,
    TOOL_INTENT,
    TOOL_OUTCOME,
    Journal,
    ResumeState,
)
from agent.model import ModelError, ModelResponse, call_model
from agent.replay import (
    CHANGED,
    INTERRUPTED_TOOL_ERROR,
    ReplayPlan,
    before_fingerprint,
    plans_for,
)
from agent.repetition import Repetition, Tally
from agent.retry import RetryPolicy, plan_retry
from agent.tools import TOOL_SCHEMAS, ToolResult, dispatch

# Re-exported so callers that only care about stopping do not have to know
# that budget.py exists. The names are defined there because the journal and
# Session 11's evaluator read them without importing the loop.
__all__ = [
    "DEFAULT_MAX_STEPS",
    "LIMIT_STOPS",
    "STOP_ANSWER",
    "STOP_CONTEXT_LIMIT",
    "STOP_REPETITION",
    "STOP_SPEND_LIMIT",
    "STOP_STEP_LIMIT",
    "STOP_TIME_LIMIT",
    "STOP_TOKEN_LIMIT",
    "STOP_TOOL_LIMIT",
    "LoopResult",
    "run_loop",
]


class ModelFn(Protocol):
    """All the loop needs from a model. call_model satisfies it; so does a fake."""

    def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...


@dataclass
class LoopResult:
    """The outcome of a whole run - not of one call.

    Token totals come from the LEDGER, not from `responses`: a resumed run
    spent what the dead process spent, and a budget that forgets that is a
    budget you can reset by killing the process (D39).
    """

    text: str | None
    stop_reason: str
    steps: int
    messages: list[dict[str, Any]]
    responses: list[ModelResponse] = field(default_factory=list)
    resumed: bool = False
    replayed_tools: int = 0
    ledger: Ledger = field(default_factory=Ledger)
    budget: Budget = field(default_factory=Budget)
    # Ids the model sent twice in one batch, or sent empty. Not an error you
    # can fix - the provider matches on the id it chose - but never silent.
    malformed_call_ids: list[str] = field(default_factory=list)
    # Session 6, D52. Workspace-relative paths this run actually changed, in
    # the order they were changed, one entry per successful edit - so a file
    # edited twice appears twice. The plan's rule for limits is "stop
    # explicitly when a limit is reached, AND REPORT CHANGES ALREADY MADE";
    # until this session there were never any changes to report.
    changed_paths: list[str] = field(default_factory=list)
    # True when files changed and no passing run_tests came after the last change.
    unverified_changes: bool = False
    # Session 8, D74/D75. Every tool error this run produced, in order, and
    # how many of them were a byte-identical repeat of a call that had already
    # failed. Neither stops anything - they are the measurement that says
    # whether Session 9 needs to.
    tool_errors: list[str] = field(default_factory=list)
    repeated_failures: int = 0
    # Session 9. The same measurement for calls that SUCCEEDED, which D78
    # argues about and which nothing counted before: a run that read one file
    # nine times is not visible in `repeated_failures` at all.
    repeated_calls: int = 0
    # The longest run of one identical call, whatever its outcome. Present
    # even when nothing stopped, because a number that only exists when a
    # rule fires cannot tell you the threshold is wrong.
    longest_repeat: int = 0
    # The call that ended the run, named the way the model sent it. None
    # unless stop_reason is `repetition`.
    repetition_stopped_on: str | None = None
    # Session 9, D85. The last measurement of the conversation this run made.
    context: ContextSize | None = None
    # Interrupted calls a resume refused to replay, by verdict (D72).
    interrupted_calls: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop_reason == STOP_ANSWER

    @property
    def stopped_by_limit(self) -> bool:
        return self.stop_reason in LIMIT_STOPS

    @property
    def tool_calls(self) -> int:
        return self.ledger.tool_calls

    @property
    def prompt_tokens(self) -> int:
        return self.ledger.prompt_tokens

    @property
    def completion_tokens(self) -> int:
        return self.ledger.completion_tokens

    @property
    def total_tokens(self) -> int:
        return self.ledger.total_tokens

    @property
    def cache_hit_tokens(self) -> int:
        return self.ledger.cache_hit_tokens

    @property
    def cost_usd(self) -> float | None:
        """Estimated, from configured prices. None when unpriced - never 0.0."""
        return self.ledger.spend(self.budget.pricing)

    @property
    def elapsed_s(self) -> float:
        """Wall clock for this process. Includes time spent in tools."""
        return self.ledger.elapsed_s

    @property
    def duration_ms(self) -> float:
        """Model time only, this process. Latency, not elapsed time."""
        return sum(r.duration_ms for r in self.responses)


def assistant_message(response: ModelResponse) -> dict[str, Any]:
    """The model's own turn, put back into the history verbatim.

    tool_calls must survive intact: the ids inside them are what the tool
    messages answer. Appending the tool results WITHOUT this message first is
    the bug everyone writes once - the provider rejects the next request with
    a 400, because a role="tool" message must follow the assistant turn that
    asked for it.
    """
    message: dict[str, Any] = {"role": "assistant", "content": response.text}
    if response.tool_calls:
        message["tool_calls"] = response.tool_calls
    return message


def tool_message(
    call: dict[str, Any], result: ToolResult, *, note: str = ""
) -> dict[str, Any]:
    """One tool result, addressed to the call it answers.

    D26: the error code goes INSIDE content, because content is the model's
    entire view of what happened - the wire has no status field here.
    "ERROR outside_workspace: ..." reads as a refusal it must work around;
    the same sentence without the code reads like data it can quote.

    The truncation note matters for the same reason: a model that does not
    know it saw half a file will answer confidently about the missing half.

    `note` is Session 9's D82 warning, and it is appended HERE rather than
    inside the ToolResult on purpose. ToolResult is frozen precisely so the
    loop cannot edit what the tool reported - a trace whose results were
    rewritten after the fact no longer describes what the tool did. The
    conversation is a different object: the loop is entitled to add its own
    sentence to the message it writes, as long as the result it records
    stays the tool's own. So the model reads one message, and the journal
    keeps both halves distinguishable.
    """
    content = result.content
    if not result.ok:
        content = f"ERROR {result.error_code}: {content}"
    if result.truncated:
        content = f"{content}\n[truncated: this is not the whole result]"
    if note:
        content = f"{content}{note}"
    return {
        "role": "tool",
        # A call with no id cannot be answered; sending "" makes the provider
        # say so loudly on the next request instead of silently dropping it.
        "tool_call_id": call.get("id") or "",
        "content": content,
    }


def _task_of(history: list[dict[str, Any]]) -> str | None:
    for message in reversed(history):
        if message.get("role") == "user":
            return message.get("content")
    return None


@dataclass
class _Run:
    """One run's moving parts, in one value.

    Everything here used to be a local variable in `run_loop` plus a closure
    that captured seven of them. Same invariants, same order of operations -
    but each phase of a step is now a method you can read on its own, and the
    call sites stop threading ten arguments through.

    `history` is the run's memory and the only thing the model ever sees.
    `step` and `ledger.steps` are kept separately on purpose: `step` is bumped
    BEFORE the POST so `MODEL_PENDING` can name the step that may have been
    billed, while the ledger counts responses that actually came back.
    """

    root: Path
    budget: Budget
    ledger: Ledger
    history: list[dict[str, Any]]
    journal: Journal | None = None
    resume: ResumeState | None = None
    on_response: Any = None
    on_tool: Any = None
    on_context: Any = None
    # The catalogue sent with every request. Held only so the context
    # measurement can count it - the loop itself still receives it per call.
    tools: list[dict[str, Any]] | None = None
    # Session 8. The policy is a value (D65) and the clock is injected for the
    # same reason model_fn is: a suite that really sleeps is a suite you stop
    # running. Default: no retries, so every test written before this session
    # still measures what it measured then.
    retry: RetryPolicy = field(default_factory=RetryPolicy.off)
    sleep: Any = time.sleep
    dispatcher: Any = None
    cancelled: Any = None
    step: int = 0
    pending: list[dict[str, Any]] = field(default_factory=list)
    responses: list[ModelResponse] = field(default_factory=list)
    # Ids the model sent twice in one batch, or sent empty.
    malformed: list[str] = field(default_factory=list)
    # D52. Appended only by _answer, only when a tool reported a change.
    changed: list[str] = field(default_factory=list)
    # A change landed and no passing run_tests has followed it.
    unverified: bool = False
    # Session 8, D72. In-flight calls from a dead process, already decided.
    # Empty for a fresh run; emptied as the resumed batch consumes it.
    replay_plans: dict[str, ReplayPlan] = field(default_factory=dict)
    interrupted: list[str] = field(default_factory=list)
    # D74/D75. Error codes in order, and the (name, arguments) pairs that have
    # already failed once, so an identical resend is countable.
    errors: list[str] = field(default_factory=list)
    # Session 9. `failed_before` and `repeats` were Session 8's measurement,
    # kept honest but moved: the same counting, plus successes, plus path
    # canonicalisation, now lives in `Tally` where a rule can read it.
    tally: Tally = field(init=False)
    stopped_on: Repetition | None = None
    # The provider's exact prompt_tokens for the LAST request, which is the
    # only exact context number that exists and is always one step stale.
    last_prompt_tokens: int | None = None

    def __post_init__(self) -> None:
        self.tally = Tally(
            root=self.root,
            max_failures=self.budget.max_repeated_failures,
            max_successes=self.budget.max_repeated_calls,
        )

    # ---- how big is the conversation --------------------------------------

    def context(self) -> ContextSize:
        """The history as the next request will carry it (D85).

        Measured fresh every time rather than cached: the history grew since
        the last measurement by exactly the tool results this step added,
        which is the growth the check exists to notice.
        """
        return measure(
            self.history,
            tools=self.tools,
            window=self.budget.context_window,
            last_prompt_tokens=self.last_prompt_tokens,
        )

    # ---- one model call ---------------------------------------------------

    def take_step(
        self, model_fn: ModelFn, tools: list[dict[str, Any]] | None
    ) -> ModelResponse:
        """POST once, record it, put the model's turn back into the history.

        The journal writes bracket the call: MODEL_PENDING before, so a death
        here says "a call may have been billed with no response" - and
        MODEL_RESULT before any tool runs, so a resume never asks the model
        for a fresh plan while the old plan's tools have already executed.

        A ModelError propagates once the retry policy is out of answers,
        carrying `attempts` so the caller can report how many requests were
        sent rather than describing the last one as if it were the only one.
        """
        self.step += 1
        self._journal(MODEL_PENDING, step=self.step)

        response = self._post_with_retries(model_fn, tools)
        self.responses.append(response)
        self.ledger.record_response(response)
        # D85's error bar. The provider has now told us exactly what the
        # history we just sent was worth; the estimate for the NEXT request
        # is checked against it rather than trusted on its own.
        self.last_prompt_tokens = response.prompt_tokens or None

        message = assistant_message(response)
        self.history.append(message)
        self._journal(
            MODEL_RESULT,
            step=self.step,
            message=message,
            finish_reason=response.finish_reason,
            # Session 10, D90. All three lived in `ModelResponse` - in RAM -
            # and Session 8 settled what happens to things that live in RAM.
            #
            # `duration_ms` is the provider's own latency. The journal could
            # almost derive it from the gap between model_pending.at and this
            # record, and that number is wrong in the one case you care
            # about: it swallows retry backoff and client overhead, so a run
            # that slept 8s between two attempts reads as a slow model.
            #
            # `model` because a trace that cannot say which model answered
            # cannot be compared with another trace, and Session 12 compares
            # one variable at a time.
            #
            # `tools` because `--tools` versus not is a DIFFERENT AGENT and
            # the single biggest confounder waiting for that comparison.
            # Names plus a short digest of the schemas: the names answer
            # "what could it call", the digest catches a description someone
            # edited between two runs, which is a prompt change by another
            # name and otherwise invisible.
            duration_ms=round(response.duration_ms, 1),
            model=response.model,
            tools=self._tools_offered(tools),
            usage={
                "prompt_tokens": response.prompt_tokens,
                "completion_tokens": response.completion_tokens,
                "total_tokens": response.total_tokens,
                # Recorded so a resumed run can price itself correctly;
                # without the split, replayed input is billed at the
                # expensive rate forever.
                "prompt_cache_hit_tokens": response.prompt_cache_hit_tokens,
                "prompt_cache_miss_tokens": response.prompt_cache_miss_tokens,
            },
        )
        if self.on_response is not None:
            self.on_response(self.step, response)
        return response

    # ---- one batch of tool calls -----------------------------------------

    def run_pending(self) -> str | None:
        """Run every pending call and append its answer. Returns a stop reason.

        No pending calls is a no-op returning None, so the caller never has to
        ask first.

        Two invariants, and the whole session rests on them:

        1. **Every call in the batch gets exactly one tool message, in order.**
           Skip one and the next POST is a 400 - the provider requires every
           tool_call.id in an assistant turn to be answered before it will
           accept another request. Ordering matters less for read-only tools
           and matters completely in Session 6, when two edits can land in
           either order.

        2. **A budget stops the RUN, never the BATCH (D33).** When the
           tool-call cap or the deadline falls at call 2 of 3, calls 2 and 3
           still get a tool message - a refusal naming the limit - and the
           stop happens after the batch. "Abort the rest" is not a stopping
           rule, it is a corrupted conversation that fails one step later with
           an error about nothing.

        TOOL_INTENT is written BEFORE dispatch and TOOL_OUTCOME after, so a
        journal that holds an intent with no outcome says precisely: this tool
        was entered and may have half-finished. A REFUSED call is the
        deliberate mirror image - an outcome with no intent, meaning "answered
        without ever running".
        """
        calls, self.pending = self.pending, []
        stop_reason: str | None = None
        seen_ids: set[str] = set()

        for call in calls:
            call_id = call.get("id") or ""

            # D34. Cannot be fixed here - the id belongs to the provider - so
            # it is recorded instead. A batch whose ids collide will get two
            # tool messages with the same tool_call_id, which is the
            # provider's problem to explain and your trace's job to have
            # predicted.
            if not call_id or call_id in seen_ids:
                self.malformed.append(call_id)
            seen_ids.add(call_id)

            # D72. Only ever non-empty on the first batch of a resumed run:
            # these ids belong to calls the dead process entered and never
            # answered. `pop` because an id is decided once - a second batch
            # carrying the same id would be a provider bug, not a crash.
            #
            # Checked BEFORE the budget gate, and that is not a hole in D35.
            # The gate exists to stop a call it would AUTHORISE; this call was
            # authorised and executed by a process that no longer exists. A
            # limit reached in the meantime cannot un-write the file, and
            # answering "the tool-call limit stopped this" about an edit that
            # already landed is the false report this whole phase is about.
            # The slot is still counted, so the run stops at the next call
            # rather than this one.
            plan = self.replay_plans.pop(call_id, None)
            if plan is not None and not plan.replays:
                self._interrupted(call, plan)
                continue

            # The gate is per call, not per batch: a batch of three with one
            # call left in the budget runs exactly one.
            if stop_reason is None:
                stop_reason = check_before_tool_call(self.budget, self.ledger)

            if self.cancelled is not None and self.cancelled():
                stop_reason = STOP_CANCELLED
            if stop_reason is not None:
                self._refuse(call, stop_reason)
            else:
                # Session 9. The only gate in this loop that runs AFTER the
                # tool rather than before it, because the outcome is half of
                # what it counts (see Tally.record). The batch still
                # finishes: the calls behind this one are refused with the
                # repetition reason, exactly as they would be for a budget.
                stop_reason = self._dispatch(call)

        return stop_reason

    def _refuse(self, call: dict[str, Any], stop_reason: str) -> None:
        """Answer a call the budget will not let run (E1).

        An outcome with no intent: the id is answered, the tool never ran, and
        the trace says which limit did it.
        """
        result = ToolResult(
            ok=False,
            content=refusal_message(stop_reason, self.budget),
            error_code=REFUSED_TOOL_ERROR,
        )
        self._answer(
            call,
            result,
            refused=True,
            stop_reason=stop_reason,
        )

    def _dispatch(self, call: dict[str, Any]) -> str | None:
        """Run one tool call, answer it, and say whether the run should stop."""
        function = call.get("function") or {}

        name = function.get("name", "")
        arguments = function.get("arguments", "")

        self._journal(
            TOOL_INTENT,
            step=self.step,
            call_id=call.get("id") or "",
            name=name,
            arguments=arguments,
            # D73. What the file looked like a moment before the tool was
            # entered. None for every tool that cannot write, so its absence
            # on a read is silence rather than a gap. This is the record that
            # makes "did the edit land?" answerable after the process is gone
            # - Session 6's step 9, left open until now.
            before=before_fingerprint(name, arguments, root=self.root),
        )
        # Counted before dispatch, not after: a tool that hangs or crashes
        # still consumed its slot. Counting on success is how a failing tool
        # gets retried forever inside its own budget.
        self.ledger.tool_calls += 1
        result = (self.dispatcher or dispatch)(name, arguments, root=self.root)

        # D74/D75. An unknown tool name and an unparseable argument list are
        # already answered correctly; what was undecided is what they COST.
        # They cost a tool call, like any other dispatch, because the slot was
        # spent whether or not the tool existed. Counting the repeats is the
        # rest of the answer: a model can send the same wrong call forever and
        # nothing in this loop notices, so the loop is made to notice out loud
        # rather than to stop - stopping is Session 9's decision to take.
        if not result.ok:
            self.errors.append(result.error_code or "error")

        # Session 9. Counted after the answer is known, warned at one below
        # the threshold, and stopped AT it. The tool message carries the
        # warning (D82) so the model gets it in the only channel it reads;
        # the ToolResult is left exactly as the tool returned it.
        repetition = self.tally.record(name, arguments, ok=result.ok)
        note = repetition.notice if self.tally.should_warn(repetition) else ""
        self._answer(call, result, note=note, repeat=repetition.count)

        if repetition.stops:
            self.stopped_on = repetition
            return STOP_REPETITION
        return None

    def _interrupted(self, call: dict[str, Any], plan: ReplayPlan) -> None:
        """Answer a call the dead process entered and this one will not repeat.

        The mirror of `_refuse`: an outcome with no intent of its own, because
        the intent is already on disk - written by the process that died.

        CHANGED is reported as a SUCCESS carrying `changed_path`, and that is
        the whole point of D72. The alternative - replaying the edit - gets a
        zero-match refusal, which tells the model its edit failed when the
        file on disk says it succeeded. "The agent said it changed nothing and
        the file changed" is the worst of the four outcomes, and it is the one
        a naive replay produces every time.

        The tool call is counted. It ran once already, in a process whose
        outcome record never landed, so the ledger never saw it; not counting
        it here is how a crash loop refills a tool budget.
        """
        landed = plan.verdict == CHANGED
        self.ledger.tool_calls += 1
        self.interrupted.append(plan.verdict)
        result = ToolResult(
            ok=landed,
            content=plan.message,
            error_code=None if landed else INTERRUPTED_TOOL_ERROR,
            changed_path=plan.path if landed else None,
        )
        if not landed:
            self.errors.append(INTERRUPTED_TOOL_ERROR)
        self._answer(
            call,
            result,
            interrupted=plan.verdict,
            before=plan.before,
            now=plan.now,
        )

    def _answer(
        self, call: dict[str, Any], result: ToolResult, *, note: str = "", **extra: Any
    ) -> None:
        """History, then journal, then the caller - in that order.

        Durable before the caller is told, so a crash inside `on_tool` still
        leaves a resumable journal.
        """
        name = (call.get("function") or {}).get("name")
        if result.changed_path is not None and name not in ("run_python", "run_tests"):
            self.unverified = True
            note = (note + "\n" if note else "") + (
                "Next: call run_tests to verify this change before you give your final answer."
            )
        elif name == "run_tests" and result.ok:
            self.unverified = False
        message = tool_message(call, result, note=note)
        self.history.append(message)

        # D51/D52. Collected HERE rather than in _dispatch, because _answer is
        # the single point every result passes through - including a refusal,
        # which must never add a path. A tool that did not succeed changed
        # nothing, so there is nothing to collect.
        if result.changed_path is not None:
            self.changed.append(result.changed_path)

        self._journal(
            TOOL_OUTCOME,
            step=self.step,
            call_id=call.get("id") or "",
            ok=result.ok,
            error_code=result.error_code,
            # D51. The journal's record SHAPE does not change; what it means
            # does. Before Session 6 a tool_outcome said "a read happened".
            # Now this field is what makes "did the file change?" answerable
            # from the trace alone - and it is written after the rename, so a
            # tool_intent with no tool_outcome means the edit did not land.
            # That is a guarantee the atomic write makes, not the journal.
            changed_path=result.changed_path,
            message=message,
            **extra,
        )
        if self.on_tool is not None:
            self.on_tool(self.step, call, result)

    # ---- the single exit --------------------------------------------------

    def finish(
        self, stop_reason: str, *, text: str | None = None, replayed: bool = False
    ) -> LoopResult:
        """One exit. Every stop reason is journalled with the same totals, so
        a trace never has to be read differently depending on how it ended."""
        context = self.context()
        self._journal(
            RUN_FINISHED,
            stop_reason=stop_reason,
            replayed=replayed,
            steps=self.ledger.steps,
            totals=self.ledger.snapshot(),
            cost_usd=self.ledger.spend(self.budget.pricing),
            # Session 9, and an early answer to Session 10's self-test #8:
            # both of today's numbers are written to disk at the one event
            # every run produces. The per-signature counts are not - they are
            # re-derivable from the tool_intent records, which is the cheaper
            # place to keep them and the only place that survives a crash
            # with no run_finished at all.
            repeats={
                "failures": self.tally.repeated_failures,
                "calls": self.tally.repeated_calls,
                "longest": self.tally.worst,
                "stopped_on": self.stopped_on.describe if self.stopped_on else None,
            },
            context={
                "messages": context.messages,
                "estimated_tokens": context.estimated_tokens,
                "last_prompt_tokens": context.last_prompt_tokens,
                "window": context.window,
            },
        )
        return LoopResult(
            text=text,
            stop_reason=stop_reason,
            steps=self.step,
            messages=self.history,
            responses=self.responses,
            resumed=self.resume is not None,
            replayed_tools=self.resume.replayed_tools if self.resume else 0,
            ledger=self.ledger,
            budget=self.budget,
            malformed_call_ids=self.malformed,
            changed_paths=self.changed,
            unverified_changes=self.unverified,
            tool_errors=self.errors,
            repeated_failures=self.tally.repeated_failures,
            repeated_calls=self.tally.repeated_calls,
            longest_repeat=self.tally.worst,
            repetition_stopped_on=(
                self.stopped_on.describe if self.stopped_on else None
            ),
            context=context,
            interrupted_calls=self.interrupted,
        )

    def _seconds_left(self) -> float | None:
        """What the wall clock still allows, or None when it is unlimited.

        A backoff is the only thing in this loop that spends time without
        doing anything, which makes it the only one that has to ask (D70).
        """
        if self.budget.max_seconds is None:
            return None
        return self.budget.max_seconds - self.ledger.elapsed_s

    def _post_with_retries(
        self, model_fn: ModelFn, tools: list[dict[str, Any]] | None
    ) -> ModelResponse:
        """Send the request until it answers or the policy stops us.

        Every decision is journaled BEFORE the sleep that follows it, so a
        process killed mid-backoff still leaves a record of the attempt that
        was already paid for.

        A retry is not a step and not a tool call. It spends tokens the
        provider never reported (a failed request can still be billed) and
        seconds the wall clock notices by itself (D70).

        `writes_done` is what makes this different from any other backoff:
        once a tool in THIS run has changed the workspace, a request that may
        already have arrived is no longer safe to send again (D69), and
        `self.changed` is the loop's own record of exactly that.
        """
        attempt = 1
        while True:
            try:
                return model_fn(self.history, tools=tools)
            except ModelError as err:
                plan = plan_retry(
                    err,
                    self.retry,
                    attempt=attempt,
                    writes_done=bool(self.changed),
                    seconds_left=self._seconds_left(),
                )
                self._journal(
                    MODEL_RETRY,
                    step=self.step,
                    attempt=attempt,
                    kind=err.kind,
                    retrying=plan.retry,
                    delay_s=round(plan.delay_s, 3),
                    reason=plan.reason,
                    retryable=err.retryable,
                    safe_to_retry=err.safe_to_retry,
                )
                if not plan.retry:
                    # The caller sees the LAST failure - the one that matters -
                    # carrying how many there were and the word for why we
                    # stopped sending.
                    err.attempts = attempt
                    err.gave_up_because = plan.reason
                    raise
                self.sleep(plan.delay_s)
                attempt += 1

    @staticmethod
    def _tools_offered(tools: list[dict[str, Any]] | None) -> dict[str, Any] | None:
        """Which tools this request carried, small enough to write every step.

        None means none were offered, which is a real configuration (the
        plain `--tools`-less CLI) and must stay distinguishable from "not
        recorded" - a generation-0 journal has no key here at all.
        """
        if not tools:
            return None
        names = [
            (tool.get("function") or {}).get("name", "?") for tool in tools
        ]
        digest = hashlib.sha256(
            json.dumps(tools, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]
        return {"names": names, "digest": digest}

    def _journal(self, event: str, **fields: Any) -> None:
        """No-op when the run is not journalled. The `if journal is not None`
        that used to sit at six call sites lives here instead."""
        if self.journal is not None:
            self.journal.append(event, **fields)


def _start(
    messages: list[dict[str, Any]],
    *,
    root: Path,
    budget: Budget | None,
    max_steps: int | None,
    journal: Journal | None,
    resume: ResumeState | None,
    on_response: Any,
    on_tool: Any,
    on_context: Any,
    tools: list[dict[str, Any]] | None,
    retry: RetryPolicy,
    sleep: Any,
    dispatcher: Any,
    cancelled: Any,
) -> _Run:
    """Build the run - from the first message, or from a journal.

    The resume branch is the reason this is a function rather than four lines
    in `run_loop`: a restarted run inherits the dead process's tokens and tool
    calls (D39), because a budget that resets on restart is a budget you clear
    with `kill -9`. Seconds are deliberately not inherited - the wall clock
    protects this process, the token and call counts protect the run.
    """
    if budget is not None and max_steps is not None:
        raise ValueError("pass budget= or max_steps=, not both")
    if budget is None:
        budget = Budget(max_steps=max_steps or DEFAULT_MAX_STEPS)

    run = _Run(
        root=root,
        budget=budget,
        ledger=Ledger(),
        history=list(messages),
        journal=journal,
        resume=resume,
        on_response=on_response,
        on_tool=on_tool,
        on_context=on_context,
        tools=tools,
        retry=retry,
        sleep=sleep,
        dispatcher=dispatcher,
        cancelled=cancelled,
    )

    if resume is None:
        run._journal(
            RUN_STARTED,
            # D94. One field per RUN, not per record: a reader needs to know
            # what it is holding from line one, and a version on every line
            # is a field written 1,600 times to answer a question asked once.
            schema=SCHEMA,
            task=_task_of(run.history),
            messages=run.history,
            budget=budget.describe(),
            # D62. Two runs of one task are only comparable if you can tell
            # which copy of the fixture each one edited. Since Session 7 that
            # is no longer always ./playground, and a trace that does not say
            # which directory it changed cannot be replayed against anything.
            workspace=str(root),
        )
        return run

    run.history = list(resume.messages)
    run.step = resume.steps_done
    run.pending = list(resume.unanswered_calls)
    # D72. Decided here, before the first batch runs and before anything is
    # sent to the provider, because the verdict is a statement about the
    # workspace as the resume found it. Reading the files later - after this
    # run's own edits - would compare against a workspace this process had
    # already changed.
    run.replay_plans = plans_for(resume, root=root)
    run.ledger.steps = resume.steps_done
    run.ledger.tool_calls = resume.tool_calls_done
    # A repetition counter that restarts on --resume is a counter you clear
    # with `kill -9`, which is the argument D39 already made about tokens.
    # Rebuilt from the journal's own tool_intent/tool_outcome pairs, so a run
    # that was stuck before the crash is still stuck after it.
    run.tally.seed(resume.calls_done)
    # D52 across a restart. Also what makes D69 hold on a resumed run: a
    # request that is not safe to send twice is not made safe by the process
    # that made the edit having died.
    run.changed = list(resume.changed_paths)
    run.unverified = bool(run.changed)  # conservative: the tests may or may not have run
    run.ledger.record_usage(
        prompt_tokens=resume.prompt_tokens,
        completion_tokens=resume.completion_tokens,
        cache_hit_tokens=resume.cache_hit_tokens,
        cache_miss_tokens=resume.cache_miss_tokens,
    )
    return run


def run_loop(
    messages: list[dict[str, Any]],
    *,
    root: Path,
    tools: list[dict[str, Any]] | None = TOOL_SCHEMAS,
    budget: Budget | None = None,
    max_steps: int | None = None,
    model_fn: ModelFn = call_model,
    on_response=None,
    on_tool=None,
    on_context=None,
    journal: Journal | None = None,
    resume: ResumeState | None = None,
    retry: RetryPolicy | None = None,
    sleep: Any = time.sleep,
    dispatcher=None,
    cancelled=None,
) -> LoopResult:
    """Call, execute, feed back, repeat - until an answer or a limit.

    `budget` carries every stopping rule (D31). `max_steps` remains as a
    shorthand for the common case of changing only that one; passing both is
    a caller bug, not a precedence puzzle to resolve silently.

    With `journal`, every event is on disk before the side effect it
    authorises. With `resume`, the run restarts from a journal instead of from
    the first message: recorded model turns are replayed for free, and the
    ledger starts from what the dead process already spent (D39) - tokens and
    tool calls belong to the run, the wall clock to the process.

    `on_context` is called once per step with the measured size of the
    history that is about to be sent (Session 9, D85). Optional, like the
    other two callbacks: the loop measures either way, because the limit
    depends on it - the callback only decides whether a human sees it.

    `retry` decides what happens when the provider fails. The default is
    RetryPolicy.off() - one attempt, which is what every session before this
    one did. A ModelError still propagates to the caller untouched; what
    changes is how many requests were sent before it did.

    The body below is the whole agent. Everything it calls is in `_Run`.
    """
    run = _start(
        messages,
        root=root,
        budget=budget,
        max_steps=max_steps,
        journal=journal,
        resume=resume,
        on_response=on_response,
        on_tool=on_tool,
        on_context=on_context,
        tools=tools,
        retry=retry or RetryPolicy.off(),
        sleep=sleep,
        dispatcher=dispatcher,
        cancelled=cancelled,
    )

    # The answer was generated and paid for before the crash. Resuming it costs
    # nothing: no POST, no tokens. This is the whole point of the journal.
    if resume is not None and resume.answer_ready:
        return run.finish(STOP_ANSWER, text=resume.answer_text, replayed=True)

    while True:
        if cancelled is not None and cancelled() and not run.pending:
            return run.finish(STOP_CANCELLED)
        # Tool calls the dead process never answered, plus the normal path.
        # An empty batch is a no-op, which is what keeps this body flat.
        stop_reason = run.run_pending()
        # After the batch, never inside it. The history is complete and a
        # --resume can pick it up from here.
        if stop_reason is not None:
            return run.finish(stop_reason)

        if cancelled is not None and cancelled():
            return run.finish(STOP_CANCELLED)

        # D35: before the POST. Covers the step limit, the clock, and anything
        # the next response would spend.
        #
        # D86 joins it here rather than becoming a second gate, because the
        # question is identical - may the loop make another model call? - and
        # two gates is how one of them gets forgotten at one of the call
        # sites. The measurement happens whether or not anyone is watching.
        context = run.context()
        if run.on_context is not None:
            run.on_context(run.step + 1, context)
        stop_reason = check_before_step(
            run.budget, run.ledger, history_tokens=context.estimated_tokens
        )
        if stop_reason is not None:
            return run.finish(stop_reason)

        response = run.take_step(model_fn, tools)

        # No tools requested == the model considers itself done.
        if not response.tool_calls:
            return run.finish(STOP_ANSWER, text=response.text)

        run.pending = list(response.tool_calls)
