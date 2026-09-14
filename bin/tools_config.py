"""
tools_config.py — resolver for the MCP startup tool set.

Which tools are registered at startup is user-owned. The full catalog costs
~33,686 tokens of schema if everything loads eagerly; the shipped default is
~2,216. This module decides which names make up that set.

Resolution order (highest first):
    1. M3_TOOLS_STARTUP env var — comma-separated tool names, explicit override
    2. .tools_config.json  ->  startup_tools: [...]
    3. Derived default — the shipped set (slim variants + escape hatch)
    4. M3_TOOLS_LAZY=0 — register EVERYTHING, the pre-slim legacy behavior

Level 4 is checked first in practice because it is a global off-switch: if a
user disables lazy loading they want every tool, and a stale startup_tools list
must not narrow that back down.

Zero dependency on memory_core, memory_bridge, or mcp_tool_catalog. Safe to
import from any module in bin/ without creating a cycle — memory_bridge imports
THIS at startup, so an import back would deadlock the catalog build. The
consequence is deliberate: validating a name against the real catalog is the
CALLER's job (see `validate_against_catalog`), because only the caller can
import the catalog without inverting the dependency.

## Why the default is derived in code and never written to disk

Materializing today's default into a config file at install FREEZES it. The user
upgrades m3, ships with a better default set, and their stale file silently
overrides it — they keep the old surface forever and nothing says so. That is
the section 3 silent-failure shape. Absent config means "use whatever this
version ships", which is the behavior an upgrading user expects.

`write_starter_config()` exists for an explicit `--init` request and stamps a
`_comment` into the file saying it now overrides future upgrade defaults.

## Why an unknown tool name is fatal

Section 3, and the precedent at m3_memory/cli.py where unknown `--json` keys are
rejected with difflib suggestions *specifically because* near-miss keys used to
be discarded silently. A typo in startup_tools, or a tool renamed by an upgrade,
would otherwise hand the user a session quietly missing its most-used tool —
and the agent, seeing no such tool, concludes the capability does not exist and
routes around it.
"""
from __future__ import annotations

import difflib
import json
import logging
import os

logger = logging.getLogger("tools_config")

CONFIG_FILENAME = ".tools_config.json"

# The escape hatch. Without these three, a narrowed startup set leaves an agent
# with no route to anything outside it: m3_call invokes any catalog tool by name,
# and the two meta-tools discover and register whole domains on demand. A config
# that omits them does not produce a smaller surface, it produces a DEAD one,
# and every call falls back to Bash (section 12a).
ESCAPE_HATCH: tuple[str, ...] = (
    "m3_call",
    "tools_list_domains",
    "tools_load_domain",
)

# The shipped startup set: narrowed variants of the highest-traffic tools, plus
# the tools that reach everything else. chatlog_status is here in FULL because
# it has no slim variant — its only parameters are the universally-injected
# ones, so a slim variant measured larger than the original.
DEFAULT_STARTUP_TOOLS: tuple[str, ...] = (
    "memory_search_slim",
    "memory_write_slim",
    "chatlog_search_slim",
    "memory_supersede_slim",
    "chatlog_status",
    *ESCAPE_HATCH,
)

ENV_STARTUP = "M3_TOOLS_STARTUP"
ENV_LAZY = "M3_TOOLS_LAZY"


class ToolsConfigError(ValueError):
    """A config that cannot be honored as written.

    Raised rather than warned-and-ignored: every path that produces this means
    the user asked for a startup set we cannot deliver, and silently giving them
    a different one is the failure mode this module exists to prevent.
    """


def config_path() -> str:
    """Absolute path to .tools_config.json in the m3 config root.

    Resolved lazily rather than at import so a test (or a caller that sets
    M3_CONFIG_ROOT after import) sees the right root.
    """
    root = os.environ.get("M3_CONFIG_ROOT")
    if not root:
        m3_root = os.environ.get("M3_MEMORY_ROOT")
        root = os.path.join(m3_root, "config") if m3_root else \
            os.path.join(os.path.expanduser("~"), ".m3", "config")
    return os.path.join(root, CONFIG_FILENAME)


def _load_file() -> dict:
    """Read the config file. A malformed file WARNS and is ignored.

    Deliberately softer than an unknown tool name: a truncated or hand-edited
    file is an accident with an obvious fix, and refusing to start m3 over it
    would be worse than falling back to the shipped default. An unknown NAME is
    different — it means the user asked for something specific and would get
    something else without being told.
    """
    path = config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to read %s: %s — using the shipped default.", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object; ignoring.", path)
        return {}
    return data


def _split_env(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


def _lazy_disabled() -> bool:
    """True when the user asked for the whole catalog (M3_TOOLS_LAZY=0)."""
    return os.environ.get(ENV_LAZY, "").strip() in ("0", "false", "no", "off")


def resolve_startup_tools() -> tuple[list[str] | None, str]:
    """Resolve the startup set.

    Returns (tool_names, source). `tool_names is None` means "register
    everything" — the level-4 legacy behavior — which is distinct from an empty
    list and must not be conflated with one.

    `source` names the precedence level that won, so `m3 doctor` can report
    where the set came from. A resolved set the user cannot explain is a set
    they cannot fix (section 3: observable, not guessed).
    """
    if _lazy_disabled():
        return None, f"{ENV_LAZY}=0 (all tools)"

    raw_env = os.environ.get(ENV_STARTUP, "").strip()
    if raw_env:
        names = _split_env(raw_env)
        if not names:
            raise ToolsConfigError(
                f"{ENV_STARTUP} is set but lists no tools. Unset it to use the "
                f"shipped default, or name at least one tool."
            )
        return _with_escape_hatch(names), f"{ENV_STARTUP} env"

    data = _load_file()
    if "startup_tools" in data:
        names = data["startup_tools"]
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise ToolsConfigError(
                f"{config_path()}: startup_tools must be a list of tool names."
            )
        if not names:
            raise ToolsConfigError(
                f"{config_path()}: startup_tools is empty. An empty startup set "
                f"leaves no way to reach any tool. Remove the key to use the "
                f"shipped default."
            )
        return _with_escape_hatch(names), config_path()

    return list(DEFAULT_STARTUP_TOOLS), "shipped default"


def _with_escape_hatch(names: list[str]) -> list[str]:
    """REFUSE a set that omits the escape hatch.

    Forcing the missing tools in would be friendlier and was considered; refusing
    is more honest about what the user asked for. A silently-amended config means
    the file on disk no longer describes the running system, and the next reader
    — human or agent — is working from a document that is wrong. The error names
    exactly what to add.
    """
    missing = [t for t in ESCAPE_HATCH if t not in names]
    if missing:
        raise ToolsConfigError(
            "Startup set omits the escape hatch: " + ", ".join(missing) + ".\n"
            "  Without these there is no route to any tool outside the startup "
            "set, and every call falls back to a shell.\n"
            "  Add them to startup_tools (they cost ~757 tokens and are what "
            "makes a small startup set usable)."
        )
    # Order-preserving dedup: a hand-edited list often repeats a name, and a
    # duplicate registration is a confusing error far from its cause.
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def validate_against_catalog(names: list[str], catalog_names: set[str]) -> None:
    """Fail LOUD on a name the catalog does not have.

    The caller passes the catalog's names because this module must not import
    mcp_tool_catalog (cycle). Mirrors the --json unknown-key handling in
    m3_memory/cli.py, including difflib suggestions: a near-miss like
    `memory_search_slm` should say so rather than silently vanish from the
    session and leave the agent concluding the capability is gone.
    """
    unknown = sorted(set(names) - catalog_names)
    if not unknown:
        return
    lines = ["Unknown tool name(s) in the startup set: "
             + ", ".join(repr(n) for n in unknown)]
    for name in unknown:
        near = difflib.get_close_matches(name, sorted(catalog_names), n=1, cutoff=0.6)
        if near:
            lines.append(f"  did you mean {near[0]!r} instead of {name!r}?")
    lines.append(
        "  A tool renamed by an upgrade shows up here. Fix the name, or remove "
        "the key to take this version's default."
    )
    raise ToolsConfigError("\n".join(lines))


def write_starter_config(path: str | None = None,
                         names: list[str] | None = None) -> str:
    """Write a starter .tools_config.json. Only ever on EXPLICIT request.

    Never called at install. The `_comment` is not decoration: a user who opts
    into a file needs to know it pins their startup set against future upgrades,
    which is the whole reason the default is not materialized automatically.
    """
    target = path or config_path()
    payload = {
        "_comment": (
            "Pins the MCP startup tool set. NOTE: while this file exists it "
            "OVERRIDES the defaults shipped by future m3 upgrades — delete it "
            "to go back to tracking them. The escape hatch (m3_call, "
            "tools_list_domains, tools_load_domain) is required."
        ),
        "startup_tools": list(names or DEFAULT_STARTUP_TOOLS),
    }
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)  # atomic, like chatlog_config.save_config
    return target
