"""Folder trust: asked once, remembered outside the workspace."""

from pathlib import Path

import pytest

from agent import trust


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "project"
    project.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(trust, "store_path", lambda: tmp_path / "state" / "t.json")
    return home, project


def answers(*lines):
    it = iter(lines)
    return lambda prompt: next(it)


def test_untrusted_folder_is_refused_without_a_terminal(env, capsys):
    _, project = env
    assert trust.ensure_trusted(project, interactive=False) is False
    assert "not trusted yet" in capsys.readouterr().err


def test_yes_is_remembered_and_not_asked_again(env):
    _, project = env
    assert trust.ensure_trusted(project, ask=answers("y"), interactive=True)
    assert trust.is_trusted(project)

    def fail(prompt):
        raise AssertionError("asked twice")

    assert trust.ensure_trusted(project, ask=fail, interactive=False)


def test_no_and_blank_are_refused_and_not_stored(env):
    _, project = env
    for reply in ("n", ""):
        assert not trust.ensure_trusted(project, ask=answers(reply), interactive=True)
    assert not trust.is_trusted(project)


def test_trusting_a_parent_covers_subfolders(env):
    _, project = env
    child = project / "sub" / "deep"
    child.mkdir(parents=True)
    trust.trust(project)
    assert trust.is_trusted(child)
    assert not trust.is_trusted(project.parent)


def test_home_by_cd_is_refused_even_if_a_terminal_exists(env, capsys):
    home, _ = env
    assert not trust.ensure_trusted(home, ask=answers("y"), interactive=True)
    assert "too broad" in capsys.readouterr().err


def test_home_by_explicit_workspace_asks_every_time_and_never_stores(env):
    home, _ = env
    assert trust.ensure_trusted(home, explicit=True, ask=answers("y"), interactive=True)
    assert not trust.is_trusted(home)
    with pytest.raises(ValueError):
        trust.trust(home)
    assert not trust.ensure_trusted(home, explicit=True, ask=answers("n"), interactive=True)


def test_filesystem_root_is_too_broad(env):
    assert trust.too_broad(Path("/"))


def test_damaged_store_means_nothing_is_trusted(env):
    _, project = env
    path = trust.store_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert not trust.is_trusted(project)


def test_end_of_input_counts_as_no(env):
    _, project = env

    def eof(prompt):
        raise EOFError

    assert not trust.ensure_trusted(project, ask=eof, interactive=True)


def test_one_shot_cli_refuses_an_untrusted_folder_before_any_model_call(tmp_path, monkeypatch, capsys):
    """mini-agent asks the same question as mini-chat. conftest pre-trusts
    folders for other tests; this one puts the real check back."""
    import agent.cli as cli

    monkeypatch.setattr(cli, "ensure_trusted", trust.ensure_trusted)
    monkeypatch.setattr(trust, "store_path", lambda: tmp_path / "state" / "t.json")
    project = tmp_path / "proj"
    project.mkdir()

    def no_model(*a, **k):
        raise AssertionError("the model must not be called")

    monkeypatch.setattr("agent.model.call_model", no_model)
    monkeypatch.setattr("agent.loop.run_loop", no_model)
    monkeypatch.setattr("sys.stdin", type("NoTty", (), {"isatty": lambda self: False})())
    code = cli.main(["mini-agent", f"--workspace={project}", "do something"])
    assert code == cli.EXIT_USAGE
    assert "not trusted yet" in capsys.readouterr().err
