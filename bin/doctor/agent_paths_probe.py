"""Doctor probe: do ANY agent's m3 config point at a dead path?

Relocating the m3 install (repo/venv/bin moving) silently breaks every
path-based agent integration — the config keeps a hardcoded interpreter/bridge/
PYTHONPATH that no longer exists, so m3 stops loading in that host with no error.
This probe scans ALL wired hosts, not just Claude Code:

  * mcpServers-schema hosts (Gemini, Antigravity, ...) — via the installer's own
    _scan_agent_configs(), which flags a dead command/args/env path.
  * OpenCode — a DIFFERENT schema (~/.config OR %APPDATA% opencode.json,
    `mcp.memory` with a LIST command), not covered by the mcpServers scan.
  * Hermes — a venv `.pth` that puts m3's bin/ on PYTHONPATH; if that dir is
    gone, m3client.py can't import the catalog.

Report-only: it names each dead-path host and the fix (`m3 setup` re-wires and
self-heals them). It does NOT mutate config here — healing lives in the wizard /
`m3 doctor --fix` via the installer, which owns the canonical write path. Bumps
the exit code, because a dead-path host is a real silent breakage (m3 not loading
where the user expects it), unlike a merely-lagging plugin version.

Cross-platform, best-effort, never raises.
"""
from __future__ import annotations

import glob
import os
from pathlib import Path


def _path_dead(value: object) -> bool:
    """True if value looks like an absolute-ish path to a missing file/dir. A
    bare console-script name ('m3', 'python') has no separator and is never dead."""
    if not isinstance(value, str) or not value:
        return False
    if not (("/" in value) or ("\\" in value) or value.endswith(".py")):
        return False
    return not Path(os.path.expandvars(os.path.expanduser(value))).exists()


def _scan_mcpservers_hosts() -> list[tuple[str, str, bool]]:
    """(label, path, dead) for the mcpServers-schema hosts, via the installer's
    single source of truth so the probe and `--fix` cover the same set."""
    try:
        import sys
        sys.path.insert(0, os.path.join(_payload_bin(), "..", "m3_memory"))
        from m3_memory.installer import _scan_agent_configs
        return [(lbl, str(p), dead) for (lbl, p, dead) in _scan_agent_configs()]
    except Exception:  # noqa: BLE001 — installer unavailable: skip this class, others still run
        return []


def _payload_bin() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _opencode_configs() -> list[Path]:
    paths = [Path.home() / ".config" / "opencode" / "opencode.json"]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "opencode" / "opencode.json")
    return [p for p in paths if p.is_file()]


def _scan_opencode() -> list[tuple[str, str, bool]]:
    """OpenCode uses mcp.memory with a LIST command — its own schema."""
    import json
    out = []
    for p in _opencode_configs():
        try:
            data = json.loads(p.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            out.append(("OpenCode", str(p), True))
            continue
        entry = (data.get("mcp") or {}).get("memory")
        if entry is None:
            continue
        cmd = entry.get("command") if isinstance(entry, dict) else None
        parts = cmd if isinstance(cmd, list) else [cmd]
        dead = any(_path_dead(x) for x in parts)
        out.append(("OpenCode", str(p), dead))
    return out


def _scan_hermes() -> list[tuple[str, str, bool]]:
    """Hermes puts m3's bin/ on PYTHONPATH via a venv `.pth`; a dead dir there
    means m3client.py can't import the catalog."""
    out = []
    roots = [
        Path.home() / "AppData" / "Local" / "hermes",
        Path.home() / ".hermes",
        Path.home() / ".local" / "share" / "hermes",
    ]
    seen = set()
    for root in roots:
        if not root.exists():
            continue
        for pth in glob.glob(str(root / "**" / "m3-memory-bin.pth"), recursive=True):
            if pth in seen:
                continue
            seen.add(pth)
            try:
                line = Path(pth).read_text(encoding="utf-8").strip().splitlines()[0].strip()
            except (OSError, IndexError):
                out.append(("Hermes", pth, True))
                continue
            out.append(("Hermes", pth, _path_dead(line)))
    return out


def run(brief: bool = False) -> int:
    rows = _scan_mcpservers_hosts() + _scan_opencode() + _scan_hermes()
    dead = [(lbl, path) for (lbl, path, is_dead) in rows if is_dead]

    if brief:
        if not rows:
            print("agent paths: no wired agent configs found")
        elif dead:
            hosts = ", ".join(sorted({lbl for lbl, _ in dead}))
            print(f"⚠️  agent paths: {len(dead)} dead-path config(s) [{hosts}] — run `m3 setup`")
        else:
            print(f"✅ agent paths: OK ({len(rows)} wired host(s), no dead paths)")
        return 1 if dead else 0

    print()
    print("=== agent integration paths ===")
    if not rows:
        print("  status : no agent has m3 wired here (nothing to check).")
        return 0
    for lbl, path, is_dead in rows:
        mark = "DEAD" if is_dead else "ok"
        print(f"  [{mark:>4}] {lbl}: {path}")
    if dead:
        print()
        print(f"  status : [FAIL] {len(dead)} config(s) point at a moved/deleted path — m3")
        print("           will silently not load in those hosts. This happens when the")
        print("           m3 repo/venv/bin is relocated after the agent was wired.")
        print("  fix    : re-run `m3 setup` — it self-heals stale agent configs (repoints")
        print("           them to the current install). For Hermes, re-run the Hermes")
        print("           wiring step so its .pth points at the live bin/.")
    else:
        print("  status : OK — every wired host points at a live m3 install.")
    return 1 if dead else 0
