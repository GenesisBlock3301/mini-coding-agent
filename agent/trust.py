"""Folder trust: ask once per workspace before the agent works in it.

Modelled on Claude Code's "Do you trust the files in this folder?". The answer
is remembered outside the workspace, so a folder cannot grant itself trust.
Trusting a folder covers its subfolders. Home and filesystem roots are too
broad to remember: they are asked about every time and never stored.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable

from agent.storage import PATHS

STORE_NAME = "trusted-workspaces.json"


def store_path() -> Path:
    return PATHS.root / STORE_NAME


def too_broad(root: Path) -> bool:
    """The home directory, one of its parents, or a filesystem root."""
    root = root.resolve()
    home = Path.home().resolve()
    return root == home or root in home.parents or root == Path(root.anchor)


def _load() -> list[Path]:
    try:
        data = json.loads(store_path().read_text(encoding="utf-8"))
        return [Path(p) for p in data["trusted"] if isinstance(p, str)]
    except (OSError, ValueError, KeyError, TypeError):
        return []  # missing or damaged file means "nothing trusted"


def is_trusted(root: Path) -> bool:
    root = root.resolve()
    if too_broad(root):
        return False
    return any(root == t or t in root.parents for t in _load())


def trust(root: Path) -> None:
    root = root.resolve()
    if too_broad(root):
        raise ValueError("refusing to remember a folder this broad")
    entries = sorted({str(p) for p in _load()} | {str(root)})
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".trust-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"trusted": entries}, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def ensure_trusted(
    root: Path,
    *,
    explicit: bool = False,
    ask: Callable[[str], str] = input,
    interactive: bool | None = None,
) -> bool:
    """True when the agent may work in `root`; prints the reason when not.

    `explicit` is True when the folder came from --workspace or a resumed
    journal. A too-broad folder reached only by `cd` is refused outright.
    """
    root = root.resolve()
    broad = too_broad(root)
    if broad and not explicit:
        print(
            f"error [trust]: {root} is too broad to be a workspace. "
            "cd into a project folder, or pass --workspace=DIR.",
            file=sys.stderr,
        )
        return False
    if is_trusted(root):
        return True
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        print(
            f"error [trust]: {root} is not trusted yet. "
            "Run mini-chat there once in a terminal and answer the prompt.",
            file=sys.stderr,
        )
        return False
    print(f"\nDo you trust the files in this folder?\n  {root}\n", file=sys.stderr)
    print(
        "The agent can read and edit files here, and this folder's .env is loaded.\n"
        "Only trust folders you own or have reviewed."
        + ("\nThis folder is very broad; the answer will not be remembered." if broad else ""),
        file=sys.stderr,
    )
    try:
        answer = ask("Trust this folder? [y/N]: ").strip().lower()
    except (EOFError, OSError):
        answer = ""
    if answer not in ("y", "yes"):
        print("error [trust]: folder not trusted; nothing was run.", file=sys.stderr)
        return False
    if not broad:
        trust(root)
    return True
