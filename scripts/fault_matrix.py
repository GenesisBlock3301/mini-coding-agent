"""Kill a real run against a real fixture, then grade what is left. (Session 8)

    python3 scripts/fault_matrix.py            # every case, every crash point
    python3 scripts/fault_matrix.py tail_window

Phase 4 of docs/session-08.md, and the reason the two halves of this course
were built in this order:

  Session 5 gave the agent failpoints in the real code path.
  Session 6 made a tool that writes, atomically.
  Session 7 built an evaluator that grades the FILESYSTEM, never the
            transcript, from a pristine copy of a small project.
  Session 8 points the first at the third.

`scripts/crash_demo.py` prints prose for a human: it is a story about one
terminal. This prints rows. The difference matters because the question Phase
4 asks is comparative - for each crash point, does the verifier's verdict
agree with what the run SAID about itself? - and a story cannot be tallied.

No API key and no network. The model is scripted: it reads the case's golden
fix out of case.json and asks for exactly that edit. That is deliberate and it
is not cheating, because nothing being measured here is the model's judgment.
What is being measured is what survives a kill, and a scripted model makes the
crash point the only variable in the table.

Four crash points, in the order a run meets them:

    none           the control: nothing kills it
    before_write   dispatch entered, the file untouched
    after_write    os.replace returned, the outcome record never written
    after_outcome  the outcome is durable, the process dies before the answer
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.cli import RUNS_DIR
from agent.journal import Journal, new_run_id, rebuild
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.replay import plans_for
from agent.tools import TOOL_SCHEMAS

CRASH_POINTS = ("none", "before_write", "after_write", "after_outcome")


def load_harness():
    """Import scripts/fixture.py by path - scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "fixture_harness", ROOT / "scripts" / "fixture.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["fixture_harness"] = module
    spec.loader.exec_module(module)
    return module


fixture = load_harness()


# --------------------------------------------------------------------------
# The scripted model: one edit, then an answer.
# --------------------------------------------------------------------------


def edit_response(case) -> ModelResponse:
    return ModelResponse(
        text=None, model="scripted", finish_reason="tool_calls",
        prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=0.0,
        tool_calls=[{
            "id": "call_fix_1",
            "type": "function",
            "function": {
                "name": "replace_text",
                "arguments": json.dumps({
                    "path": case.golden["path"],
                    "old": case.golden["old"],
                    "new": case.golden["new"],
                }),
            },
        }],
    )


def answer_response(text="fixed it") -> ModelResponse:
    return ModelResponse(
        text=text, model="scripted", finish_reason="stop",
        prompt_tokens=0, completion_tokens=0, total_tokens=0, duration_ms=0.0,
        tool_calls=[],
    )


def scripted(*responses):
    script = list(responses)

    def model_fn(messages, *, tools=None):
        return script.pop(0) if script else answer_response()

    return model_fn


# --------------------------------------------------------------------------
# The child: one run, killed at one point, in its own process.
#
# os._exit, not an exception. An exception unwinds, and a stack that unwinds
# is a crash the agent cooperated with - which is the one kind of crash the
# user will never actually have.
# --------------------------------------------------------------------------


def child(case_name: str, when: str, run_id: str, workspace: str) -> None:
    import agent.loop as loop_module

    case = fixture.load_case(case_name)
    root = Path(workspace)
    real_dispatch = loop_module.dispatch

    def crashing_dispatch(name, arguments, *, root):
        if when == "before_write":
            os._exit(137)
        result = real_dispatch(name, arguments, root=root)
        if when == "after_write":
            os._exit(137)
        return result

    def on_tool(step, call, result):
        # Fires from _answer, i.e. AFTER tool_outcome is fsynced. Nothing is
        # in flight here: this is the crash where the journal is complete and
        # only the run's own summary is lost.
        if when == "after_outcome":
            os._exit(137)

    if when != "none":
        loop_module.dispatch = crashing_dispatch

    run_loop(
        [{"role": "user", "content": case.task}],
        root=root,
        tools=TOOL_SCHEMAS,
        model_fn=scripted(edit_response(case)),
        journal=Journal.create(RUNS_DIR, run_id),
        on_tool=on_tool,
        max_steps=4,
    )


# --------------------------------------------------------------------------
# The parent: build, kill, verify, resume, verify again.
# --------------------------------------------------------------------------


def workspace_for(case, when: str) -> Path:
    target = fixture.BUILD_DIR / f"{case.name}--{when}"
    if target.exists():
        return fixture.reset(case, target)
    return fixture.build(case, into=target)


def resume(case, workspace: Path, run_id: str) -> tuple[list[str], list[str]]:
    """Pick the journal up and finish the run. Returns (verdicts, changed)."""
    journal = Journal.open(RUNS_DIR, run_id)
    state = rebuild(journal.events())
    verdicts = [p.verdict for p in plans_for(state, root=workspace).values()]
    result = run_loop(
        [], root=workspace, tools=TOOL_SCHEMAS, model_fn=scripted(),
        journal=journal, resume=state, max_steps=4,
    )
    return verdicts, result.changed_paths


def one_row(case, when: str) -> dict:
    workspace = workspace_for(case, when)
    run_id = new_run_id()

    proc = subprocess.run(
        [sys.executable, __file__, "--child", case.name, when, run_id, str(workspace)],
        capture_output=True, text=True, cwd=ROOT,
    )
    killed = proc.returncode == 137

    after_crash = fixture.verify(case, workspace)
    verdicts, changed = resume(case, workspace, run_id)
    after_resume = fixture.verify(case, workspace)

    return {
        "case": case.name,
        "when": when,
        "killed": killed,
        # What the DEAD process told anyone. A killed run prints no totals and
        # returns no LoopResult: its answer was never generated and its
        # changed_paths died in RAM with it.
        "said": "-" if killed else ("changed" if changed else "nothing"),
        "crash_ok": after_crash.ok,
        "verdict": ",".join(verdicts) or "-",
        "resume_said": ", ".join(changed) or "nothing",
        "resume_ok": after_resume.ok,
    }


def print_rows(rows: list[dict]) -> None:
    head = f"{'case':<14}{'crash':<15}{'fixed?':<8}{'run said':<10}{'replay':<9}{'resume said':<16}{'fixed?':<7}"
    print(head)
    print("-" * len(head))
    for row in rows:
        print(
            f"{row['case']:<14}{row['when']:<15}"
            f"{('PASS' if row['crash_ok'] else 'FAIL'):<8}"
            f"{row['said']:<10}{row['verdict']:<9}{row['resume_said']:<16}"
            f"{('PASS' if row['resume_ok'] else 'FAIL'):<7}"
        )


def print_findings(rows: list[dict]) -> None:
    gaps = [r for r in rows if r["killed"] and r["crash_ok"]]
    print(f"\n  {len(gaps)} run(s) were killed AFTER fixing the bug.")
    print("  The verifier graded every one of them as fixed. Not one of them")
    print("  said so: the process that could have reported it was gone.")
    print()
    print("  Self-test #8 - which of the four signals survives a kill between")
    print("  the tool call and the tool result:")
    print("    verifier        SURVIVES   it reads the filesystem")
    print("    diff vs template SURVIVES  same reason, same bytes")
    print("    changed_paths   DIES       a list in a dataclass in dead RAM,")
    print("                               recoverable only because the journal")
    print("                               records changed_path per outcome")
    print("    final message   NEVER WAS  the model was never asked for it")
    print()
    print("  Which is the argument for why the evaluator was built to read the")
    print("  filesystem in the first place (Session 7, E1): the two signals")
    print("  that survive are the two that never belonged to the agent.")


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args and args[0] == "--child":
        child(args[1], args[2], args[3], args[4])
        return 0  # unreachable when `when` != none

    names = [a for a in args if not a.startswith("-")] or fixture.list_cases()
    rows = []
    for name in names:
        case = fixture.load_case(name)
        for when in CRASH_POINTS:
            rows.append(one_row(case, when))

    print(f"\n{'=' * 78}\nFAULT MATRIX  -  {len(rows)} runs, scripted model, no network\n{'=' * 78}")
    print_rows(rows)
    print_findings(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
