"""Doctor status-section renderers + their read helpers.

Extracted verbatim from installer.py. installer.py re-imports all of these so
`installer.<name>` still resolves (tests reference e.g. installer._roots_section
directly).

ONE CAREFUL CASE: `_resolve_chatlog_db` calls `_developer_bridge`, which is a
monkeypatch target that STAYS in installer.py (tests do
`monkeypatch.setattr(installer, "_developer_bridge", ...)`). A bound
`from m3_memory.installer import _developer_bridge` would capture the original
function object at import time and silently ignore any such patch. So we reach
it via the module object, resolved lazily inside the function body — exactly
the pattern used in m3_memory/wizard/persist.py for `_ask_yes_no`.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Optional

from m3_memory._platform import os_name as _os_name


def _sqlite3_cli_hint() -> Optional[str]:
    """Return a per-OS install instruction if the `sqlite3` CLI is missing.

    Returns None if the CLI is already on PATH (no action needed).
    Purely advisory — we don't run sudo commands on the user's behalf.
    """
    import shutil

    if shutil.which("sqlite3"):
        return None

    system = _os_name()
    if system == "Linux":
        # Try /etc/os-release for the package manager.
        distro = ""
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("ID="):
                    distro = line.split("=", 1)[1].strip().strip('"')
                    break
        except OSError:
            pass
        if distro in ("debian", "ubuntu", "linuxmint", "pop"):
            cmd = "sudo apt install -y sqlite3"
        elif distro in ("fedora", "rhel", "centos", "rocky", "almalinux"):
            cmd = "sudo dnf install -y sqlite"
        elif distro in ("arch", "manjaro"):
            cmd = "sudo pacman -S sqlite"
        else:
            cmd = "install `sqlite3` with your package manager"
        return f"[!] `sqlite3` CLI not found -{cmd}"
    if system == "Darwin":
        # macOS ships sqlite3 in /usr/bin; if absent something's unusual.
        return "[!] `sqlite3` CLI not found (unexpected on macOS) — `brew install sqlite`"
    if system == "Windows":
        return "[!] `sqlite3` CLI not found -`winget install SQLite.SQLite` or download sqlite-tools from https://sqlite.org/download.html"
    return "[!] `sqlite3` CLI not found; install it for ad-hoc DB inspection"


def _resolve_chatlog_db(cfg: dict) -> Optional[Path]:
    """Best-effort resolution of the chatlog DB path.

    Order: CHATLOG_DB_PATH env, M3_DATABASE env (shared DB case), config's
    chatlog_db_path, the DECOUPLED ENGINE ROOT (M3_ENGINE_ROOT / ~/.m3/engine —
    the canonical home for chatlog DBs in the decoupled layout, which is now the
    default), then the legacy <repo_path>/memory/agent_chatlog.db.

    The engine-root check MUST come before the repo-relative fallback: with
    decoupled roots, the chatlog DB lives at <engine>/agent_chatlog.db, NOT under
    the repo. Without this, doctor/`m3 status` read a non-existent repo-relative
    path and falsely report "no captures" even when a real chatlog DB exists in
    the engine root (observed 2026-06-27 on a macOS decoupled-roots install: a
    592MB agent_chatlog.db reported as empty).
    Returns None only when nothing resolves — i.e. the system isn't installed yet.
    """
    from m3_sdk import getenv_compat

    env_chatlog = getenv_compat("M3_CHATLOG_DB_PATH", "CHATLOG_DB_PATH")
    if env_chatlog:
        return Path(env_chatlog).expanduser()
    env_main = os.environ.get("M3_DATABASE")
    if env_main:
        return Path(env_main).expanduser()
    cfg_chatlog = cfg.get("chatlog_db_path")
    if cfg_chatlog:
        return Path(cfg_chatlog).expanduser()
    # Decoupled engine root — the canonical location for chatlog DBs today.
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
        from m3_sdk import get_m3_engine_root
        eng_db = Path(get_m3_engine_root()) / "agent_chatlog.db"
        if eng_db.is_file():
            return eng_db
    except Exception:  # noqa: BLE001 — fall through to the legacy paths below
        pass
    repo = cfg.get("repo_path")
    if repo:
        return Path(repo) / "memory" / "agent_chatlog.db"
    # Developer case: `pip install -e .` from a repo clone. No config file,
    # but the DB lives next to the sibling bridge we already resolve.
    #
    # _developer_bridge is a monkeypatch target that stays in installer.py —
    # reach it via the module object at call time, not a bound import, so a
    # test's monkeypatch.setattr(installer, "_developer_bridge", ...) is honored.
    from m3_memory import installer as _inst
    dev = _inst._developer_bridge()
    if dev:
        candidate = dev.parent.parent / "memory" / "agent_chatlog.db"
        if candidate.exists():
            return candidate
    return None


def _chatlog_db_stats(db_path: Path) -> dict:
    """Open DB read-only, report row counts + last-capture timestamp.

    Returns {ok, rows, last_at, error}. Uses stdlib sqlite3 only — no
    dependency on the bin/ payload so doctor still works if the repo
    clone is incomplete.
    """
    out = {"ok": False, "rows": 0, "last_at": "", "error": ""}
    if not db_path.is_file():
        out["error"] = "file not found"
        return out
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            # Count rows the chatlog subsystem writes — types 'chat_log'
            # and 'message'. Matches chatlog_status's totals so the two
            # tools don't disagree.
            row = conn.execute(
                "SELECT COUNT(*), MAX(created_at) FROM memory_items "
                "WHERE type IN ('chat_log', 'message')"
            ).fetchone()
            out["rows"] = row[0] or 0
            out["last_at"] = row[1] or ""
            out["ok"] = True
        finally:
            conn.close()
    except sqlite3.OperationalError as e:
        # Table may not exist yet (fresh install before first write).
        out["error"] = str(e)
    return out


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _claude_hook_state() -> dict:
    """Detect Stop / PreCompact hooks in ~/.claude/settings.json."""
    settings = _read_json(Path.home() / ".claude" / "settings.json")
    if settings is None:
        return {"configured": False}
    hooks = settings.get("hooks") or {}
    stop = any("chatlog" in json.dumps(h).lower() for h in (hooks.get("Stop") or []))
    pre = any("chatlog" in json.dumps(h).lower() for h in (hooks.get("PreCompact") or []))
    return {"configured": True, "stop": stop, "precompact": pre}


def _gemini_hook_state() -> dict:
    """Detect MCP registration + SessionEnd hook in ~/.gemini/settings.json.

    Gemini CLI uses a single SessionEnd hook (no Stop/PreCompact split).
    Capture works when (a) the memory MCP is registered so in-session
    tool calls work and (b) the SessionEnd hook runs chatlog_ingest on exit.
    """
    settings = _read_json(Path.home() / ".gemini" / "settings.json")
    if settings is None:
        return {"configured": False}
    mcp = (settings.get("mcpServers") or {})
    hooks = settings.get("hooks") or {}
    session_end = any(
        "chatlog" in json.dumps(h).lower()
        for h in (hooks.get("SessionEnd") or [])
    )
    return {
        "configured": True,
        "memory_mcp": "memory" in mcp,
        "session_end": session_end,
    }


def _chatlog_section(cfg: dict) -> int:
    """Emit the chatlog health section. Returns 0 (informational — never
    the reason doctor exits nonzero)."""
    print()
    print("chatlog subsystem:")

    db_path = _resolve_chatlog_db(cfg)
    if db_path is None:
        print("  db_path:                 (unresolved — install-m3 not run yet)")
    else:
        print(f"  db_path:                 {db_path}")
        stats = _chatlog_db_stats(db_path)
        if stats["ok"]:
            last = stats["last_at"] or "(never)"
            print(f"  captured rows:           {stats['rows']}")
            print(f"  last capture at:         {last}")
        elif stats["error"] == "file not found":
            print("  status:                  db not yet created (no captures written)")
        else:
            print(f"  status:                  unreadable ({stats['error']})")

    claude = _claude_hook_state()
    if not claude["configured"]:
        print("  claude hooks:            ~/.claude/settings.json not found")
    else:
        stop_mark = "[on]" if claude["stop"] else "[off]"
        pre_mark = "[on]" if claude["precompact"] else "[off]"
        print(f"  claude hooks:            Stop {stop_mark}  PreCompact {pre_mark}")

    gemini = _gemini_hook_state()
    if not gemini["configured"]:
        print("  gemini mcp:              ~/.gemini/settings.json not found")
    else:
        mcp_mark = "[on]" if gemini["memory_mcp"] else "[off]"
        se_mark = "[on]" if gemini["session_end"] else "[off]"
        print(f"  gemini mcp (memory):     {mcp_mark}  SessionEnd {se_mark}")
    return 0


def _embedder_tier_section() -> None:
    """Emit the 'which embedder tier is live' line (Project Oxidation status).

    Tells the user whether the native in-process embedder (the oxidized hot
    path) is active or whether m3 is on the pure-Python HTTP fallback. Purely
    informational — never changes doctor's exit code. Best-effort: if the
    rust_core_install probe isn't importable, stay silent rather than error.
    """
    try:
        from m3_memory.rust_core_install import active_embedder_tier
        tier = active_embedder_tier()
    except Exception:  # noqa: BLE001 — informational only
        return
    print()
    print("embedder (Project Oxidation):")
    if tier.get("native"):
        print(f"  status:                  {tier['summary']}")
    else:
        # Wrap the longer fallback summary so it stays readable in a terminal.
        print("  status:                  pure-Python fallback")
        for line in tier["summary"].split(" — "):
            print(f"    {line.strip()}")
    _shared_embedder_status()


def _shared_embedder_status() -> None:
    """Report whether m3 is in SHARED-embedder mode (.embed_config.json disables
    the per-process in-process embedder and defers to one shared server), and if
    so, HEALTH-CHECK that server. A shared config pointing at a DEAD endpoint is a
    silent-failure trap (§3): every embed would slow-cascade or fail — so warn
    loudly. Read-only; never changes doctor's exit code. Best-effort."""
    try:
        import json
        import os
        root = os.environ.get("M3_CONFIG_ROOT")
        if not root:
            mem = os.environ.get("M3_MEMORY_ROOT")
            root = (os.path.join(os.path.abspath(os.path.expanduser(mem)), "config")
                    if mem else os.path.join(os.path.expanduser("~"), ".m3", "config"))
        path = os.path.join(root, ".embed_config.json")
        if not os.path.exists(path):
            # Shared mode is the shipped DEFAULT, so its absence is an anomaly to
            # flag, not a neutral state. A per-process fleet means N embedder
            # copies in host RAM and no single self-healed server.
            print("  mode:                    [WARN] per-process — shared mode NOT "
                  "enabled (this is the shipped default)")
            print("    fix: re-run `m3 setup` (enables it automatically), or "
                  "`m3 embedder shared` to enable it now.")
            print("    then register the keep-alive task: "
                  "`python bin/install_schedules.py --add embed-server` (admin shell).")
            return
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f) or {}
    except Exception as e:  # noqa: BLE001 — informational only
        print(f"  mode:                    UNKNOWN (.embed_config.json unreadable: {e})")
        return

    if not cfg.get("disable_inproc_embedder"):
        print("  mode:                    per-process (.embed_config.json present but "
              "in-process embedder not disabled)")
        return

    url = (cfg.get("fallback_url") or "http://127.0.0.1:8082").rstrip("/")
    print(f"  mode:                    SHARED — deferring to {url}")
    # Reject anything but http(s): the URL comes from a config file, and a
    # malformed/hostile value (file://, custom scheme) must not turn this health
    # check into a local-file read (bandit B310). Only http/https reach urlopen.
    from urllib.parse import urlparse
    if urlparse(url).scheme not in ("http", "https"):
        print(f"  shared server:           [WARN] fallback_url {url!r} is not http(s) "
              "— refusing to probe. Fix .embed_config.json.")
        return
    # Health-check the shared server. A dead endpoint here is the trap to surface.
    try:
        import urllib.request
        # scheme validated to http(s) above, so B310's file:// concern can't apply
        with urllib.request.urlopen(f"{url}/health", timeout=3) as r:  # nosec B310
            body = json.loads(r.read())
        if body.get("status") == "ok":
            print(f"  shared server:           OK (model={body.get('model')}, "
                  f"dim={body.get('dim')})")
        else:
            print(f"  shared server:           [WARN] responded status={body.get('status')!r} "
                  "(not ready) — embeds will slow-cascade until it's serving.")
    except Exception as e:  # noqa: BLE001
        print(f"  shared server:           [WARN] UNREACHABLE at {url} ({type(e).__name__}) "
              "— m3 is configured to defer to it but it's DOWN.")
        print("    Fix: start it (AgentOS_EmbedServer task or "
              "`python bin/embed_server_inproc.py`), or run `m3 embedder unshared` "
              "to revert to per-process embedders.")


def _crypto_section() -> None:
    """Emit the crypto/FIPS status line (backend, FIPS tier, trusted lib path).

    Shows whether crypto runs on the DEFAULT (Python) backend or wolfCrypt, the
    FIPS tier, and — importantly for security review — WHICH absolute, trusted
    path the wolfSSL library was loaded from (M3 never loads it by bare name).
    Best-effort and read-only; never changes doctor's exit code.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
        from crypto_provider import active_crypto_status
        cs = active_crypto_status()
    except Exception:  # noqa: BLE001 — informational only
        return
    print()
    print("crypto (FIPS):")
    tier = "strict" if cs["fips_strict"] else ("mode" if cs["fips_mode"] else "off")
    print(f"  backend:                 {cs['backend']}  (FIPS: {tier})")
    if cs["backend"] == "WOLFSSL" and cs.get("lib_path"):
        print(f"  wolfSSL loaded from:     {cs['lib_path']}"
              + ("  [integrity-pinned]" if cs["integrity_pinned"] else ""))
        print(f"  validated FIPS module:   {'yes' if cs['fips_validated'] else 'no (open-source build)'}")
        sha = cs.get("lib_sha256")
        if sha:
            print(f"  loaded lib SHA-256:      {sha}")
            if not cs["integrity_pinned"]:
                # Help the operator self-pin THEIR build (M3 doesn't ship wolfSSL).
                print("    (to detect later tampering, pin your trusted build:")
                print(f"     export M3_WOLFSSL_SHA256={sha} )")
    else:
        print(f"  status:                  {cs['summary']}")


def _roots_section() -> None:
    """Report the decoupled three-root layout and flag the split-brain hazard.

    The engine (DBs) and config roots can be relocated independently of the
    repo (M3_MEMORY_ROOT / M3_ENGINE_ROOT / M3_CONFIG_ROOT). When only SOME of
    those env vars are pinned, the MCP server and the chatlog hook can resolve
    different roots (the documented split-brain — see CLAUDE.md "Homecoming
    Architecture"). doctor is the natural place to surface where each root
    actually resolves and to warn when the pinning is partial.

    Best-effort: if the SDK resolvers aren't importable (stripped env), skip
    the section rather than fail doctor.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
        from m3_sdk import (  # type: ignore
            get_m3_config_root,
            get_m3_engine_root,
            get_m3_root,
        )
    except Exception:  # noqa: BLE001 — informational only
        return

    def _src(env_name: str) -> str:
        if os.environ.get(env_name):
            return f"({env_name} env)"
        if os.environ.get("M3_MEMORY_ROOT"):
            return "(derived from M3_MEMORY_ROOT)"
        return "(default ~/.m3)"

    print()
    print("decoupled roots:")
    mem = get_m3_root()
    cfg_root = get_m3_config_root()
    eng_root = get_m3_engine_root()
    print(f"  memory root (repo/state): {mem}  {_src('M3_MEMORY_ROOT')}")
    print(f"  config root:              {cfg_root}  {_src('M3_CONFIG_ROOT')}")
    print(f"  engine root (DBs):        {eng_root}  {_src('M3_ENGINE_ROOT')}")

    # Engine DBs presence — the thing users actually care about.
    eng = Path(eng_root)
    dbs = sorted(p.name for p in eng.glob("*.db")) if eng.is_dir() else []
    if dbs:
        print(f"  engine DBs present:       {', '.join(dbs)}")
    elif eng.is_dir():
        print("  engine DBs present:       (none yet — no captures/migrations written)")
    else:
        print("  engine DBs present:       (engine root does not exist yet)")

    # Split-brain hazard. Two independent risk signals (CLAUDE.md "Split-brain
    # hazard"): (a) exactly ONE of the engine/config roots is explicitly pinned
    # while the other only derives — the two can diverge; (b) a root is pinned
    # in THIS process but the MCP server / hook may not inherit it. We can only
    # observe this process's env, so we flag the structural asymmetry (a) and
    # always remind about the both-surfaces requirement when ANY root is pinned.
    eng_pinned = bool(os.environ.get("M3_ENGINE_ROOT"))
    cfg_pinned = bool(os.environ.get("M3_CONFIG_ROOT"))
    if eng_pinned != cfg_pinned:
        print()
        only = "M3_ENGINE_ROOT" if eng_pinned else "M3_CONFIG_ROOT"
        other = "M3_CONFIG_ROOT" if eng_pinned else "M3_ENGINE_ROOT"
        print(f"  [!] ASYMMETRIC root pinning: {only} is set but {other} is not.")
        print("      Pin BOTH together so the config and engine roots can't")
        print("      diverge. (Setting only one leaves the other on its")
        print("      derived/default path.)")
    if (eng_pinned or cfg_pinned or os.environ.get("M3_MEMORY_ROOT")):
        print()
        print("  [i] Decoupled-roots reminder: the m3 MCP server reads its root")
        print("      from the server `env` block in the client settings.json,")
        print("      while the chatlog Stop/PreCompact hook inherits the agent's")
        print("      PROCESS env. Pin M3_ENGINE_ROOT + M3_CONFIG_ROOT on BOTH")
        print("      surfaces, or the two halves write to different DBs.")
        print("      See CLAUDE.md → 'Split-brain hazard'.")


def _fmt_dual_time(value: "object") -> str:
    """Render a timestamp as 'LOCAL (ZULU)', per house convention.

    ``value`` may be an ISO-8601 string (SQLite TEXT timestamp), an epoch number,
    or a datetime. Returns e.g. ``2026-07-19 14:32:01 EDT (2026-07-19T18:32:01Z)``.
    Best-effort: an unparseable value is returned stringified, never raised — a
    doctor line must not crash on a weird timestamp.
    """
    import datetime as _dt

    if value is None or value == "":
        return "—"
    dt: "_dt.datetime | None" = None
    try:
        if isinstance(value, _dt.datetime):
            dt = value
        elif isinstance(value, (int, float)):
            dt = _dt.datetime.fromtimestamp(float(value))
        else:
            s = str(value).strip().replace("Z", "+00:00")
            dt = _dt.datetime.fromisoformat(s)
    except (ValueError, OSError, OverflowError):
        return str(value)
    if dt is None:
        return str(value)
    # Interpret a naive timestamp as UTC (SQLite stores wall-clock UTC), then
    # show it in local time with the UTC value in parens.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    local = dt.astimezone()
    # Prefer a compact tz abbreviation. On Windows %Z yields a verbose name
    # ("Eastern Daylight Time"); abbreviate to its initials (→ "EDT"). Fall back
    # to the numeric offset if %Z is empty.
    raw_tz = local.strftime("%Z")
    if raw_tz and " " in raw_tz:
        tzname = "".join(w[0] for w in raw_tz.split() if w).upper()
    else:
        tzname = raw_tz or local.strftime("%z")
    utc = dt.astimezone(_dt.timezone.utc)
    return (f"{local.strftime('%Y-%m-%d %H:%M:%S')} {tzname} "
            f"({utc.strftime('%Y-%m-%dT%H:%M:%SZ')})")


def _sqlite_store_stats(db_path: str) -> "dict | None":
    """(rows, last_updated) for a SQLite store file, or None if absent/unreadable.

    Read-only, best-effort. ``rows`` counts live memory_items; ``last_updated`` is
    the max of updated_at/created_at seen. A store that lacks memory_items (e.g. a
    files DB) reports rows via its own primary table when recognizable, else 0.
    """
    if not db_path or not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        def _has(t: str) -> bool:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
            ).fetchone() is not None

        rows = 0
        last = None
        if _has("memory_items"):
            rows = conn.execute(
                "SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted,0)=0"
            ).fetchone()[0]
            last = conn.execute(
                "SELECT MAX(COALESCE(updated_at, created_at)) FROM memory_items"
            ).fetchone()[0]
        elif _has("leaves"):  # files DB
            rows = conn.execute("SELECT COUNT(*) FROM leaves").fetchone()[0]
        return {"rows": rows, "last_updated": last}
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _cdw_sync_section() -> None:
    """If a CDW (data warehouse) DSN is defined, report last sync watermarks.

    The warehouse DSN is resolve_cdw_pg_dsn (M3_CDW_PG_URL > PG_URL deprecated).
    Sync watermarks live on the SQLite side in ``sync_watermarks(direction TEXT
    PK, last_synced_at TEXT)`` (pg_sync.py). Report each direction's last sync in
    LOCAL (ZULU) time. Silent when no CDW is configured (the common case).
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
        from m3_sdk import resolve_cdw_pg_dsn, resolve_db_path  # type: ignore
    except Exception:  # noqa: BLE001 — informational only
        return
    try:
        cdw = (resolve_cdw_pg_dsn("") or "").strip()
    except Exception:  # noqa: BLE001
        cdw = ""
    if not cdw:
        return  # no warehouse configured — nothing to report

    import re as _re
    masked = _re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", cdw)
    print()
    print("data warehouse (CDW) sync:")
    print(f"  warehouse DSN:            {masked}")

    core_db = ""
    try:
        core_db = resolve_db_path(None)
    except Exception:  # noqa: BLE001
        pass
    if not core_db or not os.path.exists(core_db):
        print("  [!] no local store to read sync watermarks from.")
        return
    try:
        conn = sqlite3.connect(f"file:{core_db}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        print("  [!] could not open the local store to read watermarks.")
        return
    try:
        have = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_watermarks'"
        ).fetchone()
        if not have:
            print("  last sync:                never (no sync_watermarks yet)")
            return
        rows = conn.execute(
            "SELECT direction, last_synced_at FROM sync_watermarks ORDER BY direction"
        ).fetchall()
        if not rows:
            print("  last sync:                never (no watermarks recorded)")
            return
        for direction, ts in rows:
            print(f"  {direction:<24} {_fmt_dual_time(ts)}")
    except sqlite3.Error as e:
        print(f"  [!] could not read watermarks: {e}")
    finally:
        conn.close()


def _backend_section(cfg: dict) -> None:
    """Report the active PRIMARY database backend and, on PostgreSQL, its health.

    The runtime selects its backend from the ENVIRONMENT (M3_DB_BACKEND +
    M3_PRIMARY_PG_URL / the selector), not the config file — so a mismatch
    between what the installer recorded in config (`db_backend`) and what the
    live env resolves to is a real footgun (the operator thinks they're on PG
    but the process still opens SQLite, or vice-versa). doctor is the place to
    surface both and flag the divergence, and to confirm a PG DSN is reachable
    rather than let the first real query be the discovery.

    Best-effort: importing the seam can fail on a stripped env — skip the section
    rather than crash doctor.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
        from memory.backends import resolve_backend_name  # type: ignore
    except Exception:  # noqa: BLE001 — informational only
        return

    print()
    print("primary database backend:")
    try:
        live = resolve_backend_name()
    except Exception as e:  # noqa: BLE001 — a bad M3_DB_BACKEND value lands here
        print(f"  [!] backend unresolved: {e}")
        return
    env_set = os.environ.get("M3_DB_BACKEND")
    src = "(M3_DB_BACKEND env)" if env_set else "(default)"
    print(f"  active backend (runtime): {live}  {src}")

    # Config vs env divergence — the switch is env, config is only a record.
    recorded = cfg.get("db_backend")
    if recorded and recorded != live:
        print(
            f"  [!] config records db_backend={recorded!r} but the live environment "
            f"resolves to {live!r}."
        )
        print("      The runtime uses the ENV value. Set M3_DB_BACKEND (and, for")
        print("      postgres, M3_PRIMARY_PG_URL) wherever m3 runs — the MCP server")
        print("      `env` block and any process that imports m3 — or re-run install.")

    if live == "sqlite":
        # Report the three local stores (core / chat / files) with full paths,
        # row counts, and last-updated time. Chat and core may be the SAME file
        # (chatlog_db == main_db) — dedupe the display. "If present and
        # discernible": a store that doesn't exist is shown as absent, not an error.
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
            from m3_sdk import resolve_db_path  # type: ignore
            core_db = resolve_db_path(None)
            try:
                from chatlog_config import DEFAULT_DB_PATH as chat_db  # type: ignore
            except Exception:  # noqa: BLE001
                chat_db = ""
            try:
                from memory.config import FILES_DB_PATH as files_db  # type: ignore
            except Exception:  # noqa: BLE001
                files_db = ""
        except Exception:  # noqa: BLE001 — informational only
            return

        stores = [("core", core_db)]
        if chat_db and os.path.abspath(chat_db) != os.path.abspath(core_db or ""):
            stores.append(("chat", chat_db))
        else:
            stores.append(("chat", core_db))  # same file as core
        if files_db:
            stores.append(("files", files_db))

        print("  stores (SQLite):")
        seen_paths: set = set()
        for label, path in stores:
            if not path:
                print(f"    {label:<6} —  (path not discernible)")
                continue
            ap = os.path.abspath(path)
            shared = "  (shared with core)" if ap in seen_paths else ""
            seen_paths.add(ap)
            stats = _sqlite_store_stats(path)
            if stats is None:
                print(f"    {label:<6} {path}  (absent){shared}")
            else:
                last = _fmt_dual_time(stats["last_updated"]) if stats["last_updated"] else "—"
                print(f"    {label:<6} {path}")
                print(f"           rows: {stats['rows']}   last updated: {last}{shared}")

        _cdw_sync_section()
        return

    # PostgreSQL: report the (masked) DSN and probe reachability.
    try:
        from m3_sdk import resolve_primary_pg_dsn  # type: ignore

        dsn = (resolve_primary_pg_dsn("") or "").strip()
    except Exception:  # noqa: BLE001
        dsn = ""
    if not dsn:
        print("  [!] M3_DB_BACKEND=postgres but no M3_PRIMARY_PG_URL/M3_PG_URL is set —")
        print("      the primary store has no DSN. Set M3_PRIMARY_PG_URL.")
        return

    import re as _re
    masked = _re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", dsn)
    print(f"  primary DSN:              {masked}")
    try:
        import psycopg2  # type: ignore

        conn = psycopg2.connect(dsn, connect_timeout=5)
        cur = conn.cursor()
        # Identity (host/db) for the panel — prepared as the same shape other
        # SQL backends (MariaDB, …) would report.
        try:
            cur.execute("SELECT current_database(), inet_server_addr()::text, current_schema()")
            dbname, host, schema = cur.fetchone()
        except Exception:  # noqa: BLE001 — older PG / restricted role
            dbname = host = schema = None
        cur.execute("SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted,0)=0")
        n = cur.fetchone()[0]
        try:
            cur.execute("SELECT MAX(COALESCE(updated_at, created_at)) FROM memory_items")
            last = cur.fetchone()[0]
        except Exception:  # noqa: BLE001
            last = None
        conn.close()
        if dbname:
            print(f"  identity:                 db={dbname} host={host or 'local'} schema={schema or 'public'}")
        print("  reachable:                yes")
        print(f"  memory_items:             {n}   last updated: "
              f"{_fmt_dual_time(last) if last else '—'}")
    except Exception as e:  # noqa: BLE001 — connection / auth / undefined-table
        detail = _re.sub(r"(://[^:/@]+:)[^@/]+(@)", r"\1***\2", str(e).strip())
        print(f"  reachable:                NO — {detail}")
        print("      Start/reach the PostgreSQL server. The schema builds")
        print("      automatically on the first successful connect.")

    _cdw_sync_section()


def _deprecated_env_section() -> None:
    """Surface deprecated (un-namespaced) env vars that are actually in use.

    m3-specific config vars are migrating under the M3_ namespace; the old
    generic names (PG_URL, tuning knobs, ...) still work via a
    back-compat shim but will be removed. This process's config modules read
    their env at import time, so by the time doctor runs, getenv_compat has
    recorded any deprecated name that resolved. Report only what's genuinely in
    use (no noise on a clean setup), with the new name to migrate to.

    Best-effort: skip silently if the SDK isn't importable.
    """
    try:
        import os as _os
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "bin"))
        from m3_core.paths import ROLE_SPLIT_ENV_RENAMES  # type: ignore
        from m3_sdk import deprecated_env_in_use  # type: ignore
    except Exception:  # noqa: BLE001 — informational only
        return
    in_use = dict(deprecated_env_in_use())
    # Role-split deprecations (e.g. PG_URL) may be SET but not read this process
    # (doctor doesn't exercise pg_sync), so getenv_compat never recorded them.
    # Surface them from the live environment directly so a set PG_URL is flagged.
    for old, new in ROLE_SPLIT_ENV_RENAMES.items():
        if _os.environ.get(old) is not None:
            in_use[old] = new

    # Windows PERSISTENT env vars live in the registry, not this process's env, so
    # a User/Machine-scope deprecated var (any name, not just PG_URL) would be
    # invisible to the checks above — doctor would report "clean" while the var is
    # still active. Scan the registry and surface each with its scope so the report
    # is complete on Windows. `m3 doctor --fix` rewrites the User-scope ones.
    reg_lines: "list[str]" = []
    try:
        from m3_core.paths import all_env_renames  # type: ignore

        from m3_memory import installer as _I  # type: ignore

        for hit in _I._scan_registry_env_deprecations(all_env_renames()):
            reg_lines.append(f"        {hit['old']}  ->  {hit['new']}   [{hit['label']}]")
    except Exception:  # noqa: BLE001 — best-effort; registry scan is Windows-only
        pass

    if not in_use and not reg_lines:
        return  # clean — say nothing rather than add noise
    print()
    print("  [!] deprecated env vars in use (still work, but migrate — the old")
    print("      names will be removed; `m3 install`/`update` will refuse while set):")
    for old, new in sorted(in_use.items()):
        print(f"        {old}  ->  {new}")
    for line in sorted(reg_lines):
        print(line)
    if reg_lines:
        print("      (registry entries: `m3 doctor --fix` rewrites User scope; "
              "Machine scope needs admin — command shown by --fix)")
