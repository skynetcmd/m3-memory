"""Autonomous cognitive-loop write toggles, resolved from a config FILE (not env).

The cognitive loop runs under launchd / systemd / schtasks — none of which
inherit the operator's shell environment (the shipped units carry no Environment
block). So an env var like ``M3_CONSOLIDATION_AUTO=1`` exported in a shell is
simply ABSENT in the daemon process, leaving every autonomous write pass
permanently dry-run. This mirrors the governor, which was migrated off env vars
to a config file for exactly this reason (DESIGN §3: a knob a headless daemon
must read belongs in a code-resolved config file, not an env var).

Precedence per flag: config file > env var > default. A missing file falls
through to env+default silently (expected before first seed); a malformed file
warns loudly rather than silently reverting.
"""

import json
import logging
import os

from m3_core.paths import get_m3_config_root

logger = logging.getLogger("M3_SDK")

_AUTONOMY_CONFIG_NAME = ".cognitive_loop_config.json"

# flag config-key -> the legacy env var it supersedes.
_FLAG_ENV = {
    "consolidation_auto": "M3_CONSOLIDATION_AUTO",
    "chatlog_prune_auto": "M3_CHATLOG_PRUNE_AUTO",
}

_AUTONOMY_TEMPLATE_COMMENT = (
    "Cognitive-loop autonomous-write toggles. Read live by the cognitive loop "
    "(launchd/systemd/schtasks daemons do NOT inherit shell env, so these must "
    "live in a file, not an env var). Set a flag to true to let that pass write "
    "for real; false (default) keeps it dry-run. Edit and save; the next pass "
    "picks it up."
)


def autonomy_config_path() -> str:
    return os.path.join(get_m3_config_root(), _AUTONOMY_CONFIG_NAME)


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def autonomy_flag(config_key: str, env_name: str, default: bool = False) -> bool:
    """Resolve a boolean autonomy flag: config file > env var > default.

    Read live (re-read each call — these gate infrequent background passes, so a
    per-pass stat+parse is negligible and there is no long-lived cache to stale).
    Never raises: a missing file falls through to env+default; a malformed file
    warns loudly (§3) and falls back rather than silently reverting.
    """
    path = autonomy_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f) or {}
        if config_key in cfg:
            return _truthy(cfg[config_key])
    except FileNotFoundError:
        pass  # not yet seeded -> env+default is the intended fallback
    except Exception as e:
        logger.warning(
            "Cognitive-loop autonomy config %s is unreadable/malformed (%s) — "
            "falling back to env var %s + default until fixed.", path, e, env_name)
    return _truthy(os.environ.get(env_name, "1" if default else "0"))


def ensure_autonomy_config() -> str:
    """Create ``<config_root>/.cognitive_loop_config.json`` with all flags at
    their CURRENT effective values if it does not already exist. Idempotent and
    race-safe (atomic create; never overwrites). Returns the path. Never raises —
    a write failure just leaves the loop on env+defaults. Call at setup and once
    at daemon startup; NOT from the per-pass read path.
    """
    path = autonomy_config_path()
    try:
        if os.path.exists(path):
            return path
        payload: dict[str, object] = {"_comment": _AUTONOMY_TEMPLATE_COMMENT}
        for key, env_name in _FLAG_ENV.items():
            payload[key] = _truthy(os.environ.get(env_name, "0"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Atomic create: O_EXCL loses the race harmlessly if a peer seeds first.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            os.close(fd)
            raise
    except FileExistsError:
        pass  # a peer seeded it between the check and the create — fine
    except Exception as e:
        logger.warning("Could not seed autonomy config %s (%s) — "
                       "loop stays on env+defaults.", path, e)
    return path
