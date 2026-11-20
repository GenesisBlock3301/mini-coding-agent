"""Session 13: create-only writes and recovery use a disposable workspace."""

import json
import os

import pytest

from agent import replay
from agent.journal import TOOL_INTENT, Journal, new_run_id, rebuild
from agent.loop import run_loop
from agent.model import ModelResponse
from agent.tools import EDIT_TMP_PREFIX, MAX_CREATE_BYTES, WRITES, dispatch, write_file


def _response(*calls, text=None):
    return ModelResponse(
        text=text, model="scripted", finish_reason="tool_calls" if calls else "stop",
        prompt_tokens=10, completion_tokens=5, total_tokens=15,
        duration_ms=0.0, tool_calls=list(calls),
    )


def _call(path="new.txt", content="hello 🌍\n"):
    return {
        "id": "create_1", "type": "function",
        "function": {"name": "write_file", "arguments": json.dumps({"path": path, "content": content})},
    }


class _Model:
    def __init__(self, *answers):
        self.answers = iter(answers)

    def __call__(self, messages, *, tools=None):
        return next(self.answers)


def test_create_is_utf8_and_exposed_through_dispatch(tmp_path):
    (tmp_path / "src").mkdir()
    result = dispatch("write_file", json.dumps({"path": "src/new.txt", "content": "hello 🌍\n"}), root=tmp_path)
    assert result.ok and result.changed_path == "src/new.txt"
    assert (tmp_path / "src/new.txt").read_bytes() == "hello 🌍\n".encode()
    assert "write_file" in WRITES
    assert not list(tmp_path.rglob(f"{EDIT_TMP_PREFIX}*"))


def test_existing_file_is_never_overwritten(tmp_path):
    target = tmp_path / "new.txt"
    target.write_text("original")
    before = target.stat().st_mtime_ns
    result = write_file("new.txt", "replacement", root=tmp_path)
    assert result.error_code == "already_exists"
    assert target.read_text() == "original"
    assert target.stat().st_mtime_ns == before


def test_racing_creation_is_not_overwritten(tmp_path, monkeypatch):
    real_link = os.link

    def racing_link(source, target):
        target.write_text("other writer")
        real_link(source, target)

    monkeypatch.setattr(os, "link", racing_link)
    result = write_file("new.txt", "agent", root=tmp_path)
    assert result.error_code == "already_exists"
    assert (tmp_path / "new.txt").read_text() == "other writer"
    assert not list(tmp_path.glob(f"{EDIT_TMP_PREFIX}*"))


@pytest.mark.parametrize("path,code", [
    ("../outside.txt", "outside_workspace"),
    ("/tmp/outside.txt", "absolute_path"),
    ("missing/new.txt", "parent_not_found"),
    (".", "already_exists"),
])
def test_refusals_create_nothing(tmp_path, path, code):
    result = write_file(path, "hello", root=tmp_path)
    assert result.error_code == code
    assert not list(tmp_path.glob(f"{EDIT_TMP_PREFIX}*"))


def test_symlink_component_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    result = write_file("link/new.txt", "hello", root=tmp_path)
    assert result.error_code == "symlink"
    assert not (tmp_path / "real/new.txt").exists()


def test_invalid_or_oversized_content_is_refused(tmp_path):
    assert write_file("x", "\0", root=tmp_path).error_code == "invalid_arguments"
    assert write_file("x", "\ud800", root=tmp_path).error_code == "invalid_arguments"
    assert write_file("x", "a" * (MAX_CREATE_BYTES + 1), root=tmp_path).error_code == "too_large_to_create"
    assert not (tmp_path / "x").exists()


def _interrupted_run(tmp_path, monkeypatch, *, after=False):
    import agent.loop as loop_module

    journal = Journal.create(tmp_path / "journals", new_run_id())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_dispatch = loop_module.dispatch

    def interrupted(name, arguments, *, root):
        if after:
            real_dispatch(name, arguments, root=root)
        raise RuntimeError("simulated process death")

    monkeypatch.setattr(loop_module, "dispatch", interrupted)
    with pytest.raises(RuntimeError, match="simulated process death"):
        run_loop(
            [{"role": "user", "content": "create a file"}], root=workspace,
            journal=journal, model_fn=_Model(_response(_call())), max_steps=5,
        )
    monkeypatch.setattr(loop_module, "dispatch", real_dispatch)
    state = rebuild(journal.events())
    intent = next(event for event in journal.events() if event["t"] == TOOL_INTENT)
    assert intent["before"] == replay.ABSENT
    return workspace, journal, state


def test_resume_replays_a_create_that_never_landed(tmp_path, monkeypatch):
    workspace, journal, state = _interrupted_run(tmp_path, monkeypatch)
    result = run_loop([], root=workspace, journal=journal, resume=state,
                      model_fn=_Model(_response(text="done")), max_steps=5)
    assert (workspace / "new.txt").read_text() == "hello 🌍\n"
    assert result.changed_paths == ["new.txt"]
    assert result.interrupted_calls == []


def test_resume_recognizes_the_exact_created_file(tmp_path, monkeypatch):
    workspace, journal, state = _interrupted_run(tmp_path, monkeypatch, after=True)
    result = run_loop([], root=workspace, journal=journal, resume=state,
                      model_fn=_Model(_response(text="done")), max_steps=5)
    assert (workspace / "new.txt").read_text() == "hello 🌍\n"
    assert result.changed_paths == ["new.txt"]
    assert result.interrupted_calls == [replay.CHANGED]


def test_resume_refuses_a_different_file_created_after_crash(tmp_path, monkeypatch):
    workspace, journal, state = _interrupted_run(tmp_path, monkeypatch)
    (workspace / "new.txt").write_text("someone else's work")
    result = run_loop([], root=workspace, journal=journal, resume=state,
                      model_fn=_Model(_response(text="done")), max_steps=5)
    assert (workspace / "new.txt").read_text() == "someone else's work"
    assert result.interrupted_calls == [replay.UNKNOWN]
    assert result.changed_paths == []


def test_loop_records_successful_create(tmp_path):
    journal = Journal.create(tmp_path / "journals", new_run_id())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    result = run_loop(
        [{"role": "user", "content": "create a file"}], root=workspace,
        journal=journal, model_fn=_Model(_response(_call()), _response(text="done")),
        max_steps=5,
    )
    assert result.changed_paths == ["new.txt"]
    assert (workspace / "new.txt").read_text() == "hello 🌍\n"
    assert next(e for e in journal.events() if e["t"] == TOOL_INTENT)["before"] == replay.ABSENT


def test_one_shot_cli_can_create_a_file(tmp_path, monkeypatch, capsys):
    import agent.cli as cli

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "journals")
    monkeypatch.setenv("PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_APIKEY", "sk-test-not-used")
    real_loop = cli.run_loop
    model = _Model(_response(_call()), _response(text="done"))

    def scripted_loop(messages, **kwargs):
        return real_loop(messages, model_fn=model, **kwargs)

    monkeypatch.setattr(cli, "run_loop", scripted_loop)
    assert cli.main(["agent.cli", "--tools", f"--workspace={workspace}", "create new.txt"]) == 0
    assert (workspace / "new.txt").read_text() == "hello 🌍\n"
    assert "done" in capsys.readouterr().out
