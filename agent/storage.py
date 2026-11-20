"""Durable agent state stored outside the installed package and workspace."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def default_state_dir() -> Path:
    configured = os.getenv("MINI_AGENT_STATE_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    xdg = os.getenv("XDG_STATE_HOME")
    parent = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return (parent / "mini-coding-agent").resolve()


@dataclass(frozen=True)
class RuntimePaths:
    root: Path

    @property
    def journals(self) -> Path:
        return self.root / "journals"

    def resolve_artifact(self, path: str | Path) -> Path:
        """Accept paths recorded in journals without rewriting their bytes."""
        return Path(path)


PATHS = RuntimePaths(default_state_dir())
JOURNALS_DIR = Path(os.getenv("RUNS_DIR", str(PATHS.journals))).expanduser().resolve()


def workspace_root(override=None) -> Path:
    """Resolve the workspace from a flag, environment, or current directory."""
    from agent.config import config_path
    from agent.policy import workspace_root as policy_workspace_root
    root = policy_workspace_root(override)
    for state in (PATHS.root, JOURNALS_DIR, config_path()):
        if state == root or root in state.parents:
            raise ValueError("agent state and configuration must be outside the selected workspace")
    return root


def recorded_workspace(directory: Path, run_id: str) -> Path:
    """Read a journal's workspace before choosing a root or making a model call."""
    from agent.journal import Journal

    events = Journal.open(directory, run_id).events()
    starts = [event for event in events if event.get("t") == "run_started"]
    if not starts or not isinstance(starts[0].get("workspace"), str):
        raise ValueError("journal has no recorded workspace")
    return Path(starts[0]["workspace"]).expanduser().resolve()
