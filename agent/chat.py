"""An interactive session: one process, many questions, history that carries.

    python -m agent.chat
    python -m agent.chat --resume=<session_id>
    python -m agent.chat 2>/dev/null          # answers only, no tool trace

docs/interlude-repl.md, phases 0-5. This module owns every session concern and
`agent/` owns none of them: `run_loop`, `Budget`, `Ledger`, `Journal` and the
policy are used exactly as the one-shot CLI uses them, with no signature
changed and no behaviour added. That is D55 option D, and it is the reason the
eval runner's path through `cli.py` cannot be affected by anything in here.

The decisions this file implements, so they can be argued with:

    D53  a separate module, not a flag on cli.py. cli.py:main is what Session
         11's eval runner calls and it stays untouched.
    D54  limits split by NATURE, not by object. Steps, seconds and tool calls
         are per TURN - "how much may one question cost" - while tokens and
         spend are per SESSION, because that is the money. A session-wide
         clock would count the time you spend reading and typing, and kill a
         conversation that was never working.
    D55  no Ledger is threaded into run_loop. Each turn gets a Budget DERIVED
         from what the session has left (`Session.turn_budget`), so the
         existing in-loop gate enforces a session cap mid-turn without
         knowing a session exists.
    D56  one journal per SESSION, not per turn. Safe since Journal.heal(), and
         required for --resume.
    D57  per-turn totals from cli.report_totals, plus one session line. `:cost`
         for the breakdown.
    D58  a turn stopped by a limit KEEPS its partial exchange. Every
         tool_call_id in it was answered, so the history stays valid, and
         rolling back would discard tool results already paid for.
    D59  context is measured by the last turn's real prompt_tokens - not an
         estimate. Warn at 80%, refuse a new turn at the ceiling.
    D60  Ctrl-C abandons the turn and keeps the session.
    D61  commands are matched before anything reaches the model.
    D62  --resume restores the conversation and the session totals. It does
         NOT auto-finish an interrupted turn: run_loop's resume= rehydrates a
         ledger from the journal, which would double-count against a session
         total rebuilt from the same file. Ask again instead.

stdout carries answers, stderr carries the model's scratch work - the same
split as cli.py, which is what makes `2>/dev/null` a clean transcript.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from agent.budget import Budget, Pricing
from agent.cli import (
    RUNS_DIR,
    TOOLS_SYSTEM_PROMPT,
    explain_limit,
    print_response,
    print_tool,
    report_model_error,
    report_totals,
)
from agent.journal import Journal, JournalCorrupt, new_run_id, rebuild
from agent.loop import LoopResult, run_loop, tool_message
from agent.model import ModelConfig, ModelError, active_provider, call_model, context_warning
from agent.policy import workspace_root
from agent.tools import TOOL_SCHEMAS, ToolResult

try:  # Arrow-key history for input(), for one import. Absent on some platforms.
    import readline  # noqa: F401
except ImportError:  # pragma: no cover - platform dependent
    pass

PROMPT = "\n> "

# D59. A ceiling on the conversation, measured in the model's own count of our
# prompt. Lives here rather than in agent/ because it is a property of the
# model being talked to, and agent/ hard-codes nothing provider-specific -
# the same rule pricing follows. Override with MAX_CONTEXT_TOKENS.
DEFAULT_MAX_CONTEXT_TOKENS = 48_000
CONTEXT_WARN_FRACTION = 0.8

RESUME_FLAG = "--resume="
USAGE = (
    "usage: python -m agent.chat [--resume=<session_id>]\n"
    "\nSession limits come from .env: MAX_SESSION_TOKENS, MAX_SESSION_SPEND\n"
    "(needs PRICE_*), MAX_CONTEXT_TOKENS. Per-turn limits are the usual\n"
    "MAX_STEPS, MAX_TOOL_CALLS, MAX_RUN_SECONDS."
)
HELP = (
    ":quit      end the session\n"
    ":cost      tokens and spend so far\n"
    ":reset     forget the conversation, keep the budget already spent\n"
    ":history   how many messages the model is being sent\n"
    ":help      this"
)


def _env_int(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else None


def _env_float(name: str) -> float | None:
    raw = os.getenv(name)
    return float(raw) if raw and raw.strip() else None


@dataclass
class Session:
    """Everything that outlives one turn.

    Deliberately not a wrapper around run_loop: it holds state and derives a
    Budget. The loop is still called directly, with the same arguments cli.py
    uses, so there is one agent and not two.
    """

    root: Path
    turn_limits: Budget
    journal: Journal
    session_id: str
    max_session_tokens: int | None = None
    max_session_spend: float | None = None
    max_context_tokens: int | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    turns: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    tool_calls: int = 0
    # The model's own count of the last prompt we sent. A measurement, not an
    # estimate - which is why it is only known after a turn has run.
    context_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def spend(self) -> float | None:
        """Estimated USD for the whole session, or None when unpriced."""
        pricing = self.turn_limits.pricing
        if pricing is None:
            return None
        return pricing.cost(
            cache_hit=self.cache_hit_tokens,
            cache_miss=self.cache_miss_tokens,
            output=self.completion_tokens,
        )

    def record(self, result: LoopResult) -> None:
        """Fold one turn's ledger into the session's."""
        self.turns += 1
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        self.cache_hit_tokens += result.ledger.cache_hit_tokens
        self.cache_miss_tokens += result.ledger.cache_miss_tokens
        self.tool_calls += result.tool_calls
        if result.responses:
            self.context_tokens = result.responses[-1].prompt_tokens

    def turn_budget(self) -> Budget:
        """Per-turn limits, tightened by whatever the session has left (D55).

        The turn's own Ledger starts at zero, so handing it the session's
        REMAINING allowance makes the existing pre-POST gate enforce a session
        cap it knows nothing about. No new check, no changed signature.
        """
        max_tokens = self.turn_limits.max_total_tokens
        if self.max_session_tokens is not None:
            left = self.max_session_tokens - self.total_tokens
            max_tokens = left if max_tokens is None else min(max_tokens, left)

        max_spend = self.turn_limits.max_spend
        if self.max_session_spend is not None:
            left_usd = self.max_session_spend - (self.spend or 0.0)
            max_spend = left_usd if max_spend is None else min(max_spend, left_usd)

        return replace(
            self.turn_limits, max_total_tokens=max_tokens, max_spend=max_spend
        )

    def refusal(self) -> str | None:
        """Why the next turn must not start. Checked BEFORE a journal is
        written or a request is built, so a refused turn costs nothing and
        leaves no half-run in the trace."""
        if (
            self.max_session_tokens is not None
            and self.total_tokens >= self.max_session_tokens
        ):
            return (
                f"session token budget spent ({self.total_tokens}/"
                f"{self.max_session_tokens}). `:reset` does not refund it."
            )

        spent = self.spend
        if (
            self.max_session_spend is not None
            and spent is not None
            and spent >= self.max_session_spend
        ):
            return (
                f"session spend limit reached (${spent:.4f}/"
                f"${self.max_session_spend:.4f})."
            )

        if (
            self.max_context_tokens is not None
            and self.context_tokens >= self.max_context_tokens
        ):
            return (
                f"the conversation is too long to continue: the last prompt was "
                f"{self.context_tokens} tokens against a ceiling of "
                f"{self.max_context_tokens}. `:reset` starts a fresh "
                "conversation in this same session."
            )
        return None

    def describe(self) -> str:
        spent = self.spend
        money = "unpriced" if spent is None else f"${spent:.4f}"
        parts = [
            f"turns={self.turns}",
            f"tokens={self.total_tokens}",
            f"(in={self.prompt_tokens} hit={self.cache_hit_tokens} "
            f"out={self.completion_tokens})",
            f"tools={self.tool_calls}",
            f"spend={money}",
        ]
        if self.max_session_tokens is not None:
            parts.append(f"token_budget<={self.max_session_tokens}")
        if self.max_session_spend is not None:
            parts.append(f"spend_budget<=${self.max_session_spend:.4f}")
        if self.context_tokens:
            parts.append(f"context={self.context_tokens}")
        return " ".join(parts)


def answer_unfinished_calls(messages: list[dict[str, Any]]) -> int:
    """Close any tool calls a dead process never answered.

    A provider rejects a request whose last assistant turn has a tool_call
    with no matching tool message - a 400 about something unrelated, one step
    removed from the cause. A restored conversation can end exactly that way,
    so every orphan is answered here with the truth: it never ran.
    """
    last_calls: list[dict[str, Any]] = []
    answered: set[str] = set()

    for message in messages:
        if message.get("role") == "assistant":
            last_calls = list(message.get("tool_calls") or [])
            answered = set()
        elif message.get("role") == "tool":
            answered.add(message.get("tool_call_id", ""))

    orphans = [c for c in last_calls if (c.get("id") or "") not in answered]
    for call in orphans:
        messages.append(
            tool_message(
                call,
                ToolResult(
                    ok=False,
                    content=(
                        "Not run: the session ended before this call was made. "
                        "No file was read or changed by it."
                    ),
                    error_code="interrupted",
                ),
            )
        )
    return len(orphans)


def start_session(
    turn_limits: Budget, root: Path, resume_id: str | None
) -> Session | None:
    """A fresh session, or one restored from a journal. None on a bad resume."""
    max_context = _env_int("MAX_CONTEXT_TOKENS") or DEFAULT_MAX_CONTEXT_TOKENS
    common = {
        "root": root,
        "turn_limits": turn_limits,
        "max_session_tokens": _env_int("MAX_SESSION_TOKENS"),
        "max_session_spend": _env_float("MAX_SESSION_SPEND"),
        "max_context_tokens": max_context,
    }

    if resume_id is None:
        session_id = new_run_id()
        return Session(
            journal=Journal.create(RUNS_DIR, session_id),
            session_id=session_id,
            history=[{"role": "system", "content": TOOLS_SYSTEM_PROMPT}],
            **common,
        )

    try:
        journal = Journal.open(RUNS_DIR, resume_id)
    except FileNotFoundError as err:
        print(f"error [no_journal]: {err}", file=sys.stderr)
        return None

    print(f"resuming session {resume_id}", file=sys.stderr)
    discarded = journal.heal()
    if discarded:
        print(
            f"  repaired: dropped {discarded} bytes of a torn final record",
            file=sys.stderr,
        )

    try:
        state = rebuild(journal.events())
    except JournalCorrupt as err:
        print(f"error [journal_corrupt]: {err}", file=sys.stderr)
        return None

    messages = list(state.messages)
    orphans = answer_unfinished_calls(messages)
    if orphans:
        print(
            f"  the last turn was cut off with {orphans} tool call(s) unanswered; "
            "each is closed as 'not run' so the conversation stays valid",
            file=sys.stderr,
        )

    session = Session(
        journal=journal, session_id=resume_id, history=messages, **common
    )
    # Every model_result in the file, which for a session journal is exactly
    # the session total. D62: nothing is replayed, so nothing is re-billed.
    session.prompt_tokens = state.prompt_tokens
    session.completion_tokens = state.completion_tokens
    session.cache_hit_tokens = state.cache_hit_tokens
    session.cache_miss_tokens = state.cache_miss_tokens
    session.tool_calls = state.tool_calls_done
    session.turns = state.steps_done

    print(f"  restored {len(messages)} messages", file=sys.stderr)
    print(f"  {session.describe()}", file=sys.stderr)
    return session


def run_command(command: str, session: Session) -> bool:
    """Handle an in-band command. Returns False to end the session.

    Matched before anything is appended to the history, so a command can never
    reach the model (D61).
    """
    if command in (":quit", ":q", ":exit"):
        return False

    if command == ":cost":
        print(session.describe(), file=sys.stderr)
    elif command == ":history":
        print(
            f"{len(session.history)} messages; last prompt was "
            f"{session.context_tokens or 'not yet measured'} tokens",
            file=sys.stderr,
        )
    elif command == ":reset":
        session.history = [{"role": "system", "content": TOOLS_SYSTEM_PROMPT}]
        session.context_tokens = 0
        # Totals are NOT cleared: money already spent is still spent, and a
        # command that silently refills a budget is a budget with a loophole.
        print(
            "conversation cleared. Budget already spent is unchanged: "
            f"{session.describe()}",
            file=sys.stderr,
        )
    elif command == ":help":
        print(HELP, file=sys.stderr)
    else:
        print(f"unknown command {command}. :help for the list.", file=sys.stderr)
    return True


def take_turn(session: Session, task: str, model_fn: Callable) -> None:
    """One question, start to finish. Never raises."""
    # The turn is a CANDIDATE: history is only replaced once run_loop returns.
    # A ModelError or a Ctrl-C therefore leaves the conversation exactly as it
    # was, instead of stranding a user message nothing ever answered.
    turn = [*session.history, {"role": "user", "content": task}]

    try:
        result = run_loop(
            turn,
            root=session.root,
            tools=TOOL_SCHEMAS,
            budget=session.turn_budget(),
            model_fn=model_fn,
            on_response=print_response,
            on_tool=print_tool,
            journal=session.journal,
        )
    except ModelError as err:
        report_model_error(err)
        print("(turn discarded; the conversation is unchanged)", file=sys.stderr)
        return
    except KeyboardInterrupt:  # D60
        print(
            "\n(interrupted; turn discarded, session intact. The journal holds "
            "the partial run.)",
            file=sys.stderr,
        )
        return

    report_totals(result)
    session.record(result)
    session.history = result.messages  # D58: the partial turn is kept
    print(f"[session] {session.describe()}", file=sys.stderr)

    if (
        session.max_context_tokens is not None
        and session.context_tokens
        >= session.max_context_tokens * CONTEXT_WARN_FRACTION
    ):
        print(
            f"warning: context is {session.context_tokens}/"
            f"{session.max_context_tokens} tokens. `:reset` when you change "
            "subject - every turn re-sends all of it.",
            file=sys.stderr,
        )

    if result.stopped_by_limit:
        # cli.py's D28 rule: a partial answer never reaches stdout.
        print(
            f"incomplete [{result.stop_reason}]: {explain_limit(result)}.",
            file=sys.stderr,
        )
        return

    if result.text is not None:
        print(result.text)


def main(
    argv: list[str] | None = None,
    *,
    model_fn: Callable = call_model,
    read_line: Callable[[str], str] = input,
) -> int:
    """`model_fn` and `read_line` are injected so the whole session is
    testable with no key and no network - the same seam run_loop offers."""
    load_dotenv()

    resume_id: str | None = None
    for argument in (argv or [])[1:]:
        if argument.startswith(RESUME_FLAG):
            resume_id = argument[len(RESUME_FLAG) :]
        else:
            print(USAGE, file=sys.stderr)
            return 2

    try:
        provider = active_provider()
    except ModelError as err:
        return report_model_error(err)
    pricing = Pricing.free() if provider.free else Pricing.from_env()

    turn_limits = Budget.from_env(pricing=pricing)
    # D38: a spend limit with no price list is a number on screen that nothing
    # enforces. True of the session cap for exactly the same reason.
    if turn_limits.needs_pricing or (
        _env_float("MAX_SESSION_SPEND") is not None and turn_limits.pricing is None
    ):
        print(
            "error [usage]: a spend limit needs prices. Set "
            "PRICE_INPUT_CACHE_HIT_PER_M, PRICE_INPUT_CACHE_MISS_PER_M and "
            "PRICE_OUTPUT_PER_M in .env.",
            file=sys.stderr,
        )
        return 2

    session = start_session(turn_limits, workspace_root(), resume_id)
    if session is None:
        return 2

    print(f"session {session.session_id}  (resume with --resume={session.session_id})",
          file=sys.stderr)
    print(f"provider:   {provider.name} ({os.getenv(provider.name.upper() + '_MODEL') or provider.model})",
          file=sys.stderr)
    print(f"workspace:  {session.root}", file=sys.stderr)
    print(f"per turn:   {turn_limits.describe()}", file=sys.stderr)
    print(f"per session: tokens<={session.max_session_tokens or 'unlimited'} "
          f"spend<={session.max_session_spend or 'unlimited'} "
          f"context<={session.max_context_tokens}", file=sys.stderr)
    # Found by writing the tests, not by reasoning about it: check_before_step
    # compares tokens-so-far PLUS the reserve against the cap, so a session
    # token budget at or below the reserve refuses every turn before a single
    # request - correctly, but silently, and it reads as "nothing happens".
    if (
        session.max_session_tokens is not None
        and session.max_session_tokens <= turn_limits.token_reserve
    ):
        print(
            f"warning: MAX_SESSION_TOKENS ({session.max_session_tokens}) is not "
            f"above TOKEN_RESERVE ({turn_limits.token_reserve}), so every turn "
            "will stop before calling the model. Raise one or lower the other.",
            file=sys.stderr,
        )

    try:
        warning = context_warning(ModelConfig.from_env())
    except ModelError:
        warning = None
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

    print(":help for commands, :quit or Ctrl-D to end.", file=sys.stderr)

    while True:
        try:
            task = read_line(PROMPT).strip()
        except EOFError:
            print(file=sys.stderr)
            break
        except KeyboardInterrupt:
            print("\n(:quit or Ctrl-D to end)", file=sys.stderr)
            continue

        if not task:
            continue

        if task.startswith(":"):
            if not run_command(task, session):
                break
            continue

        refusal = session.refusal()
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            continue

        take_turn(session, task, model_fn)

    print(f"[session {session.session_id}] {session.describe()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
