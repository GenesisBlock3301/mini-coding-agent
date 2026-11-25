import pytest


@pytest.fixture(autouse=True)
def _workspace_is_trusted(monkeypatch):
    """Chat tests drive `main` with scripted input; trust has its own tests."""
    monkeypatch.setattr("agent.chat.ensure_trusted", lambda *a, **k: True)
