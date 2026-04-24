"""Installer for the m3-memory system payload.

The pip package ships thin: it exports a ``mcp-memory`` CLI but the actual
system code (``bin/memory_bridge.py`` plus the ~60 files it imports) lives
in the GitHub repo. This module clones or downloads that payload on demand
so a plain ``pip install m3-memory`` + ``mcp-memory install-m3`` is a
complete setup, no ``git clone`` step required from the user.

Resolution order for finding the bridge (see ``find_bridge``):

1. ``$M3_BRIDGE_PATH`` env var — power-user override, honored first.
2. ``~/.m3-memory/config.json`` — written by ``install_m3``.
3. Walk up from this file looking for a sibling ``bin/memory_bridge.py`` —
   catches the developer case where someone did ``pip install -e .`` from
   a clone of the repo.
4. None — caller prints a helpful error pointing at ``install-m3``.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_URL = "https://github.com/skynetcmd/m3-memory.git"
TARBALL_URL_TEMPLATE = "https://github.com/skynetcmd/m3-memory/archive/refs/tags/{tag}.tar.gz"


def config_dir() -> Path:
    """Directory for per-user m3-memory state (config + default repo clone)."""
    return Path.home() / ".m3-memory"


def config_file() -> Path:
    return config_dir() / "config.json"


def default_repo_path() -> Path:
    return config_dir() / "repo"


def load_config() -> dict:
    """Return the saved config, or an empty dict if none exists or is malformed."""
    path = config_file()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    config_file().write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")


def _developer_bridge() -> Optional[Path]:
    """Walk up from this file looking for a sibling ``bin/memory_bridge.py``.

    Returns the path if found (developer case: ``pip install -e .`` from a
    repo clone), else None.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "bin" / "memory_bridge.py"
        if candidate.is_file():
            return candidate
    return None


def find_bridge() -> Optional[Path]:
    """Locate ``memory_bridge.py`` using the four-step resolution order.

    Returns the absolute path if found, or None to signal "not installed."
    Callers should present an actionable message when None is returned.
    """
    # 1. Env var override.
    env = os.environ.get("M3_BRIDGE_PATH")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_file():
            return p

    # 2. Config file written by install_m3.
    cfg = load_config()
    bridge = cfg.get("bridge_path")
    if bridge:
        p = Path(bridge).expanduser().resolve()
        if p.is_file():
            return p

    # 3. Developer sibling case.
    dev = _developer_bridge()
    if dev:
        return dev

    return None


def _git_clone(tag: str, dest: Path) -> bool:
    """Shallow-clone REPO_URL at ``tag`` into ``dest``. Returns True on success,
    False if ``git`` is missing. Raises on any other subprocess failure."""
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", tag, REPO_URL, str(dest)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return True
    except FileNotFoundError:
        return False


def _safe_tar_member(member: "tarfile.TarInfo", dest_root: Path) -> "tarfile.TarInfo | None":
    """Per-member filter for tarfile.extractall.

    Blocks the classic path-traversal vectors:
      - absolute paths (`/etc/passwd`)
      - parent-dir escapes (`../../something`)
      - symlinks or hardlinks that point outside dest_root
      - device files, fifos, and other non-regular non-dir entries

    Returns the member unchanged if safe, or None to drop it (extractall
    skips filter-None entries). Raising would abort the whole extraction
    which is too aggressive for a GitHub tarball that may carry innocuous
    unusual entries; dropping is defensive but recoverable.
    """
    name = member.name
    # Reject absolute paths outright.
    if os.path.isabs(name) or name.startswith(("/", "\\")):
        return None
    # Normalize the member's target path and confirm it stays under dest_root.
    resolved = (dest_root / name).resolve()
    try:
        resolved.relative_to(dest_root.resolve())
    except ValueError:
        return None
    # Only allow regular files, directories, and links whose targets ALSO
    # resolve safely. Block devices, fifos, character/block specials.
    if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
        return None
    if member.issym() or member.islnk():
        link_target = (resolved.parent / member.linkname).resolve()
        try:
            link_target.relative_to(dest_root.resolve())
        except ValueError:
            return None
    return member


def _download_tarball(tag: str, dest: Path) -> None:
    """Fallback to downloading a release tarball and extracting it into ``dest``.

    Intended for environments without git (CI, minimal containers, some
    Windows installs). The GitHub tarball's top-level dir is
    ``m3-memory-<tag-without-v>/`` — we strip that and move contents into
    ``dest`` so the layout matches a git clone.

    Extraction is filtered through ``_safe_tar_member`` to block the
    traditional tarslip / path-traversal / device-file attack classes.
    Python 3.12's built-in ``filter='data'`` would also work, but we
    support 3.11 so we roll our own filter.
    """
    url = TARBALL_URL_TEMPLATE.format(tag=tag)
    # Defense-in-depth: TARBALL_URL_TEMPLATE is a hardcoded constant that
    # pins the host to github.com/skynetcmd/m3-memory, but we revalidate
    # the fully-interpolated URL before the request anyway. A malicious
    # `tag` (e.g. one containing a scheme or authority) can't leak the
    # request to another host. This also silences SAST tools that flag
    # any `urlopen()` whose argument isn't a string literal (CWE-918).
    _TRUSTED_URL_PREFIX = "https://github.com/skynetcmd/m3-memory/archive/refs/tags/"
    if not url.startswith(_TRUSTED_URL_PREFIX):
        raise RuntimeError(
            f"refusing to fetch tarball from untrusted URL: {url!r} "
            f"(expected prefix {_TRUSTED_URL_PREFIX!r})"
        )
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        archive = tmp / "repo.tar.gz"
        print(f"  downloading {url}")
        with urllib.request.urlopen(url) as resp, archive.open("wb") as f:  # nosec B310 — trusted GitHub host, prefix-validated above
            shutil.copyfileobj(resp, f)
        with tarfile.open(archive, "r:gz") as tf:
            tmp_resolved = tmp.resolve()
            tf.extractall(tmp, filter=lambda m, _path: _safe_tar_member(m, tmp_resolved))  # nosec B202 - filter blocks tarslip
        # Find the single top-level dir extracted.
        top_level = [p for p in tmp.iterdir() if p.is_dir() and p.name.startswith("m3-memory-")]
        if len(top_level) != 1:
            raise RuntimeError(f"unexpected tarball layout (top-level dirs: {top_level})")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(top_level[0]), str(dest))


# ───────── post-install helpers (tasks #8–#11 in plan memory 776d3729) ─────────

def _register_gemini_mcp() -> Optional[str]:
    """Write a `memory` MCP entry to ~/.gemini/settings.json if Gemini CLI exists.

    Idempotent: leaves an existing `memory` entry untouched. Returns a short
    status string for user-facing logging, or None if Gemini CLI isn't on PATH
    (in which case we stay quiet — not every install has Gemini).
    """
    gemini_bin = shutil.which("gemini")
    if not gemini_bin:
        # Also check the common non-interactive npm-global path (which may not
        # be on PATH yet — see _fix_npm_global_path below).
        npm_candidate = Path.home() / ".npm-global" / "bin" / "gemini"
        if not npm_candidate.exists():
            return None

    settings_dir = Path.home() / ".gemini"
    settings_file = settings_dir / "settings.json"
    settings_dir.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_file.is_file():
        try:
            existing = json.loads(settings_file.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            # Refuse to clobber a file we can't parse — user may have hand-edited.
            return f"[!] {settings_file} is unreadable; skipping Gemini MCP registration"

    mcp_servers = existing.setdefault("mcpServers", {})
    if "memory" in mcp_servers:
        return f"[=] Gemini MCP 'memory' already registered in {settings_file}"

    mcp_servers["memory"] = {"command": "mcp-memory"}
    settings_file.write_text(
        json.dumps(existing, indent=2) + "\n",
        encoding="utf-8",
    )
    return f"[+] registered 'memory' MCP in {settings_file}"


def _sqlite3_cli_hint() -> Optional[str]:
    """Return a per-OS install instruction if the `sqlite3` CLI is missing.

    Returns None if the CLI is already on PATH (no action needed).
    Purely advisory — we don't run sudo commands on the user's behalf.
    """
    if shutil.which("sqlite3"):
        return None

    system = platform.system()
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


def _fix_npm_global_path() -> Optional[str]:
    """Append ~/.npm-global/bin to ~/.profile for non-interactive shells.

    Interactive shells typically source ~/.bashrc; cron jobs, sshd non-login
    shells, and most scripts read ~/.profile. Without this, `gemini` (and any
    other npm-global binary) is missing from those contexts.

    No-op on Windows (npm uses %APPDATA%\\npm which is added to user PATH by
    the Node installer). Idempotent — checks for the exact export line first.
    """
    if platform.system() == "Windows":
        return None
    npm_bin = Path.home() / ".npm-global" / "bin"
    if not npm_bin.exists():
        return None

    profile = Path.home() / ".profile"
    marker = 'export PATH="$HOME/.npm-global/bin:$PATH"'
    existing = ""
    if profile.is_file():
        try:
            existing = profile.read_text(encoding="utf-8")
        except OSError:
            return f"[!] {profile} unreadable; add {marker!r} manually"
    if marker in existing:
        return f"[=] ~/.npm-global/bin already in {profile}"

    suffix = "\n# Added by mcp-memory install-m3 (npm-global PATH for non-interactive shells)\n" + marker + "\n"
    if existing and not existing.endswith("\n"):
        suffix = "\n" + suffix
    try:
        with profile.open("a", encoding="utf-8") as f:
            f.write(suffix)
    except OSError as e:
        return f"[!] could not write to {profile}: {e}"
    return f"[+] appended npm-global PATH export to {profile}"


def _prompt_endpoint_choice(interactive: bool, endpoint_flag: Optional[str]) -> Optional[str]:
    """Ask which LLM endpoint to use; persist or return None to accept defaults.

    Non-interactive + no flag: return None (caller stores nothing → llm_failover
    probes both defaults). This mirrors the existing _auto_install ergonomics
    in cli.py (quiet defaults when no TTY).
    """
    if endpoint_flag is not None:
        return endpoint_flag
    if not interactive:
        return None
    print("\nLLM endpoint to use for embedding + enrichment:")
    print("  1) LM Studio (http://localhost:1234/v1)")
    print("  2) Ollama    (http://localhost:11434/v1)")
    print("  3) probe both at runtime (default)")
    try:
        reply = input("Choice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if reply == "1":
        return "http://localhost:1234/v1"
    if reply == "2":
        return "http://localhost:11434/v1"
    return None


def _prompt_capture_mode(interactive: bool, capture_flag: Optional[str]) -> Optional[str]:
    """Ask which Claude-Code capture hooks to enable.

    Values: 'both' | 'stop' | 'precompact' | 'none' | None (defer to chatlog_init
    defaults). Non-interactive returns None unless --capture-mode was passed.
    """
    if capture_flag is not None:
        return capture_flag
    if not interactive:
        return None
    print("\nChatlog capture hooks (Claude Code):")
    print("  1) both Stop + PreCompact (recommended — lossless capture)")
    print("  2) PreCompact only (lower overhead)")
    print("  3) Stop only")
    print("  4) neither (skip hook setup; configure later with `mcp-memory chatlog init`)")
    try:
        reply = input("Choice [1/2/3/4, default 1]: ").strip() or "1"
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return {"1": "both", "2": "precompact", "3": "stop", "4": "none"}.get(reply)


def _run_chatlog_init(bridge: Path, capture_mode: str) -> Optional[str]:
    """Run `chatlog_init.py --non-interactive --apply-claude --apply-gemini` so
    the user's chatlog choice actually takes effect. Without this, capture_mode
    persists in config but no settings.json is written and no migrations run.

    Returns a status message for the post-install summary, or None on failure
    (logged separately so the install still completes).
    """
    if capture_mode == "none":
        return "[=] chatlog hooks skipped (capture-mode=none)"

    chatlog_init = bridge.parent / "chatlog_init.py"
    if not chatlog_init.is_file():
        return f"[!] chatlog_init.py missing under {bridge.parent}; skipping hook wiring"

    cmd = [
        sys.executable, str(chatlog_init),
        "--non-interactive",
        "--capture-mode", capture_mode,
        "--apply-claude",
    ]
    # Wire Gemini hooks too if Gemini CLI is on PATH (or at the npm-global
    # location) — same detection used by _register_gemini_mcp.
    if shutil.which("gemini") or (Path.home() / ".npm-global" / "bin" / "gemini").exists():
        cmd.append("--apply-gemini")

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        # chatlog_init prints multiple lines; take the last non-empty line as
        # the summary so the post-install log stays compact.
        lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
        tail = lines[-1] if lines else "configured"
        return f"[+] chatlog wired ({capture_mode}): {tail}"
    except subprocess.CalledProcessError as e:
        # Don't abort the install — surface the error and let the user re-run.
        stderr = (e.stderr or "").strip() or "(no stderr)"
        return f"[!] chatlog init failed: {stderr.splitlines()[-1] if stderr else e}"


def _post_install(
    bridge: Path,
    interactive: bool,
    endpoint_choice: Optional[str],
    capture_choice: Optional[str],
) -> None:
    """Run the additive post-install steps. Each step prints its own status.

    All steps are best-effort: a failure is reported but does not abort the
    install. install-m3's core job (repo on disk + config written) is already
    done by the time we get here.
    """
    # Collect all messages first so we can suppress the section entirely when
    # nothing is actionable (every helper returning None = everything already
    # fine, which is a common outcome on well-configured boxes).
    messages = [
        m for m in (
            _register_gemini_mcp(),
            _sqlite3_cli_hint(),
            _fix_npm_global_path(),
        ) if m
    ]
    cfg = load_config()
    changed = False
    if endpoint_choice is not None:
        cfg["llm_endpoints_csv"] = endpoint_choice
        os.environ.setdefault("LLM_ENDPOINTS_CSV", endpoint_choice)
        messages.append(f"[+] pinned LLM endpoint: {endpoint_choice}")
        changed = True
    if capture_choice is not None:
        cfg["chatlog_capture_mode"] = capture_choice
        messages.append(f"[+] pinned chatlog capture mode: {capture_choice}")
        changed = True
    if changed:
        save_config(cfg)

    # Wire chatlog hooks into agent settings.json files when the user picked
    # a capture mode. Skips silently when capture_choice is None (user accepted
    # defaults and we don't auto-touch their agent configs without consent).
    if capture_choice is not None:
        chatlog_msg = _run_chatlog_init(bridge, capture_choice)
        if chatlog_msg:
            messages.append(chatlog_msg)

    if not messages:
        # Nothing to report — the three helpers all returned None and the
        # user accepted silent defaults. Tell them what that means so the
        # install doesn't end with silence that reads as "did it work?"
        print()
        print("post-install: no action needed (sqlite3 present, no Gemini CLI detected, no prompts).")
        print("              run `mcp-memory doctor` anytime to re-check.")
        return

    print()
    print("post-install:")
    for msg in messages:
        print(f"  {msg}")
    print("  run `mcp-memory doctor` to re-check anytime.")


def install_m3(
    repo_path: Optional[Path] = None,
    tag: Optional[str] = None,
    force: bool = False,
    interactive: Optional[bool] = None,
    endpoint: Optional[str] = None,
    capture_mode: Optional[str] = None,
) -> Path:
    """Clone or download the m3-memory repo and record the bridge path in config.

    ``repo_path`` defaults to ``~/.m3-memory/repo``. ``tag`` defaults to
    ``v<m3_memory.__version__>`` so the cloned payload always matches the
    installed wheel. ``force=True`` wipes an existing clone before re-fetching.

    ``interactive`` controls the post-install prompts (endpoint, capture mode).
    ``None`` auto-detects via ``sys.stdin.isatty()``. ``endpoint`` and
    ``capture_mode`` are explicit overrides that skip their respective prompts.

    Returns the resolved path to ``bin/memory_bridge.py``. Raises RuntimeError
    if neither git nor the tarball fallback can fetch the repo.
    """
    from m3_memory import __version__

    if repo_path is None:
        repo_path = default_repo_path()
    repo_path = repo_path.expanduser().resolve()

    if tag is None:
        tag = f"v{__version__}"

    if interactive is None:
        interactive = sys.stdin.isatty()

    # Collect user choices BEFORE any slow network work so the prompt appears
    # promptly and the clone/download doesn't block on input below.
    endpoint_choice = _prompt_endpoint_choice(interactive, endpoint)
    capture_choice = _prompt_capture_mode(interactive, capture_mode)

    # Preserve user data across --force / update. The repo tree under
    # repo_path/memory/ holds chatlog DBs, the chatlog config, and the
    # migration-tracking schema_version table — wiping them on every update
    # would discard captured turns and force a re-init. Stash anything that
    # looks like user data, wipe the code tree, then restore.
    preserved_dir: Optional[Path] = None
    if repo_path.exists():
        if not force:
            raise RuntimeError(
                f"{repo_path} already exists. Run `mcp-memory install-m3 --force` to replace it, "
                f"or `mcp-memory update` to refresh to the current wheel version."
            )
        memory_dir = repo_path / "memory"
        if memory_dir.is_dir():
            preserved_dir = Path(tempfile.mkdtemp(prefix="m3-preserve-"))
            for item in memory_dir.iterdir():
                # Keep .db / .json (chatlog config + state) / .jsonl (cursor).
                # The migrations/ subdir ships with the repo and will be
                # restored by the new clone, so we don't preserve it.
                if item.is_file() and item.suffix in (".db", ".json", ".jsonl"):
                    shutil.copy2(item, preserved_dir / item.name)
            print(f"  preserving {sum(1 for _ in preserved_dir.iterdir())} user-data file(s) across update")
        print(f"  removing existing {repo_path}")
        shutil.rmtree(repo_path)

    print(f"fetching m3-memory {tag} -> {repo_path}")
    if not _git_clone(tag, repo_path):
        print("  git not found; falling back to GitHub tarball")
        _download_tarball(tag, repo_path)

    bridge = repo_path / "bin" / "memory_bridge.py"
    if not bridge.is_file():
        raise RuntimeError(
            f"fetched repo but {bridge} not found. This usually means the "
            f"tag {tag!r} doesn't exist on GitHub yet. Check "
            f"https://github.com/skynetcmd/m3-memory/releases."
        )

    # Restore preserved user data on top of the fresh tree.
    if preserved_dir is not None:
        memory_dir = repo_path / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        restored = 0
        for item in preserved_dir.iterdir():
            shutil.copy2(item, memory_dir / item.name)
            restored += 1
        shutil.rmtree(preserved_dir, ignore_errors=True)
        if restored:
            print(f"  restored {restored} user-data file(s) into {memory_dir}")

    save_config({
        "repo_path": str(repo_path),
        "bridge_path": str(bridge),
        "version": __version__,
        "tag": tag,
        "installed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    print(f"[OK] installed. bridge_path = {bridge}")
    print(f"  config written to {config_file()}")

    _post_install(bridge, interactive, endpoint_choice, capture_choice)
    return bridge


def uninstall_m3(yes: bool = False) -> None:
    """Remove the cloned repo + config file. Idempotent."""
    cfg = load_config()
    repo_path = Path(cfg.get("repo_path", str(default_repo_path())))

    if not cfg and not repo_path.exists():
        print("nothing to uninstall (no config, no repo).")
        return

    if not yes:
        print(f"will remove:")
        print(f"  {repo_path}")
        print(f"  {config_file()}")
        resp = input("proceed? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("aborted.")
            return

    if repo_path.exists():
        shutil.rmtree(repo_path, ignore_errors=True)
        print(f"  removed {repo_path}")
    if config_file().is_file():
        config_file().unlink()
        print(f"  removed {config_file()}")


def _resolve_chatlog_db(cfg: dict) -> Optional[Path]:
    """Best-effort resolution of the chatlog DB path.

    Order: CHATLOG_DB_PATH env, M3_DATABASE env (shared DB case), config's
    chatlog_db_path, else <repo_path>/memory/agent_chatlog.db.
    Returns None only when no repo_path is configured AND no env override
    is set — i.e. the system isn't installed yet.
    """
    env_chatlog = os.environ.get("CHATLOG_DB_PATH")
    if env_chatlog:
        return Path(env_chatlog).expanduser()
    env_main = os.environ.get("M3_DATABASE")
    if env_main:
        return Path(env_main).expanduser()
    cfg_chatlog = cfg.get("chatlog_db_path")
    if cfg_chatlog:
        return Path(cfg_chatlog).expanduser()
    repo = cfg.get("repo_path")
    if repo:
        return Path(repo) / "memory" / "agent_chatlog.db"
    # Developer case: `pip install -e .` from a repo clone. No config file,
    # but the DB lives next to the sibling bridge we already resolve.
    dev = _developer_bridge()
    if dev:
        return dev.parent.parent / "memory" / "agent_chatlog.db"
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


def doctor() -> int:
    """Print diagnostic info and return 0 on healthy, 1 on missing payload."""
    from m3_memory import __version__

    print(f"m3-memory package version: {__version__}")
    print(f"config file:               {config_file()}")
    cfg = load_config()
    if cfg:
        print(f"  installed version:       {cfg.get('version', '?')}")
        print(f"  installed tag:           {cfg.get('tag', '?')}")
        print(f"  installed at:            {cfg.get('installed_at', '?')}")
        print(f"  repo_path:               {cfg.get('repo_path', '?')}")
    else:
        print("  (no config - system not installed via `mcp-memory install-m3`)")

    env = os.environ.get("M3_BRIDGE_PATH")
    if env:
        print(f"M3_BRIDGE_PATH (env):      {env}")
    else:
        print("M3_BRIDGE_PATH (env):      (unset)")

    dev = _developer_bridge()
    if dev:
        print(f"developer sibling bridge:  {dev}")
    else:
        print("developer sibling bridge:  (not found)")

    _chatlog_section(cfg)

    print()
    bridge = find_bridge()
    if bridge and bridge.is_file():
        print(f"[OK] resolved bridge: {bridge}")
        return 0
    print("[X] no bridge found. Run `mcp-memory install-m3` to fetch the system.")
    return 1
