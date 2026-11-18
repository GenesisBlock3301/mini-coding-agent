"""Generated runtime locations, separate from source and evaluation templates.

Importing this module creates nothing. Historical journals/results keep their
original bytes; readers translate their old artifact paths when necessary.
"""
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class RuntimePaths:
    project: Path

    @property
    def root(self) -> Path:
        return self.project / '.runtime'

    @property
    def journals(self) -> Path:
        return self.root / 'journals'

    @property
    def eval_workspaces(self) -> Path:
        return self.root / 'eval-workspaces'

    def resolve_artifact(self, path: str | Path) -> Path:
        """Translate legacy project-local artifact paths; leave others alone."""
        candidate = Path(path).expanduser().absolute()
        legacy = self.project / 'runs'
        try:
            relative = candidate.relative_to(legacy)
        except ValueError:
            return Path(path)
        if relative.parts and relative.parts[0] == 'workspaces':
            target = self.eval_workspaces.joinpath(*relative.parts[1:])
        elif len(relative.parts) == 1 and relative.suffix == '.jsonl':
            target = self.journals / relative
        else:
            return Path(path)
        # Before an explicit migration, existing source data still wins.
        return target if not candidate.exists() and target.exists() else Path(path)


PATHS = RuntimePaths(Path(__file__).resolve().parents[1])
JOURNALS_DIR = Path(os.getenv("RUNS_DIR", str(PATHS.journals))).expanduser().resolve()
EVAL_WORKSPACES_DIR = Path(
    os.getenv("EVAL_WORKSPACES_DIR", str(PATHS.eval_workspaces))
).expanduser().resolve()


def workspace_root(override=None) -> Path:
    """Resolve entry-point workspace aliases before applying the path policy."""
    from agent.policy import workspace_root as policy_workspace_root
    raw = override or os.getenv('WORKSPACE_DIR')
    return policy_workspace_root(PATHS.resolve_artifact(raw) if raw else None)


def recorded_workspace(directory: Path, run_id: str) -> Path:
    """Read a journal's workspace before choosing a root or making a model call."""
    from agent.journal import Journal, JournalCorrupt

    events = Journal.open(directory, run_id).events()
    starts = [event for event in events if event.get('t') == 'run_started']
    if not starts or not isinstance(starts[0].get('workspace'), str):
        raise ValueError('journal has no recorded workspace')
    return PATHS.resolve_artifact(starts[0]['workspace']).resolve()
