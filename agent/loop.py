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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent.budget import (
    DEFAULT_MAX_STEPS,
    LIMIT_STOPS,
    REFUSED_TOOL_ERROR,
    STOP_ANSWER,
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
from agent.journal import (
    MODEL_PENDING,
    MODEL_RESULT,
    RUN_FINISHED,
    RUN_STARTED,
    TOOL_INTENT,
    TOOL_OUTCOME,
    Journal,
    ResumeState,
)
from agent.model import ModelResponse, call_model
from agent.tools import TOOL_SCHEMAS, ToolResult, dispatch

# Re-exported so callers that only care about stopping do not have to know
# that budget.py exists. The names are defined there because the journal and
# Session 11's evaluator read them without importing the loop.
__all__ = [
    "DEFAULT_MAX_STEPS",
    "LIMIT_STOPS",
    "STOP_ANSWER",
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


def tool_message(call: dict[str, Any], result: ToolResult) -> dict[str, Any]:
    """One tool result, addressed to the call it answers.

    D26: the error code goes INSIDE content, because content is the model's
    entire view of what happened - the wire has no status field here.
    "ERROR outside_workspace: ..." reads as a refusal it must work around;
    the same sentence without the code reads like data it can quote.

    The truncation note matters for the same reason: a model that does not
    know it saw half a file will answer confidently about the missing half.
    """
    content = result.content
    if not result.ok:
        content = f"ERROR {result.error_code}: {content}"
    if result.truncated:
        content = f"{content}\n[truncated: this is not the whole result]"
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
    step: int = 0
    pending: list[dict[str, Any]] = field(default_factory=list)
    responses: list[ModelResponse] = field(default_factory=list)
    # Ids the model sent twice in one batch, or sent empty.
    malformed: list[str] = field(default_factory=list)
    # D52. Appended only by _answer, only when a tool reported a change.
    changed: list[str] = field(default_factory=list)

    # ---- one model call ---------------------------------------------------

    def take_step(
        self, model_fn: ModelFn, tools: list[dict[str, Any]] | None
    ) -> ModelResponse:
        """POST once, record it, put the model's turn back into the history.

        The journal writes bracket the call: MODEL_PENDING before, so a death
        here says "a call may have been billed with no response" - and
        MODEL_RESULT before any tool runs, so a resume never asks the model
        for a fresh plan while the old plan's tools have already executed.

        A ModelError propagates. The loop does not retry (Session 8).
        """
        self.step += 1
        self._journal(MODEL_PENDING, step=self.step)

        response = model_fn(self.history, tools=tools)
        self.responses.append(response)
        self.ledger.record_response(response)

        message = assistant_message(response)
        self.history.append(message)
        self._journal(
            MODEL_RESULT,
            step=self.step,
            message=message,
            finish_reason=response.finish_reason,
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

            # The gate is per call, not per batch: a batch of three with one
            # call left in the budget runs exactly one.
            if stop_reason is None:
                stop_reason = check_before_tool_call(self.budget, self.ledger)

            if stop_reason is not None:
                self._refuse(call, stop_reason)
            else:
                self._dispatch(call)

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

    def _dispatch(self, call: dict[str, Any]) -> None:
        """Run one tool call and answer it."""
        function = call.get("function") or {}

        self._journal(
            TOOL_INTENT,
            step=self.step,
            call_id=call.get("id") or "",
            name=function.get("name"),
            arguments=function.get("arguments"),
        )
        # Counted before dispatch, not after: a tool that hangs or crashes
        # still consumed its slot. Counting on success is how a failing tool
        # gets retried forever inside its own budget.
        self.ledger.tool_calls += 1
        result = dispatch(
            function.get("name", ""), function.get("arguments", ""), root=self.root
        )
        self._answer(call, result)

    def _answer(
        self, call: dict[str, Any], result: ToolResult, **extra: Any
    ) -> None:
        """History, then journal, then the caller - in that order.

        Durable before the caller is told, so a crash inside `on_tool` still
        leaves a resumable journal.
        """
        message = tool_message(call, result)
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
        self._journal(
            RUN_FINISHED,
            stop_reason=stop_reason,
            replayed=replayed,
            steps=self.ledger.steps,
            totals=self.ledger.snapshot(),
            cost_usd=self.ledger.spend(self.budget.pricing),
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
        )

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
    )

    if resume is None:
        run._journal(
            RUN_STARTED,
            task=_task_of(run.history),
            messages=run.history,
            budget=budget.describe(),
        )
        return run

    run.history = list(resume.messages)
    run.step = resume.steps_done
    run.pending = list(resume.unanswered_calls)
    run.ledger.steps = resume.steps_done
    run.ledger.tool_calls = resume.tool_calls_done
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
    journal: Journal | None = None,
    resume: ResumeState | None = None,
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

    A ModelError propagates to the caller untouched.

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
    )

    # The answer was generated and paid for before the crash. Resuming it costs
    # nothing: no POST, no tokens. This is the whole point of the journal.
    if resume is not None and resume.answer_ready:
        return run.finish(STOP_ANSWER, text=resume.answer_text, replayed=True)

    while True:
        # Tool calls the dead process never answered, plus the normal path.
        # An empty batch is a no-op, which is what keeps this body flat.
        stop_reason = run.run_pending()
        # After the batch, never inside it. The history is complete and a
        # --resume can pick it up from here.
        if stop_reason is not None:
            return run.finish(stop_reason)

        # D35: before the POST. Covers the step limit, the clock, and anything
        # the next response would spend.
        stop_reason = check_before_step(run.budget, run.ledger)
        if stop_reason is not None:
            return run.finish(stop_reason)

        response = run.take_step(model_fn, tools)

        # No tools requested == the model considers itself done.
        if not response.tool_calls:
            return run.finish(STOP_ANSWER, text=response.text)

        run.pending = list(response.tool_calls)
