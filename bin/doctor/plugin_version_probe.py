"""Doctor probe: is the installed Claude Code plugin current + enabled?

The m3 plugin, the served MCP payload, and the Python package each carry their
own version, and they drift: the plugin manifest can lag the code by weeks, and
`/plugin install` can silently DISABLE the plugin (enabledPlugins -> false), so
m3 vanishes from /mcp with no error. This probe DETECTS both — a stale plugin
version and a disabled-but-installed plugin — and prints the exact fix commands.

It cannot FIX either itself: `/plugin marketplace update`, `/plugin install`,
and `/reload-plugins` are Claude Code CLIENT slash-commands, not things a Python
process can invoke, and editing Claude Code's own config from here would risk a
half-broken state. So this is report-only (like schedule_probe) — it never
mutates ~/.claude, and it does not bump the doctor exit code (a lagging plugin
is a recoverable, user-actionable state, not a broken install).

Cross-platform, network-free (reads local Claude Code config only), never raises.
"""
from __future__ import annotations

import json
import os

PLUGIN_KEY = "m3@skynetcmd"


def _claude_dir() -> str:
    # Claude Code config lives at ~/.claude on every OS.
    return os.path.join(os.path.expanduser("~"), ".claude")


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 — missing/malformed config is "unknown", handled by caller
        return None


def _installed_version() -> str | None:
    """The plugin version Claude Code has installed (from installed_plugins.json)."""
    d = _read_json(os.path.join(_claude_dir(), "plugins", "installed_plugins.json"))
    if not isinstance(d, dict):
        return None
    recs = d.get("plugins", {}).get(PLUGIN_KEY)
    if isinstance(recs, list) and recs:
        return recs[0].get("version")
    if isinstance(recs, dict):
        return recs.get("version")
    return None


def _enabled() -> bool | None:
    """The enabledPlugins flag (True/False), or None if not present."""
    d = _read_json(os.path.join(_claude_dir(), "settings.json"))
    if not isinstance(d, dict):
        return None
    return d.get("enabledPlugins", {}).get(PLUGIN_KEY)


def _latest_cached_version() -> str | None:
    """The newest plugin version present in the local plugin cache — the best
    network-free proxy for 'what an update would install' (a just-fetched
    marketplace update lands a new cache dir here)."""
    cache = os.path.join(_claude_dir(), "plugins", "cache", "skynetcmd", "m3")
    try:
        versions = [d for d in os.listdir(cache)
                    if os.path.isdir(os.path.join(cache, d))]
    except OSError:
        return None
    return max(versions, key=_ver_key) if versions else None


def _marketplace_age_days() -> float | None:
    """Days since the marketplace clone last refreshed, or None if unknown.

    `_latest_cached_version` can only ever see versions the marketplace clone has
    already fetched, so a clone that never refreshes makes this probe report
    "current" forever while main is releases ahead (observed 2026-07-22: plugin
    pinned at 2026.7.13.0 / commit bb0e0ce, clone last fetched four days earlier,
    doctor said OK). The refresh timestamp is the missing signal — WITHOUT
    touching the network, which doctor must never do.

    Prefer known_marketplaces.json's `lastUpdated` (Claude Code's own record of
    the refresh); fall back to the clone's .git/FETCH_HEAD mtime.
    """
    entry = None
    d = _read_json(os.path.join(_claude_dir(), "plugins", "known_marketplaces.json"))
    if isinstance(d, dict):
        entry = d.get("skynetcmd")

    if isinstance(entry, dict):
        raw = entry.get("lastUpdated")
        if isinstance(raw, str) and raw:
            try:
                from datetime import datetime, timezone
                ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
            except (ValueError, TypeError):
                pass

    # Fallback: the clone's own fetch marker.
    loc = entry.get("installLocation") if isinstance(entry, dict) else None
    if not loc:
        loc = os.path.join(_claude_dir(), "plugins", "marketplaces", "skynetcmd")
    try:
        from datetime import datetime, timezone
        mtime = os.path.getmtime(os.path.join(loc, ".git", "FETCH_HEAD"))
        ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    except OSError:
        return None


# Days before the marketplace clone is worth mentioning. Generous on purpose:
# this is an FYI, not a fault, and an offline/air-gapped box must never be nagged
# into chasing a network it doesn't have.
_MARKETPLACE_STALE_DAYS = 14.0


def _ver_key(v: str) -> tuple[tuple[int, int | str], ...]:
    """Sort key for dotted numeric versions (2026.7.4.0, 3.7.4, ...)."""
    parts: list[tuple[int, int | str]] = []
    for p in str(v).split("."):
        try:
            parts.append((0, int(p)))
        except ValueError:
            parts.append((1, p))  # non-numeric sorts after numeric
    return tuple(parts)


def _package_version() -> str | None:
    try:
        import importlib.metadata as m
        return m.version("m3-memory")
    except Exception:  # noqa: BLE001
        return None


def run(brief: bool = False) -> int:
    installed = _installed_version()
    enabled = _enabled()
    latest = _latest_cached_version()
    pkg = _package_version()
    age_days = _marketplace_age_days()

    # A newer version sits in the cache than what's installed -> update available.
    stale = bool(installed and latest and _ver_key(latest) > _ver_key(installed))
    disabled = enabled is False and installed is not None
    # `latest` is bounded by what the clone has fetched, so "installed == latest"
    # only means "current" if the clone is fresh. An old clone makes this probe
    # blind, not healthy — say so rather than claiming currency we can't verify.
    # This is explicitly NOT a fault: an offline/air-gapped install has no way to
    # refresh and is working exactly as intended.
    clone_cold = (
        installed is not None
        and not stale
        and age_days is not None
        and age_days >= _MARKETPLACE_STALE_DAYS
    )
    problem = stale or disabled

    if brief:
        if installed is None:
            print("plugin: not installed via Claude Code (CLI-only / unknown)")
        elif disabled:
            print(f"⚠️  plugin: {installed} but DISABLED — enable it + /reload-plugins")
        elif stale:
            print(f"⚠️  plugin: {installed} installed, {latest} available — run `m3 doctor`")
        elif clone_cold:
            # No ⚠️ — an unrefreshed clone is not a fault (air-gapped installs
            # never refresh), so this stays a plain FYI in the one-line summary.
            print(f"✅ plugin: {installed} (marketplace clone {age_days:.0f}d old — "
                  "version check may be behind upstream)")
        else:
            print(f"✅ plugin: {installed}" + (" (enabled)" if enabled else ""))
        return 0

    print()
    print("=== Claude Code plugin (m3@skynetcmd) ===")
    print(f"  installed : {installed or 'not installed via Claude Code'}")
    print(f"  enabled   : {enabled if enabled is not None else 'unknown'}")
    print(f"  newest in cache : {latest or 'n/a'}")
    if pkg:
        print(f"  m3-memory pkg   : {pkg}")
    if age_days is not None:
        print(f"  marketplace last refreshed : {age_days:.0f}d ago")

    if installed is None:
        print("  status    : m3 is not installed as a Claude Code plugin here (this is")
        print("              fine if you run m3 purely via the CLI / a manual MCP config).")
        return 0

    if disabled:
        print("  status    : [FAIL] installed but DISABLED — m3's tools will NOT load and")
        print("              m3 is absent from /mcp. `/plugin install` can silently flip")
        print("              the enabled flag off on re-install.")
        print("  fix       : 1. edit ~/.claude/settings.json -> enabledPlugins ->")
        print(f"                 set \"{PLUGIN_KEY}\": true")
        print("              2. /reload-plugins")
    if stale:
        print(f"  status    : [NAG] {installed} installed but {latest} is available. Update:")
        print("  fix       : /plugin marketplace update skynetcmd")
        print("              /plugin install m3@skynetcmd")
        print("              /reload-plugins")
        print("              (if m3 then vanishes from /mcp, see the DISABLED fix above —")
        print("               re-install can flip the enabled flag off.)")
    if not problem and clone_cold:
        print("  status    : OK — installed and enabled. Version check is only as")
        print("              fresh as the marketplace clone, last refreshed")
        print(f"              {age_days:.0f}d ago, so a newer release may exist upstream.")
        print("  note      : expected if this machine is offline / air-gapped —")
        print("              nothing is wrong. If it IS networked and you want the")
        print("              latest: /plugin marketplace update skynetcmd")
    elif not problem:
        print("  status    : OK — installed, enabled, and current.")

    # Report-only: a stale/disabled plugin is user-recoverable, not a broken
    # install, so do not bump the doctor exit code.
    return 0
