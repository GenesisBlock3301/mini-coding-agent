"""Load optional user configuration from outside the target workspace."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def config_path() -> Path:
    configured = os.getenv("MINI_AGENT_CONFIG_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    base = os.getenv("XDG_CONFIG_HOME")
    directory = Path(base).expanduser() if base else Path.home() / ".config"
    return (directory / "mini-coding-agent" / ".env").resolve()


def load_config() -> None:
    path = config_path()
    if path.is_file():
        load_dotenv(path, override=False)
    # Also support credentials kept beside this source checkout. External
    # config and already-set environment variables take precedence.
    checkout_env = Path(__file__).resolve().parent.parent / ".env"
    if checkout_env != path and checkout_env.is_file():
        load_dotenv(checkout_env, override=False)
