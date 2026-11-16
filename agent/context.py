"""How big the conversation is, and how wrong that number is. (Session 9)

Owns one estimate and its error bar. No I/O, no provider, no loop - it is
arithmetic over a message list, which is what makes it testable without a key
and comparable between providers.

D85, and the decision is not "which measure is accurate" because neither is:

    prompt_tokens   EXACT, free, provider-native - and one step stale. It
                    describes the history BEFORE the turn you are about to
                    send, and it does not exist at all before the first
                    response. The step that overflows is the one it has no
                    number for.
    characters / 4  fresh, available before the first POST, and wrong by a
                    factor that moves with the content. Code is denser than
                    prose; JSON tool arguments are denser than code.

This module computes the second and CHECKS IT AGAINST the first every time a
response arrives. The ratio it keeps is not decoration - it is the error bar
on every context decision the run makes, and a run whose ratio drifts far
from 1.0 is a run whose context limit is a different number than the user
set. Session 12 can read it; Session 10 has to decide whether to journal it.

What is deliberately NOT here: summarisation and compaction. The plan defers
them, and this project can now say why precisely - a summary that drops a
`tool_call` id produces a history the provider rejects, and a summary that
drops a file's exact bytes produces an `old` that no longer matches. Session
12's report is what should decide whether that risk is worth taking.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# 3.5, not the usual rule-of-thumb 4, and the difference is a measurement
# rather than a preference. Two real DeepSeek requests from
# runs/20261116T102728-d4263f read 0.90 and 0.91 against the provider's own
# prompt_tokens at 4 chars/token - a consistent 10% UNDER-read, which is the
# dangerous direction: the check passes and the provider disagrees. At 3.5
# the same two requests read 1.02 and 1.04, which errs toward stopping one
# step early.
#
# This is a calibrated CONSTANT, not the dynamic correction `calibrate`
# refuses to apply: it was chosen once from recorded traces, it is written
# down here, and `ContextSize.ratio` keeps reporting the live figure so the
# next person can see it drift. An agent reading mostly source code will
# tokenise denser than this; an agent reading prose, looser.
CHARS_PER_TOKEN = 3.5

# What one message costs beyond its text: role, delimiters, the envelope the
# provider wraps every turn in. A small constant rather than a modelled
# format, because the formats differ per provider and the difference is
# smaller than the error already in CHARS_PER_TOKEN.
TOKENS_PER_MESSAGE = 4


def message_chars(message: dict[str, Any]) -> int:
    """Every character the provider will be sent for one message.

    `content` is the obvious half. The other half is what an agent's history
    is mostly made of by step four: `tool_calls` - the function names and the
    JSON argument strings the model emitted - and the `tool_call_id` each
    answer carries. Counting content alone under-reads an agent transcript by
    exactly the part that makes it an agent transcript.
    """
    total = 0
    content = message.get("content")
    if isinstance(content, str):
        total += len(content)

    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(str(function.get("name") or ""))
        total += len(str(function.get("arguments") or ""))
        total += len(str(call.get("id") or ""))

    total += len(str(message.get("tool_call_id") or ""))
    total += len(str(message.get("role") or ""))
    return total


def tool_chars(tools: list[dict[str, Any]] | None) -> int:
    """What the tool definitions cost, serialised the way they are sent.

    Found the hard way, and the reason this function exists at all: the first
    version of this module measured the HISTORY and nothing else, and a real
    run reported ~473 estimated tokens against the provider's 1161. The tool
    schemas are sent with every single request and are not in `messages`,
    so leaving them out under-read the prompt by a factor of 2.5 - in the
    dangerous direction, where the check passes and the provider disagrees.
    """
    if not tools:
        return 0
    return len(json.dumps(tools, ensure_ascii=False))


def estimate_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
) -> int:
    """A local, fresh, deliberately rough token count for one request.

    One REQUEST, not one history: whatever travels on the wire counts, and
    the tool catalogue travels on every one of them.
    """
    chars = sum(message_chars(message) for message in messages)
    chars += tool_chars(tools)
    return int(chars / CHARS_PER_TOKEN) + TOKENS_PER_MESSAGE * len(messages)


@dataclass(frozen=True)
class ContextSize:
    """One measurement of the conversation, with the window it sits in."""

    messages: int
    chars: int
    estimated_tokens: int
    window: int | None
    # Part of `chars`, kept separately because it is the fixed overhead every
    # request pays and a run whose history is small is mostly this.
    tool_chars: int = 0
    # The provider's exact count for the PREVIOUS request, when there was
    # one. Kept beside the estimate rather than instead of it, because the
    # pair is the finding and either one alone is a claim.
    last_prompt_tokens: int | None = None

    @property
    def fraction(self) -> float | None:
        """How full the window is, as the estimate sees it."""
        if not self.window:
            return None
        return self.estimated_tokens / self.window

    @property
    def ratio(self) -> float | None:
        """estimate / exact, for the last request that has both.

        1.0 means the estimator was right. Below 1.0 means it UNDER-counts,
        which is the dangerous direction: the check passes and the provider
        disagrees.
        """
        if not self.last_prompt_tokens:
            return None
        return self.estimated_tokens / self.last_prompt_tokens

    def line(self) -> str:
        """One line for a human watching a run fill up its context."""
        parts = [f"~{self.estimated_tokens} tokens", f"{self.messages} msgs"]
        if self.window:
            parts.append(f"{(self.fraction or 0) * 100:.0f}% of {self.window}")
        if self.ratio is not None:
            parts.append(f"est/actual {self.ratio:.2f}")
        return "context " + ", ".join(parts)

    @property
    def loud(self) -> bool:
        """Past half the window - the point a human should start noticing."""
        fraction = self.fraction
        return fraction is not None and fraction >= 0.5


def measure(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    window: int | None = None,
    last_prompt_tokens: int | None = None,
) -> ContextSize:
    """Size one request. The only entry point the loop uses."""
    schema_chars = tool_chars(tools)
    return ContextSize(
        messages=len(messages),
        chars=sum(message_chars(message) for message in messages) + schema_chars,
        estimated_tokens=estimate_tokens(messages, tools),
        window=window,
        last_prompt_tokens=last_prompt_tokens,
        tool_chars=schema_chars,
    )


def calibrate(estimated: int, actual: int) -> float | None:
    """estimate / exact for one request, or None when there is no exact.

    Reported, never applied. Scaling the estimate by a ratio measured on the
    previous request is a correction that looks like accuracy and is actually
    a second estimator with no error bar of its own.
    """
    if not actual:
        return None
    return estimated / actual


def json_chars(value: Any) -> int:
    """Characters a value costs once serialised. For tests and tools."""
    return len(json.dumps(value, ensure_ascii=False))
