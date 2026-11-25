"""run_python asks first; every other tool passes straight through."""

import json

import pytest

from agent.approval import approving_dispatcher


@pytest.fixture(autouse=True)
def _no_bypass(monkeypatch):
    monkeypatch.delenv("ALLOW_RUN_PYTHON", raising=False)


def call(d, root, path="hi.py"):
    return d("run_python", json.dumps({"path": path}), root=root)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "hi.py").write_text("print('ran')")
    return tmp_path


def test_yes_runs_it(project):
    result = call(approving_dispatcher(lambda p: "y", interactive=True), project)
    assert result.ok and "ran" in result.content


def test_no_and_blank_refuse_and_the_script_never_runs(project):
    (project / "side.py").write_text("open('MARK','w').write('x')")
    for reply in ("n", ""):
        result = call(approving_dispatcher(lambda p, r=reply: r, interactive=True), project, "side.py")
        assert not result.ok and result.error_code == "denied_by_user"
    assert not (project / "MARK").exists()


def test_always_asks_once_per_dispatcher(project):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return "a"

    d = approving_dispatcher(ask, interactive=True)
    assert call(d, project).ok and call(d, project).ok
    assert len(asked) == 1
    assert len([1 for _ in [approving_dispatcher(ask, interactive=True)]]) == 1  # a new session asks again


def test_no_terminal_means_refused_not_run(project):
    result = call(approving_dispatcher(interactive=False), project)
    assert result.error_code == "approval_required"


def test_env_switch_skips_the_question(project, monkeypatch):
    monkeypatch.setenv("ALLOW_RUN_PYTHON", "1")

    def never(prompt):
        raise AssertionError("asked")

    assert call(approving_dispatcher(never, interactive=False), project).ok


def test_other_tools_are_never_gated(project):
    d = approving_dispatcher(interactive=False)
    result = d("read_file", json.dumps({"path": "hi.py"}), root=project)
    assert result.ok
