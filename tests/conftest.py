import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# These need evals/, scripts/ or fixtures/, which live on the `main` branch and
# are not packaged with this branch. Run them from `main`.
collect_ignore = [
    "test_compare.py",
    "test_evals.py",
    "test_fixture.py",
    "test_prune.py",
    "test_storage.py",
]


@pytest.fixture(autouse=True)
def _workspace_is_trusted(monkeypatch):
    """Chat tests drive `main` with scripted input; trust has its own tests."""
    monkeypatch.setattr("agent.chat.ensure_trusted", lambda *a, **k: True)
    monkeypatch.setattr("agent.cli.ensure_trusted", lambda *a, **k: True)


@pytest.fixture(scope="session", autouse=True)
def _attack_fixtures():
    """Build playground/ and playground-evil/ (gitignored) for the tool tests."""
    spec = importlib.util.spec_from_file_location("make_fixtures", ROOT / "scripts" / "make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.clean()
    module.build()
    (ROOT / "playground" / ".gitkeep").touch()


@pytest.fixture(autouse=True)
def _default_test_workspace(monkeypatch):
    """The tool tests were written against playground/ as the default root."""
    monkeypatch.setenv("WORKSPACE_DIR", str(ROOT / "playground"))
