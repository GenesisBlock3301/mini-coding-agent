"""Kill the agent mid-run and look at what is left. Spoiler: nothing.

    python scripts/crash_demo.py --state        # what run state exists on disk?
    python scripts/crash_demo.py --at 1         # crash right after model call #1
    python scripts/crash_demo.py --at 2         # crash after a tool has run
    python scripts/crash_demo.py --sigkill      # real SIGKILL of the real CLI
    python scripts/crash_demo.py --cost         # crash, restart, add up the bill
    python scripts/crash_demo.py --edit         # Session 6: crash around a WRITE

    python scripts/crash_demo.py --at 1 "read src/utils.py"   # your own task

This is not a test. Tests assert; this one makes real API calls and prints what
survives, so you can see the hole before deciding what plugs it.

Each run costs a few hundredths of a cent - except --edit, which is free.

--edit scripts the model response on purpose. Everything being demonstrated
there is below the model: the journal, the atomic rename, and what is on disk
afterwards. Paying a provider to re-decide which tool to call would make the
demo cost money AND become flaky, and would not make the filesystem any more
real than it already is.

The crash point is `on_response`, the callback agent/loop.py already calls after
every model call - no change to loop.py is needed to break it, which is itself
the finding: the loop has no durability to break.

os._exit() is used deliberately instead of sys.exit(). sys.exit raises, which
unwinds the stack and runs `finally` blocks and atexit handlers - a polite
shutdown you will not get from a SIGKILL, an OOM kill, or a power cut.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

# Running a file inside scripts/ puts scripts/ on sys.path, not the project root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from agent.cli import RUNS_DIR, TOOLS_SYSTEM_PROMPT
from agent.journal import TOOL_INTENT, TOOL_OUTCOME, Journal, new_run_id
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.policy import workspace_root
from agent.tools import TOOL_SCHEMAS

# find_dotenv() searches upward from the CALLING FILE, which here is scripts/.
# Point it at the project root or you get "No API key" from a correct setup.
load_dotenv(ROOT / ".env")

DEFAULT_TASK = "what's in the awkward folder?"

# Anywhere a run could plausibly have left a trace of itself.
STATE_GLOBS = ("run-*.jsonl", "runs/*", ".agent-state*", "*.journal")


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def show_state(label: str) -> None:
    """List any durable run state. There is none; that is the point."""
    found = [p for pattern in STATE_GLOBS for p in ROOT.glob(pattern)]
    print(f"  {label}: ", end="")
    if not found:
        print("no run state on disk. The conversation exists only in RAM.")
        return
    for path in found:
        print(f"\n    {path.relative_to(ROOT)}  ({path.stat().st_size} bytes)")


def start_messages(task: str) -> list[dict]:
    return [
        {"role": "system", "content": TOOLS_SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]


def crash_run(task: str, crash_at: int) -> None:
    """Run the loop for real and pull the plug after model call `crash_at`."""
    banner(f"CRASH after model call #{crash_at}")
    show_state("before")
    print()

    def on_response(step, response):
        print(
            f"  [step {step}] model returned: tool_calls={len(response.tool_calls)} "
            f"finish={response.finish_reason} "
            f"billed in={response.prompt_tokens} out={response.completion_tokens}"
        )
        if step != crash_at:
            return
        print(f"  [step {step}] >>> PULLING THE PLUG: os._exit(137) <<<")
        print(
            f"  [step {step}] lost with it: {response.prompt_tokens + response.completion_tokens}"
            " billed tokens, the whole message history, and any idea that this run existed."
        )
        sys.stdout.flush()
        os._exit(137)

    def on_tool(step, call, result):
        print(
            f"  [step {step}] EXECUTED {call['function']['name']}"
            f"({call['function']['arguments']}) -> ok={result.ok}"
        )
        print(
            "              nothing recorded that this happened. Today the tool is"
            " read-only, so you only lost money."
        )
        print(
            "              in Session 6 this is replace_text and the file is already"
            " edited, with no record that it was."
        )

    result = run_loop(
        start_messages(task),
        root=workspace_root(),
        tools=TOOL_SCHEMAS,
        max_steps=5,
        on_response=on_response,
        on_tool=on_tool,
    )
    print(f"\n  did NOT crash (the run finished in {result.steps} steps before step {crash_at}).")
    print(f"  answer: {result.text}")


def sigkill_run(task: str, after_seconds: float = 1.6) -> None:
    """SIGKILL the actual CLI process. Nothing simulated, nothing cooperative."""
    import time

    banner(f"REAL SIGKILL of agent/cli.py after {after_seconds}s")
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "agent" / "cli.py"), "--tools", task],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env, cwd=ROOT,
    )
    time.sleep(after_seconds)
    proc.send_signal(signal.SIGKILL)
    output, _ = proc.communicate()

    print("  what the user saw before the process died:")
    for line in output.strip().splitlines():
        print(f"    {line}")
    print(f"\n  returncode: {proc.returncode}   (-9 == SIGKILL, not a clean exit)")
    print("  no traceback, no `finally`, no atexit. This is what a real crash looks like.")
    show_state("after")


def cost_run(task: str) -> None:
    """Crash once, restart from zero, and add up what one answer cost."""
    banner("THE BILL: crash, then restart from scratch")

    child = subprocess.run(
        [sys.executable, __file__, "--at", "1", task],
        capture_output=True, text=True, cwd=ROOT,
    )
    billed = re.search(r"billed in=(\d+) out=(\d+)", child.stdout)
    wasted = sum(int(g) for g in billed.groups()) if billed else 0
    print(f"  run 1 (crashed): {wasted} tokens billed, 0 answers produced")

    print("  run 2 (restart): no memory of run 1, so it starts from the first message...")
    result = run_loop(
        start_messages(task), root=workspace_root(), tools=TOOL_SCHEMAS, max_steps=5
    )
    print(f"  run 2 finished: stop={result.stop_reason} steps={result.steps} "
          f"tokens={result.total_tokens}")

    total = wasted + result.total_tokens
    print(f"\n  ONE answer cost {total} tokens, of which {wasted} bought nothing.")
    print("  Nothing in the restart could reuse run 1's work, because run 1 left none.")
    print("  Worse: nothing in the restart could even tell that run 1 had happened.")


# --------------------------------------------------------------------------
# Session 6. The same demo, but the tool now WRITES.
#
# Until this session a tool_intent with no tool_outcome meant "a read may not
# have happened" - you lost money and nothing else. Now it means "a file may
# be half written", and step 9 of docs/session-06.md asks the question this
# block exists to answer: can you tell, FROM THE JOURNAL ALONE, whether the
# edit landed?
#
# Two crash points, deliberately indistinguishable on paper:
#
#   --edit before   intent is on disk, the write never started
#   --edit after    intent is on disk, the write COMPLETED, outcome never ran
#
# Both leave a journal holding exactly one tool_intent and no tool_outcome.
# The file is different. The journal is not.
# --------------------------------------------------------------------------

EDIT_FIXTURE = "crash_target.py"
EDIT_BEFORE_BYTES = b"def subtract(a, b):\n    return a + b\n"
EDIT_AFTER_BYTES = b"def subtract(a, b):\n    return a - b\n"


def _scripted_edit() -> ModelResponse:
    """One response, always the same: edit the fixture, once."""
    return ModelResponse(
        text=None,
        model="scripted",
        finish_reason="tool_calls",
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        duration_ms=0.0,
        tool_calls=[
            {
                "id": "call_edit_1",
                "type": "function",
                "function": {
                    "name": "replace_text",
                    "arguments": json.dumps(
                        {
                            "path": EDIT_FIXTURE,
                            "old": "return a + b",
                            "new": "return a - b",
                        }
                    ),
                },
            }
        ],
    )


def edit_crash_child(when: str, run_id: str) -> None:
    """Run until the write, then die. Called in a SUBPROCESS; never returns.

    The plug is pulled inside loop.dispatch rather than in on_tool, because
    on_tool fires from _answer - after the outcome has already been written.
    To land BETWEEN the two records you have to interrupt the thing they
    bracket.
    """
    import agent.loop as loop_module

    real_dispatch = loop_module.dispatch

    def crashing_dispatch(name, arguments, *, root):
        if when == "before":
            # tool_intent is already durable. Nothing has touched the file.
            os._exit(137)
        result = real_dispatch(name, arguments, root=root)
        # The rename has happened. The outcome record never will.
        os._exit(137)

    loop_module.dispatch = crashing_dispatch

    run_loop(
        start_messages("fix the subtraction"),
        root=workspace_root(),
        tools=TOOL_SCHEMAS,
        model_fn=lambda messages, *, tools=None: _scripted_edit(),
        journal=Journal.create(RUNS_DIR, run_id),
        max_steps=5,
    )


def _journal_summary(run_id: str) -> tuple[int, int, list[str]]:
    """(intents, outcomes, the tool names that were entered)."""
    events = Journal.open(RUNS_DIR, run_id).events()
    intents = [e for e in events if e["t"] == TOOL_INTENT]
    outcomes = [e for e in events if e["t"] == TOOL_OUTCOME]
    return len(intents), len(outcomes), [e.get("name") for e in intents]


def edit_crash(when: str) -> str:
    """Crash around a write and report what the journal and the disk each say."""
    banner(f"CRASH {when.upper()} THE WRITE")

    target = workspace_root() / EDIT_FIXTURE
    target.write_bytes(EDIT_BEFORE_BYTES)
    before_hash = hashlib.sha256(EDIT_BEFORE_BYTES).hexdigest()[:12]
    print(f"  fixture {EDIT_FIXTURE}: {EDIT_BEFORE_BYTES!r}")
    print(f"  sha256[:12] before = {before_hash}")

    run_id = new_run_id()
    proc = subprocess.run(
        [sys.executable, __file__, "--edit-child", when, run_id],
        capture_output=True, text=True, cwd=ROOT,
    )
    print(f"\n  child exited {proc.returncode}  (137 == os._exit, no unwinding)")

    intents, outcomes, names = _journal_summary(run_id)
    print(f"\n  JOURNAL {run_id}")
    print(f"    tool_intent  : {intents}  {names}")
    print(f"    tool_outcome : {outcomes}")
    print("    -> the journal says a tool was entered and never answered.")

    raw = target.read_bytes()
    after_hash = hashlib.sha256(raw).hexdigest()[:12]
    landed = raw == EDIT_AFTER_BYTES
    print(f"\n  DISK")
    print(f"    contents          : {raw!r}")
    print(f"    sha256[:12] after : {after_hash}")
    print(f"    edit landed       : {landed}")
    print(f"    half-written      : {raw not in (EDIT_BEFORE_BYTES, EDIT_AFTER_BYTES)}")

    litter = list(workspace_root().glob(".agent-edit-*"))
    print(f"    temp files left   : {[p.name for p in litter] or 'none'}")

    target.unlink(missing_ok=True)
    for stray in litter:
        stray.unlink(missing_ok=True)
    return f"{intents} intent / {outcomes} outcome, edit landed = {landed}"


def edit_crash_both() -> None:
    """Run both crash points and put the two verdicts side by side."""
    before = edit_crash("before")
    after = edit_crash("after")

    banner("THE POINT")
    print(f"  crash BEFORE the write: {before}")
    print(f"  crash AFTER  the write: {after}")
    print()
    print("  The journals are IDENTICAL: one tool_intent, no tool_outcome.")
    print("  The files are not. So 'did the file change?' CANNOT be answered")
    print("  from the journal alone - which is docs/session-06.md self-test #2.")
    print()
    print("  Two ways to fix it, and they are not the same fix:")
    print("    - record a content hash with the intent, and the question")
    print("      becomes answerable after the fact;")
    print("    - keep the write atomic, and the question stops mattering,")
    print("      because the file is never half-written either way.")
    print("  replace_text does the second already: 'half-written' is False")
    print("  above in BOTH runs. Nothing yet does the first.")
    print()
    print("  Until something does, --resume re-runs an unanswered call. That")
    print("  is safe when the edit landed (old is gone -> no_match) and NOT")
    print("  safe when new contains old: the edit applies a second time.")


def main(argv: list[str]) -> int:
    args = argv[1:]
    task = next((a for a in args if not a.startswith("--") and not a.isdigit()), DEFAULT_TASK)

    if "--state" in args:
        banner("DURABLE RUN STATE")
        show_state("now")
        return 0
    if "--sigkill" in args:
        sigkill_run(task)
        return 0
    if "--cost" in args:
        cost_run(task)
        return 0
    if "--edit-child" in args:
        index = args.index("--edit-child")
        edit_crash_child(args[index + 1], args[index + 2])
        return 0  # unreachable: the child always os._exit()s
    if "--edit" in args:
        index = args.index("--edit")
        when = args[index + 1] if len(args) > index + 1 else None
        if when in ("before", "after"):
            edit_crash(when)
        else:
            edit_crash_both()
        return 0
    if "--at" in args:
        index = args.index("--at")
        crash_at = int(args[index + 1]) if len(args) > index + 1 else 1
        crash_run(task, crash_at)
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
