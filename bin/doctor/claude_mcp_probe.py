"""Doctor probe: is Claude Code running exactly ONE m3 memory server?

m3 can reach Claude Code two ways, and they COMPETE:

  * the direct server, registered via `claude mcp add --scope user m3_memory m3`
    (written to ~/.claude.json) -> clean `mcp__m3_memory__` namespace. THE DEFAULT.
  * the m3 plugin's own memory server (mcp__plugin_m3_memory__), enabled by
    `/plugin install m3`.

If BOTH are live, Claude loads the m3 MCP server TWICE — double model/payload
load, and the same 100+ tools appear under two prefixes, so the agent picks
inconsistently and chatlog writes can double. `m3 setup` disables the plugin's
server (disabledMcpServers += plugin:m3:memory) so only the direct one runs, but
that is a one-shot write: a LATER `/plugin install m3` re-enables the plugin
server with no disable entry, and nothing else notices. This probe is the thing
that notices.

It also flags two other incorrect states nothing else catches:
  * a legacy roots-less `memory` direct entry (the pre-rename registration) — it
    uses the wrong `mcp__memory__` namespace AND pins no roots (split-brain risk).
  * `memory` and `m3_memory` both registered directly (two direct servers).

REPAIR (only under `m3 doctor --fix --fix-hooks`, mirroring the other probes that
touch the user's own ~/.claude files — bare --fix reports, --fix-hooks writes,
after a timestamped backup):
  * disable the plugin's server (merge into settings.json disabledMcpServers), but
    ONLY when a direct server exists to take over — never leave the user with zero
    m3 servers.
  * `claude mcp remove --scope user memory` to drop the legacy/roots-less entry,
    again only when a replacement (direct m3_memory or the plugin) is live.

Report-only and exit-code-neutral: a double-run still WORKS (just wastefully), and
a user who deliberately prefers the plugin is a supported choice — so this nags
with the exact fix but never fails the doctor. Cross-platform, network-free
(reads local Claude Code config only), never raises.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

# Mirror of m3_memory.setup_wizard._CLAUDE_PLUGIN_SERVER_IDS and
# generate_configs.install_claude_settings section 4. `memory` is the current
# plugin server key; `m3` is the pre-rename key an upgrader may still carry.
_PLUGIN_SERVER_IDS = ("plugin:m3:memory", "plugin:m3:m3")

# The direct registration names m3 uses/used in ~/.claude.json mcpServers.
_DIRECT_NAME = "m3_memory"       # current, roots-pinned, mcp__m3_memory__
_LEGACY_DIRECT_NAME = "memory"   # pre-rename, roots-less, mcp__memory__


def _claude_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude")


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — missing/malformed config is "unknown"
        return None


def _looks_m3(entry: object) -> bool:
    """True if a direct mcpServers entry is one m3 registered (not a foreign
    server that merely shares the name `memory`). m3's server runs the `m3`
    console script, or carries m3's own env pins."""
    if not isinstance(entry, dict):
        return False
    cmd = str(entry.get("command") or "")
    base = os.path.basename(cmd).lower()
    if base in ("m3", "m3.exe") or base.startswith("m3-memory"):
        return True
    args = entry.get("args")
    if isinstance(args, list) and any(
        isinstance(a, str) and ("memory_bridge" in a or "m3_memory" in a) for a in args
    ):
        return True
    env = entry.get("env")
    if isinstance(env, dict) and any(
        k.startswith("M3_") or k == "LLM_ENDPOINTS_CSV" for k in env
    ):
        return True
    return False


def _has_root_pins(entry: object) -> bool:
    env = entry.get("env") if isinstance(entry, dict) else None
    if not isinstance(env, dict):
        return False
    # A non-empty root pin (empty string == unset, treated as default root).
    return bool(env.get("M3_ENGINE_ROOT")) or bool(env.get("M3_CONFIG_ROOT"))


def _direct_servers() -> dict:
    """m3-owned direct registrations from ~/.claude.json, keyed by name."""
    d = _read_json(os.path.join(os.path.expanduser("~"), ".claude.json"))
    servers = d.get("mcpServers") if isinstance(d, dict) else None
    out: dict = {}
    if isinstance(servers, dict):
        for name in (_DIRECT_NAME, _LEGACY_DIRECT_NAME):
            entry = servers.get(name)
            if _looks_m3(entry):
                out[name] = entry
    return out


def _plugin_state() -> dict:
    """Plugin install/enabled/server-disabled state. Reuses plugin_version_probe
    for the install+enabled read so there is one source of truth for that."""
    try:
        from doctor import plugin_version_probe as pv
        installed = pv._installed_version() is not None
        enabled = pv._enabled() is True
    except Exception:  # noqa: BLE001
        installed, enabled = False, False
    settings = _read_json(os.path.join(_claude_dir(), "settings.json"))
    disabled = settings.get("disabledMcpServers") if isinstance(settings, dict) else None
    server_disabled = (
        isinstance(disabled, list) and "plugin:m3:memory" in disabled
    )
    return {
        "installed": installed,
        "enabled": enabled,
        "server_disabled": server_disabled,
        # The plugin server Claude actually launches: installed, plugin enabled,
        # and its server not in disabledMcpServers.
        "active": installed and enabled and not server_disabled,
    }


def _assess() -> dict:
    direct = _direct_servers()
    direct_present = _DIRECT_NAME in direct
    legacy_present = _LEGACY_DIRECT_NAME in direct
    plugin = _plugin_state()

    # Any m3 direct server Claude will launch.
    direct_active = direct_present or legacy_present
    double_run = direct_active and plugin["active"]
    two_direct = direct_present and legacy_present
    legacy_rootless = legacy_present and not _has_root_pins(direct.get(_LEGACY_DIRECT_NAME))

    return {
        "direct": direct,
        "direct_present": direct_present,
        "legacy_present": legacy_present,
        "plugin": plugin,
        "direct_active": direct_active,
        "double_run": double_run,
        "two_direct": two_direct,
        "legacy_rootless": legacy_rootless,
        "problem": double_run or two_direct or legacy_present,
        # No m3 server anywhere — not this probe's job to register (that's setup),
        # but worth an FYI so the user isn't left guessing.
        "none": not direct_active and not plugin["installed"],
    }


def _claude_mcp_remove(name: str) -> tuple[bool, str]:
    """Best-effort `claude mcp remove --scope user <name>`."""
    if not shutil.which("claude"):
        return False, "claude CLI not on PATH"
    try:
        proc = subprocess.run(
            ["claude", "mcp", "remove", "--scope", "user", name],
            check=False, capture_output=True, text=True,
        )
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    ok = proc.returncode == 0 or "no mcp server" in (
        (proc.stdout or "") + (proc.stderr or "")).lower()
    return ok, (proc.stdout or proc.stderr or "").strip()


def _disable_plugin_server_in_settings() -> tuple[bool, str | None, str]:
    """Back up settings.json, then merge the plugin server ids into
    disabledMcpServers. Returns (changed, backup_path, detail)."""
    settings_path = os.path.join(_claude_dir(), "settings.json")
    live = _read_json(settings_path)
    if not isinstance(live, dict):
        live = {}
    disabled = live.get("disabledMcpServers")
    if not isinstance(disabled, list):
        disabled = []
    to_add = [pid for pid in _PLUGIN_SERVER_IDS if pid not in disabled]
    if not to_add:
        return False, None, "plugin server already disabled"
    backup = None
    try:
        if os.path.isfile(settings_path):
            backup = f"{settings_path}.m3-backup-{time.strftime('%Y%m%d-%H%M%S')}"
            shutil.copy2(settings_path, backup)
    except Exception as e:  # noqa: BLE001 — no backup, no write
        return False, None, f"could not back up settings.json ({e}); refused to write"
    disabled.extend(to_add)
    live["disabledMcpServers"] = disabled
    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(live, f, indent=2, sort_keys=True)
    except Exception as e:  # noqa: BLE001
        return False, backup, f"{type(e).__name__}: {e}; settings.json unchanged"
    return True, backup, f"disabled {', '.join(to_add)}"


def _fix(a: dict) -> None:
    """Converge to a single active m3 server. Callers gate this behind
    --fix --fix-hooks (it edits the user's ~/.claude files)."""
    acted = False

    # 1. Stop a double-run by turning the plugin's server off — but only when a
    #    direct server exists to take over (never leave zero m3 servers).
    if a["double_run"] and a["direct_present"]:
        changed, backup, detail = _disable_plugin_server_in_settings()
        status = "ok" if changed else "skip"
        print(f"  [{status}] disable plugin server: {detail}")
        if backup:
            print(f"         settings.json backed up to {backup}")
        acted = acted or changed
    elif a["double_run"] and not a["direct_present"]:
        # Only the legacy `memory` direct entry + the plugin. Prefer the plugin
        # (roots via userConfig) and drop the roots-less legacy entry below.
        pass

    # 2. Drop the legacy/roots-less `memory` direct entry, but only when a
    #    replacement is live (direct m3_memory OR an active plugin server) so we
    #    never remove the user's only working server.
    if a["legacy_present"] and (a["direct_present"] or a["plugin"]["active"]):
        ok, detail = _claude_mcp_remove(_LEGACY_DIRECT_NAME)
        print(f"  [{'ok' if ok else 'error'}] remove legacy `memory` server: "
              f"{detail or 'done'}")
        acted = acted or ok
    elif a["legacy_present"]:
        print("  [skip] legacy `memory` is the only m3 server — not removing it; "
              "run `m3 setup` to migrate it to `m3_memory` (adds root pins).")

    if not acted:
        print("  (nothing to converge — already single-server)")
    else:
        print("  Restart Claude Code for the change to take effect "
              "(server env re-resolves only on restart).")


def _print_fix_hint() -> None:
    print("  fix       : m3 setup                     (recommended — direct "
          "mcp__m3_memory__, plugin server off)")
    print("              m3 doctor --fix --fix-hooks  (converge in place; backs up "
          "~/.claude/settings.json)")
    print("              To keep the PLUGIN instead: `claude mcp remove --scope user "
          "m3_memory` and drop \"plugin:m3:memory\" from disabledMcpServers.")


def run(brief: bool = False, fix: bool = False) -> int:
    a = _assess()

    if fix:
        print()
        print("=== Claude MCP single-server convergence (fix) ===")
        _fix(a)
        return 0

    if brief:
        if a["double_run"]:
            print("⚠️  claude mcp: TWO m3 servers live (direct + plugin) — "
                  "run `m3 doctor --fix --fix-hooks`")
        elif a["two_direct"]:
            print("⚠️  claude mcp: `memory` AND `m3_memory` both registered — "
                  "run `m3 doctor --fix --fix-hooks`")
        elif a["legacy_present"]:
            print("⚠️  claude mcp: legacy `memory` server (wrong namespace / no root "
                  "pins) — run `m3 setup`")
        elif a["direct_present"]:
            print("✅ claude mcp: single direct server (mcp__m3_memory__)")
        elif a["plugin"]["active"]:
            print("✅ claude mcp: single plugin server (mcp__plugin_m3_memory__)")
        elif a["none"]:
            print("claude mcp: no m3 memory server registered (run `m3 setup`)")
        else:
            print("✅ claude mcp: single m3 server")
        return 0

    print()
    print("=== Claude MCP server(s) (one m3 memory server, not two) ===")
    p = a["plugin"]
    print(f"  direct m3_memory : {'yes' if a['direct_present'] else 'no'}")
    print(f"  legacy memory    : {'yes (roots-less)' if a['legacy_rootless'] else ('yes' if a['legacy_present'] else 'no')}")
    print(f"  plugin installed : {p['installed']}  enabled: {p['enabled']}  "
          f"server disabled: {p['server_disabled']}")

    if a["double_run"]:
        _which = "m3_memory" if a["direct_present"] else "legacy `memory`"
        print("  status    : [WARN] TWO m3 servers are live — a direct server "
              f"({_which}) AND")
        print("              the plugin's server. Claude loads the m3 payload twice and")
        print("              the tools split across mcp__m3_memory__ / mcp__memory__ AND")
        print("              mcp__plugin_m3_memory__. A `/plugin install m3` after setup")
        print("              re-enables the plugin server with no disable entry.")
        _print_fix_hint()
    elif a["two_direct"]:
        print("  status    : [WARN] both `memory` and `m3_memory` are registered directly")
        print("              — two direct servers. Keep `m3_memory`; drop the legacy one.")
        _print_fix_hint()
    elif a["legacy_present"]:
        print("  status    : [WARN] only the legacy `memory` server is registered — it")
        print("              uses the wrong mcp__memory__ namespace" +
              (" and pins no roots (split-brain risk)." if a["legacy_rootless"] else "."))
        print("  fix       : m3 setup   (migrates `memory` -> `m3_memory`, pins roots,")
        print("              disables the plugin's competing server)")
    elif a["direct_present"]:
        print("  status    : OK — single direct server (mcp__m3_memory__), plugin server off.")
    elif a["plugin"]["active"]:
        print("  status    : OK — single plugin server (mcp__plugin_m3_memory__). Supported")
        print("              choice; `m3 setup` would switch you to the direct server.")
    elif a["none"]:
        print("  status    : no m3 memory server registered for Claude Code (fine if you")
        print("              run m3 purely via the CLI). `m3 setup` registers one.")
    else:
        print("  status    : OK — single m3 server.")

    # Exit-code-neutral: a double-run still works (wastefully) and a plugin-only
    # setup is a supported choice — nag with the fix, never fail the doctor.
    return 0
