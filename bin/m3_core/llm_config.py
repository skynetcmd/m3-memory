"""LLM endpoint switches that headless daemons can read.

Hazard: launchd and systemd start the cognitive loop with a bare environment, so
a switch kept only in the user's shell rc (e.g. M3_ENABLE_OLLAMA_FAILOVER) never
reaches it. ``<config_root>/.llm_config.json`` resolves the same way under every
launcher. Precedence: environment > this file > the caller's default.
"""
from __future__ import annotations

import json
import logging
import os

from m3_core.paths import get_m3_config_root

logger = logging.getLogger("M3_SDK")

LLM_CONFIG_NAME = ".llm_config.json"


def llm_config_path() -> str:
    return os.path.join(get_m3_config_root(), LLM_CONFIG_NAME)


def read_llm_config() -> dict:
    """The file's settings, or {} when absent. A malformed file is reported,
    because the daemon would otherwise run without the user's switches."""
    path = llm_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("observed: %s is unreadable (%s: %s); LLM endpoint switches "
                       "fall back to the environment. inspect: that file",
                       path, type(e).__name__, e)
        return {}
    return data if isinstance(data, dict) else {}


def llm_setting(name: str) -> "str | None":
    """The environment value of ``name`` if set, else the config file's."""
    env = os.environ.get(name, "").strip()
    if env:
        return env
    value = read_llm_config().get(name)
    return None if value is None else str(value).strip()


def write_llm_setting(name: str, value: str) -> str:
    """Record ``name=value`` in the config file (atomic replace). Returns the path."""
    path = llm_config_path()
    data = read_llm_config()
    data[name] = value
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
    return path
