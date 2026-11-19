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

from agent.config import load_config

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
from agent.storage import workspace_root, recorded_workspace
from agent.retry import RetryPolicy
from agent.tools import TOOL_SCHEMAS, ToolResult
from agent.memory import read_memory, memory_message, approve_user_fact
from agent.tracing import load as load_journal, segments, timeline

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
CONTINUE_FLAG = "--continue"
USAGE = (
    "usage: mini-agent-chat [--resume=<session_id> | --continue] [--workspace=DIR]\n"
    "\nSession limits come from .env: MAX_SESSION_TOKENS, MAX_SESSION_SPEND\n"
    "(needs PRICE_*), MAX_CONTEXT_TOKENS. Per-turn limits are the usual\n"
    "MAX_STEPS, MAX_TOOL_CALLS, MAX_RUN_SECONDS."
)
# D61 said commands are matched before anything reaches the model; it did not
# say what marks one. `:` still does. `/` is added because it is what a person
# who has used Claude Code or Codex reaches for, and because it is the key
# that opens the menu - see `_looks_like_command` for the ambiguity it costs.
COMMAND_PREFIXES = ("/", ":")


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
    # Only for display, and only so `:status` can stand alone.
    model_label: str = ""
    # Session 8's retry policy, which this REPL did not have until now: it
    # called run_loop without `retry=` and therefore ran at RetryPolicy.off(),
    # one attempt per request. The one-shot CLI has read MAX_ATTEMPTS from
    # .env since Session 8, so the same 500 was retried there and fatal here -
    # a difference nobody chose, in the entry point where it hurts most,
    # because a REPL turn that dies to one blip loses the user's place in a
    # conversation rather than one command.
    #
    # D69 still holds across the whole turn: `writes_done` is the loop's own
    # record of whether a tool in THIS turn has edited anything, and a request
    # that may already have arrived is not resent once one has.
    #
    # The default stays `off` so that constructing a Session in a test gets
    # the old behaviour; main() passes RetryPolicy.from_env().
    retry: RetryPolicy = field(default_factory=RetryPolicy.off)
    history: list[dict[str, Any]] = field(default_factory=list)
    memory_proposal: str | None = None

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
    turn_limits: Budget,
    root: Path,
    resume_id: str | None,
    retry: RetryPolicy | None = None,
) -> Session | None:
    """A fresh session, or one restored from a journal. None on a bad resume."""
    max_context = _env_int("MAX_CONTEXT_TOKENS") or DEFAULT_MAX_CONTEXT_TOKENS
    common = {
        "root": root,
        "turn_limits": turn_limits,
        "retry": retry or RetryPolicy.off(),
        "max_session_tokens": _env_int("MAX_SESSION_TOKENS"),
        "max_session_spend": _env_float("MAX_SESSION_SPEND"),
        "max_context_tokens": max_context,
    }

    if resume_id is None:
        session_id = new_run_id()
        text, digest, path = read_memory(root)
        print(f"memory: {path} (sha256 {digest})", file=sys.stderr)
        history = [{"role": "system", "content": TOOLS_SYSTEM_PROMPT}]
        if text:
            history.append(memory_message(text))
        return Session(
            journal=Journal.create(RUNS_DIR, session_id),
            session_id=session_id,
            history=history,
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
        events = journal.events()
        starts = [e for e in events if e.get("t") == "run_started"]
        if starts and starts[0].get("workspace") and Path(starts[0]["workspace"]).resolve() != root.resolve():
            print("error [workspace]: journal belongs to a different workspace", file=sys.stderr)
            return None
        state = rebuild(events)
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
    # D107: turns are RUNS in this journal, not steps. `state.steps_done`
    # counts model_result events, which is the right number for run_loop's
    # own resume - one run, one step counter - and the wrong one here, where
    # D56 puts many runs in one file. A three-step answer to one question
    # came back as `turns=3`, and `/status` now prints that beside a journal
    # line saying "1 turn(s) recorded", so the file disagreed with itself.
    session.turns = len(segments(load_journal(journal.path)))

    print(f"  restored {len(messages)} messages", file=sys.stderr)
    print(f"  {session.describe()}", file=sys.stderr)
    return session


# --------------------------------------------------------------------------
# Commands. One table, three consumers (D101): the dispatcher below, `:help`,
# and the completion menu. The previous shape kept a hand-written HELP string
# beside an if/elif chain, which is two places to edit and one to forget - and
# a command that works but is undocumented is a command nobody runs.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """`handler` returns False to end the session, as `run_command` always has."""

    name: str
    summary: str
    handler: Callable[[Session], bool] | Callable[[Session, str], bool]
    aliases: tuple[str, ...] = ()
    takes_argument: bool = False


def _context_line(session: Session) -> str:
    """D59's measurement, said in a way that distinguishes 0 from unknown."""
    measured = session.context_tokens
    ceiling = session.max_context_tokens
    if not measured:
        return f"not yet measured (ceiling {ceiling or 'none'})"
    if not ceiling:
        return f"{measured} tokens (no ceiling)"
    return f"{measured}/{ceiling} tokens ({measured / ceiling:.0%})"


def _cmd_status(session: Session) -> bool:
    """Where am I - as opposed to `:cost`, which is what has this cost.

    D102: the two are kept apart because they answer different questions at
    different moments. `:cost` is a number you check against a budget.
    `:status` is what you run when the banner has scrolled off the top and you
    need the session id to `--resume`, the journal path `:trace` reads, or an
    answer to "why was my last turn refused". `refusal()` is called here
    rather than re-derived, so this line cannot disagree with the gate.
    """
    trace = load_journal(session.journal.path)
    recorded = len(segments(trace)) if trace.events else 0
    spent = session.spend
    rows = [
        f"session    {session.session_id}  (resume with --resume={session.session_id})",
        f"model      {session.model_label or 'unknown'}",
        f"workspace  {session.root}",
        f"journal    {session.journal.path}",
        f"           {len(trace.events)} events, {recorded} turn(s) recorded"
        + (f", {trace.damaged} damaged line(s)" if trace.damaged else ""),
        f"turns      {session.turns}  tools={session.tool_calls}",
        f"tokens     {session.total_tokens} (in={session.prompt_tokens} "
        f"hit={session.cache_hit_tokens} out={session.completion_tokens})"
        + (f"  of {session.max_session_tokens}" if session.max_session_tokens else ""),
        f"spend      {'unpriced' if spent is None else f'${spent:.4f}'}"
        + (
            f"  of ${session.max_session_spend:.4f}"
            if session.max_session_spend is not None
            else ""
        ),
        f"context    {_context_line(session)}",
        f"per turn   {session.turn_limits.describe()}",
        f"retry      {session.retry.describe()}",
        f"next turn  {session.refusal() or 'ok'}",
    ]
    print("\n".join(rows), file=sys.stderr)
    return True


TRACE_USAGE = "usage: /trace [all|<n>] [full]"


def _cmd_trace(session: Session, argument: str = "") -> bool:
    """The journal, read back, without leaving the session.

    D103: this is `tracing.py` pointed at the file this very session is still
    appending to, and that is safe for one reason - every append is flushed
    and fsynced BEFORE the side effect it authorises, so what is on disk
    mid-session is a complete account of everything that has already happened.
    The reader writes nothing (D89), so running it against a live journal
    cannot damage the session it is describing.

    The default is the LAST turn rather than the session, because a trace you
    have to scroll is a trace you stop reading. `all` and `<n>` exist for when
    you are actually hunting something.

    `full` is the D92 opt-in: it prints tool results verbatim, which means the
    contents of every file this session has read.
    """
    words = argument.split()
    full = any(word in ("full", "--full") for word in words)
    rest = [word for word in words if word not in ("full", "--full")]

    trace = load_journal(session.journal.path)
    if not trace.events:
        print(
            f"nothing recorded yet: {session.journal.path} is empty. The first "
            "record is written before the first request leaves.",
            file=sys.stderr,
        )
        return True

    turns = segments(trace)
    if not rest:
        chosen = turns[-1:]
    elif rest[0] in ("all", "*"):
        chosen = turns
    elif rest[0].isdigit() and int(rest[0]) > 0:
        chosen = turns[-int(rest[0]) :]
    else:
        print(f"{TRACE_USAGE}  (did not understand {rest[0]!r})", file=sys.stderr)
        return True

    first = len(turns) - len(chosen) + 1
    print(
        f"# {session.session_id}  turn {first}-{len(turns)} of {len(turns)}, "
        f"tool output {'VERBATIM - do not paste this anywhere' if full else 'bounded'}",
        file=sys.stderr,
    )
    for turn in chosen:
        print("\n".join(timeline(turn, full=full)), file=sys.stderr)
    return True


def _cmd_cost(session: Session) -> bool:
    print(session.describe(), file=sys.stderr)
    return True


def _cmd_history(session: Session) -> bool:
    print(
        f"{len(session.history)} messages; last prompt was "
        f"{session.context_tokens or 'not yet measured'} tokens",
        file=sys.stderr,
    )
    return True


def _cmd_reset(session: Session) -> bool:
    # Preserve the snapshot read at session start when resetting conversation.
    session.history = [m for m in session.history[:2] if m.get("role") == "system"]
    session.context_tokens = 0
    # Totals are NOT cleared: money already spent is still spent, and a
    # command that silently refills a budget is a budget with a loophole.
    print(
        "conversation cleared. Budget already spent is unchanged: "
        f"{session.describe()}",
        file=sys.stderr,
    )
    return True


def _cmd_help(session: Session) -> bool:
    print(help_text(), file=sys.stderr)
    return True


def _cmd_quit(session: Session) -> bool:
    return False


def _cmd_remember(session: Session, argument: str = "") -> bool:
    if not argument or not any(m.get("role") == "user" and m.get("content") == argument
                               for m in session.history):
        print("Proposal refused: provide an exact single-line statement you previously sent; file and model text are not eligible.", file=sys.stderr)
        session.memory_proposal = None
        return True
    session.memory_proposal = argument
    print(f"Proposed memory (source: your message): {argument}\nType /approve-memory to save it, or /cancel-memory to discard it.", file=sys.stderr)
    return True


def _cmd_approve_memory(session: Session) -> bool:
    fact = session.memory_proposal
    session.memory_proposal = None
    if fact is None:
        print("No memory proposal to approve.", file=sys.stderr)
        return True
    try:
        path = approve_user_fact(session.root, fact, fact)
        print(f"Saved to {path}; takes effect in a new session.", file=sys.stderr)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"Memory not saved: {exc}", file=sys.stderr)
    return True


def _cmd_cancel_memory(session: Session) -> bool:
    session.memory_proposal = None
    print("Memory proposal discarded.", file=sys.stderr)
    return True


COMMANDS: tuple[Command, ...] = (
    Command("status", "current session metrics", _cmd_status),
    Command("trace", "execution timeline   [all|<n>] [full]", _cmd_trace, takes_argument=True),
    Command("cost", "token usage and estimated cost", _cmd_cost),
    Command("history", "how many messages the model is being sent", _cmd_history),
    Command("remember", "propose an exact user statement", _cmd_remember, takes_argument=True),
    Command("approve-memory", "save the current proposal", _cmd_approve_memory),
    Command("cancel-memory", "discard the current proposal", _cmd_cancel_memory),
    Command("reset", "forget the conversation, keep the budget spent", _cmd_reset),
    Command("help", "this", _cmd_help),
    Command("quit", "end the session", _cmd_quit, aliases=("q", "exit")),
)

BY_NAME: dict[str, Command] = {
    alias: command for command in COMMANDS for alias in (command.name, *command.aliases)
}


def help_text() -> str:
    width = max(len(command.name) for command in COMMANDS)
    rows = [f"  /{command.name:<{width}}  {command.summary}" for command in COMMANDS]
    return "\n".join(["commands (/ or : both work):", *rows])


def _looks_like_command(task: str) -> bool:
    """`/status` is a command. `/Users/sifat/notes.md` is a question.

    D104: `:` stays unambiguous - anything starting with it is a command, and
    an unknown one is reported rather than sent, which is the behaviour the
    tests have always asserted. `/` cannot be treated that way, because a
    POSIX path starts with one. Answering "unknown command" to someone who
    asked about a file would read as a REFUSAL, which is the worst available
    failure, so the `/` rule is about shape rather than membership: one word,
    letters and dashes only. A path fails it at the second slash and goes to
    the model as the question it is.
    """
    if not task or task[0] not in COMMAND_PREFIXES:
        return False
    word = task[1:].split(" ", 1)[0]
    if not word:
        # D106: a bare `/` is "what can I type here". In a terminal the menu
        # answers it before Enter is ever pressed, but the menu is the layer
        # that can be absent (D105), and sending `/` to the model is a billed
        # request whose answer is nothing. The prefix alone prints the list.
        return True
    if task[0] == ":":
        return True
    return all(char.isalpha() or char == "-" for char in word)


def run_command(command: str, session: Session) -> bool:
    """Handle an in-band command. Returns False to end the session.

    Matched before anything is appended to the history, so a command can never
    reach the model (D61).
    """
    head, _, argument = command.partition(" ")
    name = head[1:].lower()
    if not name:
        print(help_text(), file=sys.stderr)
        return True

    entry = BY_NAME.get(name)
    if entry is None:
        print(f"unknown command {head}.", file=sys.stderr)
        print(help_text(), file=sys.stderr)
        return True

    argument = argument.strip()
    if argument and not entry.takes_argument:
        print(
            f"{head} takes no arguments (ignoring {argument!r})",
            file=sys.stderr,
        )
    if entry.takes_argument:
        return entry.handler(session, argument)
    return entry.handler(session)


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
            retry=session.retry,
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


def _parse_resume_flag(argv: list[str] | None) -> tuple[str | None, str | None, bool]:
    """(resume_id, workspace, ok)."""
    resume_id: str | None = None
    workspace: str | None = None
    for argument in (argv or [])[1:]:
        if argument.startswith("--workspace="):
            if workspace is not None or not argument[len("--workspace="):]:
                return None, None, False
            workspace = argument[len("--workspace="):]
            continue
        if argument == CONTINUE_FLAG:
            if resume_id is not None:
                return None, None, False
            resume_id = CONTINUE_FLAG
            continue
        if not argument.startswith(RESUME_FLAG):
            return None, None, False
        if resume_id is not None:
            return None, None, False
        resume_id = argument[len(RESUME_FLAG) :]
    return resume_id, workspace, True


def latest_session(root: Path) -> str | None:
    """Choose the most recently started journal for this workspace."""
    candidates = []
    for path in RUNS_DIR.glob("*.jsonl"):
        try:
            events = Journal(path).events()
            starts = [e for e in events if e.get("t") == "run_started"
                      and e.get("workspace") == str(root.resolve())]
            if starts:
                candidates.append((starts[0].get("at", 0), path.stem))
        except (OSError, JournalCorrupt, ValueError):
            continue
    return max(candidates)[1] if candidates else None


def _spend_limit_without_prices(turn_limits: Budget) -> bool:
    """D38: a spend limit with no price list is a number on screen that
    nothing enforces. True of the session cap for exactly the same reason."""
    return turn_limits.needs_pricing or (
        _env_float("MAX_SESSION_SPEND") is not None and turn_limits.pricing is None
    )


def model_label(provider) -> str:
    """`.env` wins over the provider default - the banner and `:status` must
    not be able to disagree about which model is answering."""
    model = os.getenv(provider.name.upper() + "_MODEL") or provider.model
    return f"{provider.name} ({model})"


def _print_banner(session: Session, provider, turn_limits: Budget) -> None:
    """Everything printed once, before the first prompt."""
    print(
        f"session {session.session_id}  (resume with --resume={session.session_id})",
        file=sys.stderr,
    )
    print(f"provider:   {session.model_label}", file=sys.stderr)
    print(f"workspace:  {session.root}", file=sys.stderr)
    print(f"per turn:   {turn_limits.describe()}", file=sys.stderr)
    print(f"retry:      {session.retry.describe()}", file=sys.stderr)
    print(
        f"per session: tokens<={session.max_session_tokens or 'unlimited'} "
        f"spend<={session.max_session_spend or 'unlimited'} "
        f"context<={session.max_context_tokens}",
        file=sys.stderr,
    )

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

    print(file=sys.stderr)
    print(help_text(), file=sys.stderr)
    print("type / for the menu. /quit or Ctrl-D to end.", file=sys.stderr)


def _command_completer():
    """Candidates for the live menu. Imports inside, so the module still
    loads where prompt_toolkit is absent."""
    from prompt_toolkit.completion import Completer, Completion

    class CommandCompleter(Completer):
        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            # Only the first word, and only a prefixed one: once a space is
            # typed the line is an argument (`/trace all`), and a question
            # must never have a menu opened over it.
            if not text or text[0] not in COMMAND_PREFIXES or " " in text:
                return
            prefix, typed = text[0], text[1:].lower()
            for command in COMMANDS:
                if command.name.startswith(typed):
                    yield Completion(
                        prefix + command.name,
                        start_position=-len(text),
                        display=prefix + command.name,
                        display_meta=command.summary,
                    )

    return CommandCompleter()


def prompt_widget(**overrides: Any):
    """The configured prompt. `overrides` exists for the tests, which pass a
    pipe and a dummy terminal - the point being that what they exercise is
    this configuration and not a second one written beside it."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    settings: dict[str, Any] = {
        "completer": _command_completer(),
        # The whole request: the menu appears as `/` is typed, and narrows
        # with every further character. Tab or the arrow keys pick.
        "complete_while_typing": True,
        "history": InMemoryHistory(),
        "reserve_space_for_menu": len(COMMANDS) + 1,
    }
    settings.update(overrides)
    return PromptSession(**settings)


def make_read_line() -> Callable[[str], str]:
    """A prompt with a live command menu, or plain `input()` when that cannot work.

    D105: gated on BOTH streams being a terminal, not just stdin.
    prompt_toolkit draws its menu by moving the cursor around stdout, and
    `python -m agent.chat > transcript` is a documented way to run this
    module. Rendering a dropdown into that file would corrupt the one stream
    this module promises is clean, so the fallback is the old `input()`, which
    still has arrow-key history from the readline import above.

    The history is in-memory on purpose: `input()` + readline never persisted
    a history file either, and quietly starting to write the user's questions
    to disk is not a change that belongs in a completion feature.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return input
    try:
        prompt_session = prompt_widget()
    except Exception as err:  # pragma: no cover - terminal dependent
        # Deliberately broad. prompt_toolkit may be absent, or present and
        # unable to take this particular terminal. Either way the session
        # must still start: a REPL that refuses to open because its
        # autocomplete would not render is worse than one without a menu.
        # Said out loud rather than swallowed, so it is not a silent downgrade.
        print(
            f"(no command menu: {type(err).__name__}: {err}. "
            "Type / or /help for the list.)",
            file=sys.stderr,
        )
        return input

    def read_line(prompt: str) -> str:
        # PROMPT leads with a blank line for spacing. prompt_toolkit would
        # treat it as part of a multi-line prompt and redraw it on every
        # keystroke, so it is written out here and kept out of the widget.
        blanks = len(prompt) - len(prompt.lstrip("\n"))
        if blanks:
            sys.stderr.write("\n" * blanks)
            sys.stderr.flush()
        # Ctrl-C raises KeyboardInterrupt and Ctrl-D raises EOFError, exactly
        # as input() does, so _read_task's D60 handling is unchanged.
        return prompt_session.prompt(prompt.lstrip("\n"))

    return read_line


def _read_task(read_line: Callable[[str], str]) -> str | None:
    """One line from the user, or None to end the session.

    Ctrl-C returns "" (keep going), Ctrl-D and EOF return None (stop). Those
    two have to be told apart, which is the only reason this is a function
    and not an expression.
    """
    try:
        return read_line(PROMPT).strip()
    except EOFError:
        print(file=sys.stderr)
        return None
    except KeyboardInterrupt:
        print("\n(:quit or Ctrl-D to end)", file=sys.stderr)
        return ""


def _repl(session: Session, model_fn: Callable, read_line: Callable[[str], str]) -> None:
    """The prompt loop. Returns when the user ends the session."""
    while True:
        task = _read_task(read_line)
        if task is None:
            return
        if not task:
            continue

        if _looks_like_command(task):
            if not run_command(task, session):
                return
            continue

        refusal = session.refusal()
        if refusal is not None:
            print(f"refused: {refusal}", file=sys.stderr)
            continue

        take_turn(session, task, model_fn)


def main(
    argv: list[str] | None = None,
    *,
    model_fn: Callable = call_model,
    read_line: Callable[[str], str] | None = None,
) -> int:
    """`model_fn` and `read_line` are injected so the whole session is
    testable with no key and no network - the same seam run_loop offers.

    `read_line=None` means "decide at runtime": a terminal gets the menu, a
    pipe gets `input()`, and a test gets whatever it passed.
    """
    load_config()

    if argv and ("--help" in argv[1:] or "-h" in argv[1:]):
        print(USAGE)
        return 0

    resume_id, selected_workspace, ok = _parse_resume_flag(argv)
    if not ok:
        print(USAGE, file=sys.stderr)
        return 2

    try:
        provider = active_provider()
    except ModelError as err:
        return report_model_error(err)
    pricing = Pricing.free() if provider.free else Pricing.from_env()

    turn_limits = Budget.from_env(pricing=pricing)
    if _spend_limit_without_prices(turn_limits):
        print(
            "error [usage]: a spend limit needs prices. Set "
            "PRICE_INPUT_CACHE_HIT_PER_M, PRICE_INPUT_CACHE_MISS_PER_M and "
            "PRICE_OUTPUT_PER_M in .env.",
            file=sys.stderr,
        )
        return 2

    # Read from .env with every other rule set, exactly as cli.py does. A
    # retry policy the two entry points disagree about is two agents.
    retry = RetryPolicy.from_env()

    if resume_id not in (None, CONTINUE_FLAG) and selected_workspace is None:
        # An explicit resume gets its root from the journal, not cwd or env.
        root = None
    else:
        try:
            root = workspace_root(selected_workspace)
        except ValueError as exc:
            print(f"error [workspace]: {exc}", file=sys.stderr)
            return 2
    if resume_id == CONTINUE_FLAG:
        assert root is not None
        resume_id = latest_session(root)
        if resume_id is None:
            print("error [no_journal]: no session for this workspace", file=sys.stderr)
            return 2
    if resume_id is not None:
        try:
            recorded = recorded_workspace(RUNS_DIR, resume_id)
        except FileNotFoundError as exc:
            print(f"error [no_journal]: {exc}", file=sys.stderr)
            return 2
        except (JournalCorrupt, ValueError, OSError) as exc:
            print(f"error [resume]: {exc}", file=sys.stderr)
            return 2
        if selected_workspace is not None and root != recorded:
            print("error [workspace]: --workspace conflicts with the journal", file=sys.stderr)
            return 2
        try:
            root = workspace_root(recorded)
        except ValueError as exc:
            print(f"error [workspace]: {exc}", file=sys.stderr)
            return 2
    assert root is not None
    if not root.is_dir():
        print(f"error [workspace]: {root} is missing", file=sys.stderr)
        return 2
    try:
        session = start_session(turn_limits, root, resume_id, retry)
    except (ValueError, OSError, UnicodeError) as exc:
        print(f"error [memory]: {exc}", file=sys.stderr)
        return 2
    if session is None:
        return 2
    session.model_label = model_label(provider)

    _print_banner(session, provider, turn_limits)
    _repl(session, model_fn, read_line if read_line is not None else make_read_line())

    print(f"[session {session.session_id}] {session.describe()}", file=sys.stderr)
    return 0


def console_main() -> int:
    """Entry point installed as the ``mini-agent-chat`` command."""
    return main(sys.argv)


if __name__ == "__main__":
    raise SystemExit(console_main())
