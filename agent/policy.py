"""Workspace boundaries and operation limits. (Session 2+)

Resolves every path against the playground workspace and rejects escapes,
including symlinks. Enforces step, tool-call, output-size, duration and budget
limits. These rules are enforced here in code, never by the prompt.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKSPACE = PROJECT_ROOT / "playground"


def workspace_root(override: str | os.PathLike[str] | None = None) -> Path:
    """The absolute, resolved directory the agent is allowed to touch.

    Precedence: explicit override > WORKSPACE_DIR env var > <project>/playground

    The default is anchored to the project root, NOT the current working
    directory. A relative default would make the workspace depend on where the
    process was launched from: running the CLI from ~ would silently point at
    ~/playground, which resolve() returns without complaint because it does not
    check existence. That reads as a boundary bug in step 4 but is really a
    config bug here.

    A relative path given EXPLICITLY (override or WORKSPACE_DIR) still resolves
    against the cwd - that is the normal convention for user-supplied config.

    D1: resolved on every call instead of cached in a module-level constant.
    A global would be one line shorter today and a rewrite in Session 13, where
    each concurrent run needs its own workspace - that run passes `override`.

    D2: `.resolve()`, not `normpath`. It follows symlinks, which is what makes
    the comparison in later steps meaningful. It also fixes the macOS
    /var -> /private/var mismatch that would otherwise break every test.
    """
    raw = override or os.getenv("WORKSPACE_DIR") or DEFAULT_WORKSPACE
    return Path(raw).expanduser().resolve()


class PolicyViolation(Exception):
    """A path the policy refuses. Raised here, converted to a ToolResult in tools.py.

    Two audiences, two fields:

    `code`    stable and machine-readable. tools.py switches on it, Session 11
              evals assert on it. Never reword it to improve a message.
    `message` what the MODEL reads. It must say what IS allowed, not only what
              was refused, because the model's only chance to correct itself is
              this sentence.

    The message may quote the path the model sent. It must never contain the
    RESOLVED path - that leaks the host filesystem layout into the model's
    context and into the JSONL traces of Session 10.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def echo_path(path: object, limit: int = 80) -> str:
    """Quote the caller's path for an error message, bounded.

    Public because tools.py quotes paths in its own messages (step 11). Error
    text that truncates in one module and not the other is how a 10k-character
    path sneaks back into the trace.

    A model can emit a 10k-character path. Echoing it whole would put 10k
    characters into the model's context and into every JSONL trace, to say
    one thing. Output limits start here, not in step 13.
    """
    text = str(path)
    if len(text) > limit:
        return f"{text[:limit]!r}... ({len(text)} chars)"
    return repr(text)


def resolve_in_workspace(path: str | os.PathLike[str], *, root: Path) -> Path:
    """Resolve a workspace-relative path to an absolute Path.

    Rules are applied in a fixed order, cheapest and most specific first:

        step 3  absolute paths      - rejected before the join
        step 4  containment         - the resolved path must sit under root
        step 5  symlinks            - no component may be a symlink

    The order is load-bearing. Step 3 runs before the join because the join
    discards the root. Step 5 runs after step 4 so that it only ever lstats
    paths already known to be inside the workspace.

    Step 6 - the invariant that order exists to protect:

        NO REJECTION MAY DEPEND ON WHETHER THE TARGET EXISTS.

    A real file and a fabricated one must produce the identical code, or the
    agent becomes a probe for enumerating the host filesystem. What each code
    reveals:

        absolute_path     nothing. Purely syntactic, decided before any
                          filesystem call is made.
        outside_workspace nothing about the target - only that the caller's
                          path left the boundary.
        symlink           that a component INSIDE the workspace is a link.
                          Inside information, acceptable.
        not_found         that a file does not exist. Acceptable ONLY for
                          paths already proven to be inside, which is why it
                          belongs in tools.py (step 11) and never here.

    This is why policy.py has no existence check at all. When step 11 adds
    not_found to tools.py, it must run AFTER resolve_in_workspace, never
    before - otherwise a real outside file reports not_found while a fake one
    reports outside_workspace, and the oracle is back.

    `root` is a required keyword argument rather than a default. The caller
    must say which workspace it means - in Session 13 there is more than one.

    Returns a Path even when the target does not exist: a missing file is a
    TOOL error the model can recover from, not a path-layer crash.

    Raises:
        PolicyViolation: absolute_path (3), outside_workspace (4),
            symlink (5), invalid_path (7)
    """
    # Step 3. This MUST come before the join. In pathlib an absolute right-hand
    # side discards the left: Path("/a/b") / "/etc/passwd" == Path("/etc/passwd").
    # After the join the root has already been thrown away, so checking later
    # would catch this only by accident of ordering - and would stat a path we
    # never intended to touch.
    # Step 7a. Null bytes are rejected syntactically, for the same reason as
    # step 3: no syscall should ever see this input. resolve() would raise
    # ValueError anyway, but only after the path reached the filesystem layer.
    if "\0" in str(path):
        raise PolicyViolation(
            "invalid_path",
            f"{echo_path(path)} contains a null byte and is not a valid path.",
        )

    if Path(path).is_absolute():
        raise PolicyViolation(
            "absolute_path",
            f"{echo_path(path)} is an absolute path. Paths must be relative to the "
            "workspace root, e.g. 'src/app.py'.",
        )

    # The root is resolved defensively. The comparison below is only meaningful
    # if BOTH sides were canonicalised the same way; a caller passing an
    # unresolved root (/tmp/ws rather than /private/tmp/ws) would otherwise get
    # "outside_workspace" for paths that are plainly inside. resolve() is
    # idempotent, so this costs nothing when the root is already resolved.
    # Step 7b. Everything below touches the filesystem, and a model emits
    # tokens rather than valid filenames. The two known failures land in
    # DIFFERENT steps, which is why the whole region is wrapped rather than a
    # single call:
    #   null byte          -> ValueError from resolve()      (step 4)
    #   300-char component -> OSError ENAMETOOLONG from       (step 5)
    #                         is_symlink(); resolve() lets it through
    # PolicyViolation is neither a ValueError nor an OSError, so the rejections
    # raised inside this block pass through untouched.
    try:
        return _check(path, root)
    except (ValueError, OSError) as exc:
        raise PolicyViolation(
            "invalid_path",
            f"{echo_path(path)} is not a usable path. It may be malformed or too long.",
        ) from exc


def _check(path: str | os.PathLike[str], root: Path) -> Path:
    """Steps 4 and 5. Called only from resolve_in_workspace, inside its guard."""
    root = Path(root).resolve()
    candidate = (root / path).resolve()

    # Step 4. is_relative_to compares path COMPONENTS, not characters.
    #
    #   str(candidate).startswith(str(root))  is the bug, and it is quiet:
    #   the sibling directory ".../playground-evil" starts with the string
    #   ".../playground" while living completely outside it. Tuple comparison
    #   of ('...', 'playground-evil') against ('...', 'playground') does not.
    #
    # This also catches both symlink escapes for free, because resolve() has
    # already followed them - see step 5 for why that is not enough.
    if not candidate.is_relative_to(root):
        raise PolicyViolation(
            "outside_workspace",
            f"{echo_path(path)} resolves outside the workspace. Use a path inside "
            "the workspace, e.g. 'src/app.py'.",
        )

    # Step 5. Walk EVERY component, not just the last one.
    #
    # Path.is_symlink() inspects only the final component, so
    # "inside_dir_link/app.py" reports False while its parent is a symlink.
    # Containment does not help here either: that path resolves to
    # playground/src/app.py, which is legitimately inside.
    #
    # This is also defence in depth. Step 4 currently catches symlinks that
    # point outside only because resolve() follows them - swap resolve() for
    # normpath() and that protection silently disappears. This rule does not
    # depend on resolution at all.
    probe = root
    for part in Path(path).parts:
        probe = probe / part
        if probe.is_symlink():
            raise PolicyViolation(
                "symlink",
                f"{echo_path(path)} goes through a symlink ({echo_path(part, 40)}). Symlinks are "
                "not allowed; use the real path.",
            )

    return candidate
