# Session 13 — Create a File in the Selected Workspace

> **Note (2026-11-25):** written before `run_python` was added. Statements here that the agent has no command or code execution describe the project at that time. See [`run-python-addendum.md`](run-python-addendum.md).

**Goal:** let the local coding agent create a new text file in its selected workspace.

**Status: implemented.** The agent can create a file through the one-shot CLI or interactive chat and recover safely if the process stops before the result is journalled. An existing file is never overwritten by this tool.

The CLI, interactive chat, workspace override, budgets and journals already exist. This session adds one missing coding operation. It does not add an HTTP service, shell execution, file deletion, or general command approval.

## Decisions

- **Tool:** `write_file(path, content)` creates a new UTF-8 text file. Existing files are refused; replacing whole files is a separate future decision.
- **Boundary:** use the same workspace resolver and symlink rules as `replace_text`. Refusal must not reveal whether a path outside the workspace exists.
- **Size:** cap input bytes and refuse oversized content before writing.
- **Atomicity:** write to a temporary file in the target directory, flush it, and publish with an atomic no-clobber operation so a file created by another process is never replaced. Do not silently create missing parent directories.
- **Recovery:** journal the intent before the write. On resume, compare the intended content and recorded precondition with disk state. If the outcome is ambiguous, refuse replay and tell the model to inspect the file rather than write again.
- **Scope:** file creation only. Keep overwrite, multi-file patches, deletion, command execution and approvals in the later roadmap.

## Build order

1. Add the tool schema, argument validation and workspace policy checks. An existing path, missing parent, invalid encoding or oversized input gets a controlled result and changes nothing.
2. Add atomic creation and register the tool as a writer so the existing journal and replay safeguards apply.
3. Handle crash points before publication and after publication but before the outcome record. Resume must recognize an already-created file and must refuse a file whose content differs from the intended write.
4. Exercise the tool through both the one-shot CLI and interactive chat with a scripted model. Inspect the created file and journal independently.

## Acceptance checks

- A new file is created under the selected workspace with exactly the requested bytes.
- An existing file is unchanged, even when its contents already match the request.
- Traversal, absolute outside paths and symlinks receive the established boundary refusal.
- A failed or interrupted call never exposes a partial target file; resume never overwrites a user change.
- Every tool call has a matching result, including refusals and recovered calls.

Workspace selection and resume validation continue in [Session 14](session-14.md). The fresh-environment walkthrough and real-project demo belong to [Session 15](session-15.md).

## Evidence

`tests/test_write_file.py` exercises creation, existing paths, a racing writer,
boundary refusals, oversized and invalid content, journal intent and replay
on both sides of publication, plus the one-shot CLI. `tests/test_chat.py`
exercises creation in the interactive session. The full offline suite passed:
652 tests.

A hard process kill while the staging file is being written can leave a hidden
`.agent-edit-*` file in the workspace. The target path is never published with
partial content; the staging file can be inspected and removed separately.
