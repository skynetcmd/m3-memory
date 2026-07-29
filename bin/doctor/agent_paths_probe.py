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


def _current_payload_bin() -> "str | None":
    """The authoritative payload bin/ (the installed wheel, or a dev checkout) —
    the single directory every wired m3 command should resolve under."""
    try:
        import sys
        sys.path.insert(0, os.path.join(_payload_bin(), "..", "m3_memory"))
        from m3_memory.installer import bin_dir
        bd = bin_dir()
        return str(bd.resolve()) if bd else None
    except Exception:  # noqa: BLE001 — resolver unavailable: skip stale detection
        return None


# m3-owned MCP server names generate_configs writes; only these are ours to judge.
_M3_SERVER_NAMES = frozenset(
    {"memory", "custom_pc_tool", "grok_intel", "web_research", "debug_agent"}
)


def _scan_stale_payload() -> list[tuple[str, str]]:
    """(label, script) for wired m3 commands whose SCRIPT still exists but does
    NOT live under the current payload bin — a ~/.m3/repo clone the upgrade left
    behind. `_path_dead` misses these (the file is present), yet they run OLD
    hook/bridge code. Only flags m3-owned entries (known server names + m3 hook
    markers), never a user's own MCP server."""
    import json

    payload = _current_payload_bin()
    if not payload:
        return []
    # realpath both sides so a symlinked pipx/venv dir (settings holds one form,
    # bin_dir() resolves another) is not a false "stale" — only a genuinely
    # different payload root trips the check.
    payload_real = os.path.realpath(payload).replace("\\", "/").rstrip("/") + "/"

    def _under(script: object) -> bool:
        if not isinstance(script, str) or not script:
            return True  # nothing to judge
        return os.path.realpath(script).replace("\\", "/").startswith(payload_real)

    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.is_file():
        return []
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []

    out: list[tuple[str, str]] = []
    # MCP bridges: the script is args[0].
    for name, srv in (data.get("mcpServers") or {}).items():
        if name not in _M3_SERVER_NAMES or not isinstance(srv, dict):
            continue
        args = srv.get("args") or []
        script = args[0] if args else None
        if isinstance(script, str) and script.endswith(".py") and not _under(script):
            out.append((f"Claude/{name}", script))
    # Hooks + statusline: the m3 script is the second token of the command string.
    def _cmd_script(cmd: object) -> "str | None":
        if not isinstance(cmd, str):
            return None
        parts = cmd.split()
        # <interpreter> <script...>; find the m3 payload token.
        for tok in parts[1:]:
            if ("bin/hooks/chatlog/" in tok.replace("\\", "/")
                    or "bin/statusline-command" in tok.replace("\\", "/")):
                return tok
        return None

    for event, entries in (data.get("hooks") or {}).items():
        for entry in entries or []:
            for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
                s = _cmd_script(h.get("command"))
                if s and not _under(s):
                    out.append((f"Claude/hook:{event}", s))
    sl = (data.get("statusLine") or {}).get("command") if isinstance(data.get("statusLine"), dict) else None
    s = _cmd_script(sl)
    if s and not _under(s):
        out.append(("Claude/statusLine", s))
    return out


def _remediate_stale() -> bool:
    """Rewrite every m3-managed Claude command (memory + aux bridges + hooks +
    statusline) to the CURRENT payload via the canonical writer,
    generate_configs.install_claude_settings. This is what actually fixes the
    stale-clone drift — `m3 setup` does not run it, and _heal_agent_settings only
    touches the memory entry + dead hooks. Returns True if settings changed."""
    try:
        import shutil
        import sys
        import time
        # Back up the user's settings.json first (their non-m3 hooks live there
        # too) — same discipline as the --fix-hooks environment repair.
        settings = Path.home() / ".claude" / "settings.json"
        if settings.is_file():
            bak = settings.with_name(f"settings.json.m3bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(settings, bak)
            print(f"  [fix] backed up settings.json -> {bak}")
        bin_dir = _payload_bin()
        if bin_dir not in sys.path:
            sys.path.insert(0, bin_dir)
        import generate_configs
        res = generate_configs.install_claude_settings(assume_yes=True)
        return bool(res.get("changed"))
    except Exception as e:  # noqa: BLE001 — a failed fix must not crash the doctor
        print(f"  [fix] could not rewrite Claude settings: {type(e).__name__}: {e}")
        return False


def run(brief: bool = False, fix: bool = False) -> int:
    rows = _scan_mcpservers_hosts() + _scan_opencode() + _scan_hermes()
    dead = [(lbl, path) for (lbl, path, is_dead) in rows if is_dead]
    stale = _scan_stale_payload()

    # --fix: auto-repair the Claude stale-payload class via the canonical writer.
    # (Dead paths on other hosts still route through `m3 setup`; only this class
    # is self-healed here.)
    if fix and stale and not dead:
        print("  [fix] rewriting stale Claude commands to the current payload …")
        if _remediate_stale():
            stale = _scan_stale_payload()  # re-scan so the verdict reflects reality

    if brief:
        if not rows and not stale:
            print("agent paths: no wired agent configs found")
        elif dead:
            hosts = ", ".join(sorted({lbl for lbl, _ in dead}))
            print(f"⚠️  agent paths: {len(dead)} dead-path config(s) [{hosts}] — run `m3 setup`")
        elif stale:
            print(f"⚠️  agent paths: {len(stale)} config(s) point at a STALE payload clone "
                  "(runs old code) — run `m3 doctor --fix --fix-hooks`")
        else:
            print(f"✅ agent paths: OK ({len(rows)} wired host(s), no dead or stale paths)")
        return 1 if (dead or stale) else 0

    print()
    print("=== agent integration paths ===")
    if not rows and not stale:
        print("  status : no agent has m3 wired here (nothing to check).")
        return 0
    for lbl, path, is_dead in rows:
        mark = "DEAD" if is_dead else "ok"
        print(f"  [{mark:>4}] {lbl}: {path}")
    for lbl, script in stale:
        print(f"  [STALE] {lbl}: {script}")
    if dead:
        print()
        print(f"  status : [FAIL] {len(dead)} config(s) point at a moved/deleted path — m3")
        print("           will silently not load in those hosts. This happens when the")
        print("           m3 repo/venv/bin is relocated after the agent was wired.")
        print("  fix    : re-run `m3 setup` — it self-heals stale agent configs (repoints")
        print("           them to the current install). For Hermes, re-run the Hermes")
        print("           wiring step so its .pth points at the live bin/.")
    elif stale:
        print()
        print(f"  status : [FAIL] {len(stale)} config(s) point at a payload clone that is")
        print("           NOT the installed wheel — the file exists but runs OLD hook/")
        print("           bridge code after an upgrade (the classic ~/.m3/repo drift).")
        print("  fix    : run `m3 doctor --fix --fix-hooks` — it backs up settings.json,")
        print("           then rewrites every m3 command (memory, aux bridges, hooks,")
        print("           statusline) to the current payload via the canonical writer,")
        print("           so all servers agree on one fresh copy.")
    else:
        print("  status : OK — every wired host points at the live m3 payload.")
    return 1 if (dead or stale) else 0
