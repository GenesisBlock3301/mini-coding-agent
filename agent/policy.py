"""Workspace boundaries and operation limits. (Session 2+)

Resolves every path against the playground workspace and rejects escapes,
including symlinks. Enforces step, tool-call, output-size, duration and budget
limits. These rules are enforced here in code, never by the prompt.
"""
