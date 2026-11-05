"""The agent execution loop. (Session 4+)

Owns: conversation history, the step counter, dispatching tool calls, and
matching each tool result to its tool-call ID.

Knows nothing about argparse, printing, or what list_files actually does. It
is handed a message list and a tool catalogue, and returns either a final
answer or an honest statement that it ran out of steps.

The model is INJECTED (model_fn). That is what makes the whole loop testable
with a scripted model - no API key, no network, no sockets. A loop you can
only test live is a loop you test twice and then stop testing.

Decisions locked here (see docs/session-04.md A):
  D26 the tool message carries the error code inside `content`
  D27 one step == one model call
  D28 the step limit is an explicit failure, never a quiet partial answer
  D30 history keeps everything; truncation happens in tools.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from agent.model import ModelResponse, call_model
from agent.tools import TOOL_SCHEMAS, ToolResult, dispatch

DEFAULT_MAX_STEPS = 5

STOP_ANSWER = "answer"
STOP_STEP_LIMIT = "step_limit"


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
    """The outcome of a whole run - not of one call."""

    text: str | None
    stop_reason: str
    steps: int
    messages: list[dict[str, Any]]
    responses: list[ModelResponse] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.stop_reason == STOP_ANSWER

    @property
    def prompt_tokens(self) -> int:
        return sum(r.prompt_tokens for r in self.responses)

    @property
    def completion_tokens(self) -> int:
        return sum(r.completion_tokens for r in self.responses)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.responses)

    @property
    def cache_hit_tokens(self) -> int:
        return sum(r.prompt_cache_hit_tokens for r in self.responses)

    @property
    def duration_ms(self) -> float:
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


def run_loop(
    messages: list[dict[str, Any]],
    *,
    root: Path,
    tools: list[dict[str, Any]] | None = TOOL_SCHEMAS,
    max_steps: int = DEFAULT_MAX_STEPS,
    model_fn: ModelFn = call_model,
    on_response=None,
    on_tool=None,
) -> LoopResult:
    """Call, execute, feed back, repeat - until an answer or the step cap.

    Returns on a model turn with no tool calls (the answer), or when max_steps
    model calls have been made (STOP_STEP_LIMIT). A ModelError propagates to
    the caller untouched: transport failure is not a loop concern.

    The caller's list is copied, not mutated.
    """
    history = list(messages)
    responses: list[ModelResponse] = []

    for step in range(1, max_steps + 1):
        response = model_fn(history, tools=tools)
        responses.append(response)
        history.append(assistant_message(response))
        if on_response is not None:
            on_response(step, response)

        # No tools requested == the model considers itself done. This is the
        # ONLY normal exit. finish_reason is advisory; this is authoritative.
        if not response.tool_calls:
            return LoopResult(
                text=response.text,
                stop_reason=STOP_ANSWER,
                steps=step,
                messages=history,
                responses=responses,
            )

        # Every call gets an answer. Skip one and the next request is a 400.
        for call in response.tool_calls:
            function = call.get("function") or {}
            result = dispatch(
                function.get("name", ""), function.get("arguments", ""), root=root
            )
            history.append(tool_message(call, result))
            if on_tool is not None:
                on_tool(step, call, result)

    return LoopResult(
        text=None,
        stop_reason=STOP_STEP_LIMIT,
        steps=max_steps,
        messages=history,
        responses=responses,
    )
