# Fixture projects (Session 7+)

One directory per case. The agent sees `workspace/` and nothing else.

    fixtures/<case>/
        case.json          the task, the allowed changes, the golden fix
        workspace/         copied fresh per run - this IS the agent's world
        verify/check.py    the independent check - never copied in (D60)

The split is the point. `check.py` lives outside `workspace/`, so the agent
cannot read the tests, cannot edit them, and cannot make a failing check pass
by changing what it asserts. Making a test pass by editing the test is a
correct solution to the problem you posed; the fix is to pose a different
problem.

Build, run, verify:

    python scripts/fixture.py build tail_window
    python -m agent.cli --tools --workspace=<the path it printed> "<the task>"
    python scripts/fixture.py verify tail_window --workspace=<that path>

`case.json` carries the `golden` fix - the one-line answer, written down
before any model saw the fixture. A fixture whose correct fix you cannot state
in advance is a fixture you cannot grade.
