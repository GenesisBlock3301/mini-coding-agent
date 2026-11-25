"""Tool schemas, validation, and dispatch."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import MAX_CREATE_BYTES, ToolResult, _tool_error
from .execution import run_python, run_tests
from .filesystem import list_files, list_folders, read_file, replace_text, write_file

# --------------------------------------------------------------------------
# The menu the model sees, and the table the dispatcher uses.
#
# Pulled forward from Session 4 (steps 1-3) so the tools can be exercised from
# the CLI before the loop exists. Session 4 decides where this really belongs
# (D21/D22); until then it lives beside the functions it describes.
# --------------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List the entries of one directory inside the workspace. "
                "Names ending '/' are directories; names ending '@' are "
                "symlinks and cannot be read, listed or edited; everything "
                "else is a file. Does not recurse. Paths are relative to the "
                "workspace root; absolute paths and paths outside the "
                "workspace are refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory, e.g. 'src' or '.'",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_folders",
            "description": (
                "List only the subdirectories of one directory inside the "
                "workspace, skipping files. Use this instead of list_files "
                "when you only need to know where to go next, especially in "
                "a directory with many files. Symlinks are never listed here, "
                "even when they point at a directory, because they cannot be "
                "listed. Does not recurse. Paths are relative to the workspace "
                "root; absolute paths and paths outside the workspace are "
                "refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory, e.g. 'src' or '.'",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read one UTF-8 text file inside the workspace. Long files are "
                "truncated. Paths are relative to the workspace root; absolute "
                "paths and paths outside the workspace are refused."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file, e.g. 'src/app.py'",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_text",
            "description": (
                "Replace exactly one occurrence of 'old' with 'new' in one "
                "file inside the workspace. The text in 'old' must appear "
                "EXACTLY ONCE: if it appears zero times or more than once, "
                "nothing is changed and you are told which. Copy 'old' "
                "verbatim from read_file output, including indentation, and "
                "include enough surrounding lines to identify one place. "
                "Edits existing files only; it cannot create one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file, e.g. 'src/app.py'",
                    },
                    "old": {
                        "type": "string",
                        "description": (
                            "The exact text to replace. Must occur exactly once."
                        ),
                    },
                    "new": {
                        "type": "string",
                        "description": "The text to put in its place.",
                    },
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create one new UTF-8 text file inside the workspace. "
                "Refuses an existing file and does not create parent directories. "
                "Use replace_text to edit an existing file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative new file, e.g. 'src/new.py'"},
                    "content": {"type": "string", "description": "Complete UTF-8 text for the new file"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run one Python (.py) file inside the workspace with the current "
                "Python interpreter. No shell or extra arguments are accepted. "
                "Execution is limited to 30 seconds and output to 8000 bytes. "
                "Only run code the user asked to execute and trusts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative Python file, e.g. 'main.py'"},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the workspace's whole unittest suite (python -m unittest discover, "
                "starting in tests/ if it exists). Takes no arguments. Call it after every "
                "change you make, and report whether it passed. Finding no tests is a "
                "failure to verify, not a pass. Limited to 30 seconds."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

TOOLS = {
    "list_files": list_files,
    "list_folders": list_folders,
    "read_file": read_file,
    "replace_text": replace_text,
    "write_file": write_file,
    "run_python": run_python,
    "run_tests": run_tests,
}

# Session 8, D73. Which of those tools can change the workspace.
#
# Not a flag ON the tool and not a new tool behaviour - docs/session-08.md D
# rules both out. It is a fact ABOUT the catalogue, and the one caller is the
# journal: a run that dies between `tool_intent` and `tool_outcome` has to
# know whether the call it never answered could have written something.
#
# _check_registries() refuses a writing tool that does not take 'path',
# because agent/replay.py identifies the file at risk by that argument and a
# writer named any other way would be fingerprinted as `no-path` - which
# reads as "nothing to protect" and is exactly the silent wrong answer.
WRITES = frozenset({"replace_text", "write_file", "run_python"})


# --------------------------------------------------------------------------
# D41. The schemas are the single source of truth for arguments.
#
# Until Session 6 the dispatcher hardcoded `{"path"}`, which was honest while
# every tool took exactly one argument of that name. replace_text takes three,
# and the choice was: write the argument list down a second time, or read the
# one already written down for the model.
#
# The second, because the failure mode of the first is specific and nasty - the
# schema and the validator drift apart, and the tool then rejects an argument
# the model was TOLD to send. The model cannot debug that; it can only retry.
#
# The cost is that this is an interpreter for a subset of JSON Schema, and
# interpreters grow. So the subset is enforced: anything outside it raises at
# IMPORT time, where it is a traceback during development, rather than at call
# time, where it would be a tool that quietly stopped validating.
# --------------------------------------------------------------------------

_JSON_TYPES: dict[str, type] = {"string": str}

_SUPPORTED_SCHEMA_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties"}
)


@dataclass(frozen=True, slots=True)
class _ArgSpec:
    """One tool's arguments, as read from its schema."""

    properties: dict[str, type]
    descriptions: dict[str, str]
    required: tuple[str, ...]


def _schema_properties(name: str, properties: dict) -> tuple[dict, dict]:
    """(python types, descriptions) for one schema's properties.

    An unsupported `type` raises rather than being skipped: a property this
    validator cannot check would otherwise pass through unvalidated, which is
    the one outcome worse than not having a validator at all.
    """
    types: dict[str, type] = {}
    descriptions: dict[str, str] = {}
    for argument, spec in properties.items():
        json_type = spec.get("type")
        if json_type not in _JSON_TYPES:
            raise RuntimeError(f"{name}.{argument}: unsupported schema type {json_type!r}")
        types[argument] = _JSON_TYPES[json_type]
        descriptions[argument] = spec.get("description", "")
    return types, descriptions


def _arg_spec_for(name: str, params: dict) -> _ArgSpec:
    """One tool's _ArgSpec, or a RuntimeError naming what is wrong with it."""
    unsupported = set(params) - _SUPPORTED_SCHEMA_KEYS
    if unsupported:
        raise RuntimeError(
            f"{name}: schema uses {sorted(unsupported)}, which this validator "
            "does not implement. Add it to _arg_spec_for or do not use it."
        )
    if params.get("type") != "object":
        raise RuntimeError(f"{name}: parameters must be a JSON object schema")
    if params.get("additionalProperties") is not False:
        # The validator's whole closed-world assumption. A schema that allows
        # extra properties would be telling the model one thing while
        # dispatch refuses them.
        raise RuntimeError(f"{name}: schema must set additionalProperties=False")

    properties, descriptions = _schema_properties(name, params["properties"])

    required = tuple(params.get("required", ()))
    unknown_required = set(required) - set(properties)
    if unknown_required:
        raise RuntimeError(f"{name}: required names {sorted(unknown_required)}")

    return _ArgSpec(
        properties=properties, descriptions=descriptions, required=required
    )


def _build_arg_specs() -> dict[str, _ArgSpec]:
    """Index TOOL_SCHEMAS by name, rejecting anything the validator cannot honour.

    Every assertion here is about a mistake made while EDITING THIS FILE, not
    about anything a model can send. That is why they raise instead of
    returning a ToolResult: there is no model to apologise to, and a half-
    validated tool is worse than an import that fails loudly.
    """
    specs: dict[str, _ArgSpec] = {}

    for entry in TOOL_SCHEMAS:
        function = entry["function"]
        name = function["name"]
        if name in specs:
            raise RuntimeError(f"two schemas both named {name!r}")
        specs[name] = _arg_spec_for(name, function["parameters"])

    return specs


_ARG_SPECS = _build_arg_specs()


def _check_registries() -> None:
    """TOOLS and TOOL_SCHEMAS are two tables keyed by the same names.

    They are kept separate on purpose - one answers "does this tool exist",
    the other "what does it take" - but they are never allowed to disagree. A
    schema with no implementation would answer `unknown_tool` to a tool the
    model was shown; an implementation with no schema would be unreachable.

    The signature check is the price of calling tools as `TOOLS[name](**args)`:
    that makes the PYTHON parameter names part of the contract, silently. This
    turns that into an import-time failure instead of a runtime TypeError.
    """
    import inspect

    unknown_writers = WRITES - set(TOOLS)
    if unknown_writers:
        raise RuntimeError(f"WRITES names tools that do not exist: {sorted(unknown_writers)}")

    missing_schema = set(TOOLS) - set(_ARG_SPECS)
    missing_impl = set(_ARG_SPECS) - set(TOOLS)
    if missing_schema or missing_impl:
        raise RuntimeError(
            f"TOOLS and TOOL_SCHEMAS disagree: no schema for "
            f"{sorted(missing_schema)}, no implementation for {sorted(missing_impl)}"
        )

    for name, function in TOOLS.items():
        parameters = inspect.signature(function).parameters
        declared = {
            argument
            for argument, parameter in parameters.items()
            if parameter.kind
            in (parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY)
            and argument != "root"
        }
        if declared != set(_ARG_SPECS[name].properties):
            raise RuntimeError(
                f"{name}: schema says {sorted(_ARG_SPECS[name].properties)}, "
                f"the function takes {sorted(declared)}"
            )

        # D73. A writing tool whose target is not called 'path' would be
        # recorded with no fingerprint and replayed blind after a crash.
        if name in WRITES and "path" not in _ARG_SPECS[name].properties:
            raise RuntimeError(
                f"{name} writes but takes no 'path' argument, so a crashed "
                "run cannot tell which file it was about to change "
                "(agent/replay.py)"
            )


_check_registries()


def _argument_sentence(spec: _ArgSpec) -> str:
    """Name a tool's arguments, in the schema's order.

    Singular and plural are separate sentences because "Its only argument is
    'path', 'old', 'new'" is the kind of generated prose that makes a model
    distrust the rest of the message.
    """
    names = ", ".join(repr(argument) for argument in spec.properties)
    if len(spec.properties) == 1:
        return f"Its only argument is {names}."
    return f"Its arguments are {names}."


def dispatch(name: str, arguments: str, *, root: Path) -> ToolResult:
    """Run one tool call from the model. Returns a ToolResult for everything.

    The caller here is a language model, which emits plausible text rather than
    valid function calls. Four things arrive broken in practice, and each must
    come back as a result the model can act on - never as an exception:

        an unknown tool name          -> unknown_tool
        arguments that are not JSON   -> invalid_arguments
        a missing or wrong-typed one  -> invalid_arguments
        an argument we never defined  -> invalid_arguments

    Since Session 6 the argument rules are READ FROM THE SCHEMA (D41) rather
    than hardcoded here, so this function no longer knows what any tool takes.

    The unknown_tool message names the tools that DO exist. A model told only
    "unknown tool" guesses again; a model shown the menu picks from it.
    """
    if name not in TOOLS:
        available = ", ".join(sorted(TOOLS))
        return _tool_error(
            "unknown_tool",
            f"No tool named {name!r}. Available tools: {available}.",
        )

    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        return _tool_error(
            "invalid_arguments",
            f"The arguments for {name} were not valid JSON ({exc.msg}). "
            'Send an object like {"path": "src"}.',
        )

    if not isinstance(parsed, dict):
        return _tool_error(
            "invalid_arguments",
            f"The arguments for {name} must be a JSON object, not "
            f"{type(parsed).__name__}.",
        )

    spec = _ARG_SPECS[name]

    # ORDER MATTERS, and it is a decision rather than an accident.
    #
    # An object can be wrong in two ways at once: {"recursive": true} both
    # invents an argument and omits a required one. Unexpected wins, because
    # it names the model's ACTUAL mistake - it invented something - whereas
    # leading with "needs a string 'path'" would send it to fix the wrong
    # thing. (This is also what tests/test_list_folders.py already asserts.)
    #
    # Whatever the choice, it must be deterministic: same arguments, same
    # message, every run. Session 11 compares runs.
    unexpected = set(parsed) - set(spec.properties)
    if unexpected:
        return _tool_error(
            "invalid_arguments",
            f"{name} does not take {', '.join(sorted(unexpected))}. "
            f"{_argument_sentence(spec)}",
        )

    # Missing and wrong-type are ONE check, walked in the schema's own order.
    # Two separate loops would report "missing 'old'" for one call and "'old'
    # is a number" for another, which is the same mistake told two ways.
    for argument in spec.required:
        value = parsed.get(argument)
        if not isinstance(value, spec.properties[argument]):
            got = "nothing" if argument not in parsed else type(value).__name__
            hint = spec.descriptions.get(argument, "")
            return _tool_error(
                "invalid_arguments",
                f"{name} needs a string {argument!r} argument; got {got}."
                + (f" {hint}" if hint else ""),
            )

    # D41's consequence: the Python parameter names are part of the contract.
    # _check_registries() proved at import that they match the schema.
    return TOOLS[name](**parsed, root=root)
