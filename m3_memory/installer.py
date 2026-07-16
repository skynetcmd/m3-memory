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

from m3_memory._platform import os_name as _os_name
from m3_memory.install.fs import (  # noqa: F401  (re-exported facade surface — see cli.py / test_installer.py importers)
    _drain_wal,
    _robust_rmtree,
    _safe_copy_sqlite,
    _safe_tar_member,
)
from m3_memory.install.sections import (  # noqa: F401  (re-exported facade surface)
    _chatlog_db_stats,
    _chatlog_section,
    _claude_hook_state,
    _crypto_section,
    _deprecated_env_section,
    _embedder_tier_section,
    _gemini_hook_state,
    _read_json,
    _resolve_chatlog_db,
    _roots_section,
    _sqlite3_cli_hint,
)

REPO_URL = "https://github.com/skynetcmd/m3-memory.git"
TARBALL_URL_TEMPLATE = "https://github.com/skynetcmd/m3-memory/archive/refs/tags/{tag}.tar.gz"


def config_dir() -> Path:
    """Directory for per-user m3-memory state (config + default repo clone).
    Honors M3_MEMORY_ROOT environment variable, defaults to ~/.m3-memory.
    """
    root = os.environ.get("M3_MEMORY_ROOT")
    if root:
        return Path(root).expanduser().resolve()
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
    import os
    for env_var in ("M3_CONFIG_ROOT", "M3_ENGINE_ROOT", "M3_FIPS_MODE"):
        if env_var in os.environ and env_var not in cfg:
            cfg[env_var] = os.environ[env_var]
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


def _payload_dir(component: str, env_var: str, must_contain: "Optional[str]" = None) -> "Optional[Path]":
    """Resolve a payload component dir (bin/docs/_assets/examples) with a
    per-component env-var override. Precedence:
      1. $<env_var> if set and exists (and, if must_contain given, contains it)
      2. Path(__file__).parent / component  (the wheel-shipped packaged copy)
      3. a dev-checkout sibling: walk up from __file__ for a <component>/ dir
         (mirrors _developer_bridge for the pip-install-e . case)
      4. None
    """
    def _ok(base: "Path") -> bool:
        return base.is_dir() and (must_contain is None or (base / must_contain).exists())

    env = os.environ.get(env_var)
    if env:
        p = Path(env).expanduser()
        if _ok(p):
            return p.resolve()
    packaged = Path(__file__).resolve().parent / component
    if _ok(packaged):
        return packaged
    # dev-checkout sibling: walk up looking for <repo>/<component>
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / component
        if _ok(cand):
            return cand
    return None


def bin_dir() -> "Optional[Path]":
    """The bin/ directory (payload scripts + memory_bridge.py). $M3_PATH_BIN overrides."""
    return _payload_dir("bin", "M3_PATH_BIN", must_contain="memory_bridge.py")


def docs_dir() -> "Optional[Path]":
    """The docs/ directory. $M3_PATH_DOC overrides."""
    return _payload_dir("docs", "M3_PATH_DOC")


def assets_dir() -> "Optional[Path]":
    """The _assets/ directory. $M3_PATH_ASSETS overrides."""
    return _payload_dir("_assets", "M3_PATH_ASSETS")


def examples_dir() -> "Optional[Path]":
    """The examples/ directory. $M3_PATH_EXAMPLES overrides."""
    return _payload_dir("examples", "M3_PATH_EXAMPLES")


def find_bridge() -> Optional[Path]:
    """Resolve bin/memory_bridge.py via bin_dir() (which honors $M3_PATH_BIN,
    then the wheel-packaged location, then a dev-checkout sibling). Returns None
    if no payload bin/ can be found (caller errors to `m3 install-m3`)."""
    d = bin_dir()
    return (d / "memory_bridge.py") if d else None


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

def _m3_state_root() -> Path:
    """The M3_MEMORY_ROOT used in generated MCP env blocks (parent of the repo).

    Honors M3_MEMORY_ROOT; otherwise the config dir's root (~/.m3 today). This is
    the value written as ``M3_MEMORY_ROOT`` into each agent's MCP ``env`` so the
    bridge resolves the decoupled engine/config roots consistently.
    """
    root = os.environ.get("M3_MEMORY_ROOT")
    if root:
        return Path(root).expanduser()
    # config_dir() is <root>/config-ish; its parent is the state root (~/.m3).
    return config_dir()


def _canonical_bridge_path() -> Optional[Path]:
    """The bridge path a freshly-written agent config SHOULD point at.

    Prefers the live, resolved bridge (find_bridge: env > config > dev-sibling)
    so the config tracks whatever is actually running. Returns None only when no
    bridge can be found at all (system not installed).
    """
    b = find_bridge()
    return Path(b).resolve() if b else None


def _canonical_memory_env() -> dict:
    """The env block every agent's ``memory`` MCP server should carry.

    Single source of truth shared by install-time registration and
    ``doctor --fix`` self-heal, so the two never drift apart again.
    """
    state_root = str(_m3_state_root()).replace("\\", "/")
    cfg = load_config()
    # Precedence MUST match the canonical resolver in m3_core.paths
    # (env > derived), or the bridge and this generated env block disagree on
    # which roots are authoritative. Previously this read the config file FIRST
    # (cfg.get(...) or env or derived), which let a stale/hand-edited config key
    # beat an explicit env var — the opposite of the documented rule. In
    # practice m3 never writes these keys to the config file (the setup wizard
    # sets them as env vars), so cfg is now only a last-resort override BELOW
    # env + the canonical derivation.
    from m3_sdk import get_m3_config_root, get_m3_engine_root
    eng = os.environ.get("M3_ENGINE_ROOT") or get_m3_engine_root() \
        or cfg.get("M3_ENGINE_ROOT") or str(_m3_state_root() / "engine")
    conf = os.environ.get("M3_CONFIG_ROOT") or get_m3_config_root() \
        or cfg.get("M3_CONFIG_ROOT") or str(_m3_state_root() / "config")
    env = {
        "M3_MEMORY_ROOT": state_root,
        "M3_ENGINE_ROOT": str(eng).replace("\\", "/"),
        "M3_CONFIG_ROOT": str(conf).replace("\\", "/"),
    }
    # Pin the payload bin/ DIRECTORY via M3_PATH_BIN (replaces the removed
    # M3_BRIDGE_PATH file-var). Only written when the resolved bin/ is NOT the
    # packaged default — a packaged install needs no pin (the server resolves
    # bin/ from its own package location), so we keep the env block clean in the
    # common case and only pin a non-standard (dev / staged) location.
    _bd = bin_dir()
    if _bd is not None:
        _packaged_default = Path(__file__).resolve().parent / "bin"
        if _bd.resolve() != _packaged_default.resolve():
            env["M3_PATH_BIN"] = str(_bd).replace("\\", "/")
    # Deliberately NO M3_EMBED_GGUF in the MCP server env. An env var forces the
    # bridge to load its OWN in-process CUDA embedder (a second context; the exact
    # hang that motivated this). Discover the GGUF and seed it into the SHARED
    # config instead (§3 headless knob -> config file), so the single :8082 server
    # owns the one CUDA context and the bridge defers to it.
    embed = os.environ.get("M3_EMBED_GGUF")
    if not embed:
        cand = Path.home() / ".lmstudio" / "models" / "deepsweet" \
            / "bge-m3-GGUF-Q4_K_M" / "bge-m3-GGUF-Q4_K_M.gguf"
        if cand.is_file():
            embed = str(cand)
    try:
        from m3_memory.embedder_admin import seed_shared_config
        seed_shared_config(conf, gguf_path=(str(embed) if embed else None))
    except Exception:  # noqa: BLE001 — env generation must not fail on config seed
        pass
    return env


def _canonical_memory_server() -> dict:
    """The canonical ``memory`` MCP server entry (command + args + env).

    Uses an explicit interpreter + bridge-path arg (the schema that actually
    works), NOT a bare ``{"command": "mcp-memory"}`` console-script — that older
    shape could not carry the decoupled-root env and is what caused configs to
    drift. Falls back to the console script only if no bridge/interpreter can be
    resolved (degraded, but better than nothing).
    """
    bridge = _canonical_bridge_path()
    if bridge:
        # Prefer the interpreter that imports m3 cleanly: the running one.
        python_cmd = sys.executable.replace("\\", "/")
        return {
            "command": python_cmd,
            "args": [str(bridge).replace("\\", "/")],
            "env": _canonical_memory_env(),
        }
    return {"command": "mcp-memory", "env": _canonical_memory_env()}


def _path_is_stale(value: object) -> bool:
    """True if ``value`` looks like a filesystem path that no longer exists.

    Used to decide whether an existing ``memory`` entry must be repointed. Only
    flags absolute-ish paths (containing a separator) so we never misjudge a bare
    console-script name like ``mcp-memory`` or ``python`` as stale.
    """
    if not isinstance(value, str) or not value:
        return False
    looks_like_path = ("/" in value) or ("\\" in value) or value.endswith(".py")
    if not looks_like_path:
        return False
    return not Path(value).expanduser().exists()


def _memory_entry_needs_repoint(entry: object) -> bool:
    """Does an existing agent ``memory`` MCP entry point at dead/moved paths?

    Returns True when the command, any arg, or any M3_* path-like env value
    references a file that no longer exists — the split-brain signature.
    """
    if not isinstance(entry, dict):
        return True
    if _path_is_stale(entry.get("command")):
        return True
    for a in entry.get("args", []) or []:
        if _path_is_stale(a):
            return True
    env = entry.get("env", {}) or {}
    for k, v in env.items():
        # Only path-bearing env vars matter here; M3_BRIDGE_PATH / roots / gguf.
        if k.startswith("M3_") and _path_is_stale(v):
            return True
    return False


def _heal_agent_settings(settings_file: Path, *, force: bool = False) -> Optional[str]:
    """Repoint a single agent's ``memory`` MCP entry to the canonical config.

    This FIXES the historical bug where registration skipped an already-present
    ``memory`` entry even when its paths were dead (the split-brain that survived
    upgrades). Behavior:
      - no file / unparseable      -> leave alone, report
      - no ``memory`` entry        -> add the canonical one
      - entry present but stale    -> repoint (back up first)
      - entry present and healthy  -> no-op (unless force=True)
    Returns a short status string, or None when nothing was actionable.
    """
    if not settings_file.is_file():
        return None
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return f"[!] {settings_file} is unreadable; skipping (hand-edited?)"

    servers = data.setdefault("mcpServers", {})
    existing = servers.get("memory")
    if existing is not None and not force and not _memory_entry_needs_repoint(existing):
        return None  # healthy — stay quiet

    canonical = _canonical_memory_server()
    if existing == canonical:
        return None

    # Back up before any rewrite so the prior config is always restorable.
    if existing is not None:
        bak = settings_file.with_suffix(settings_file.suffix + ".m3bak")
        try:
            bak.write_text(settings_file.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            pass
    servers["memory"] = canonical
    settings_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    verb = "repointed" if existing is not None else "registered"
    return f"[+] {verb} 'memory' MCP in {settings_file}"


def _register_gemini_mcp() -> Optional[str]:
    """Register/repoint the ``memory`` MCP entry in ~/.gemini/settings.json.

    Idempotent AND self-healing: unlike the old version, an already-present but
    STALE ``memory`` entry (dead bridge/root paths from a moved install) is
    repointed to the canonical layout instead of being skipped. Returns None when
    Gemini CLI isn't present (we stay quiet — not every box has Gemini).
    """
    gemini_bin = shutil.which("gemini")
    if not gemini_bin:
        npm_candidate = Path.home() / ".npm-global" / "bin" / "gemini"
        if not npm_candidate.exists() and not (Path.home() / ".gemini").is_dir():
            return None

    settings_dir = Path.home() / ".gemini"
    settings_file = settings_dir / "settings.json"
    settings_dir.mkdir(parents=True, exist_ok=True)
    return _heal_agent_settings(settings_file)


def _register_antigravity_mcp() -> Optional[str]:
    """Register/repoint the ``memory`` MCP entry in Antigravity's settings.json.

    Same self-healing behavior as ``_register_gemini_mcp``. Returns None when
    Antigravity CLI / its app-data dir isn't present.
    """
    agy_bin = shutil.which("agy")
    settings_dir = Path.home() / ".gemini" / "antigravity-cli"
    if not agy_bin:
        candidate = Path.home() / ".local" / "bin" / "agy"
        appdata = os.environ.get("LOCALAPPDATA")
        win_candidate = Path(appdata) / "agy" / "bin" / "agy.exe" if appdata else None
        if not candidate.exists() and not (win_candidate and win_candidate.exists()) \
                and not settings_dir.is_dir():
            return None

    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file = settings_dir / "settings.json"
    return _heal_agent_settings(settings_file)


def _fix_npm_global_path() -> Optional[str]:
    """Append ~/.npm-global/bin to ~/.profile for non-interactive shells.

    Interactive shells typically source ~/.bashrc; cron jobs, sshd non-login
    shells, and most scripts read ~/.profile. Without this, `gemini` (and any
    other npm-global binary) is missing from those contexts.

    No-op on Windows (npm uses %APPDATA%\\npm which is added to user PATH by
    the Node installer). Idempotent — checks for the exact export line first.
    """
    if _os_name() == "Windows":
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


def _prompt_cognitive_loop(interactive: bool, cognitive_loop_flag: bool) -> bool:
    """Passthrough for the --cognitive-loop install flag.

    Stub today: returns the flag verbatim. Reserved as the prompt site for
    when the cognitive-loop install path lands and we want to ask
    interactively (mirrors _prompt_endpoint_choice / _prompt_capture_mode).
    """
    return cognitive_loop_flag


def _chatlog_init_supports(chatlog_init: Path, flag: str) -> bool:
    """Probe whether bin/chatlog_init.py advertises a given flag.

    Needed because install-m3 can fetch a tarball whose bin/ predates the
    flags we want to pass — that combination ships in any release where
    the wheel rolls forward before the tag does, or when a user installs
    the latest wheel against a pinned older tag. Falling back gracefully
    is better than failing the install with 'unrecognized arguments'.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(chatlog_init), "--help"],
            capture_output=True, text=True, timeout=10,
        )
        return flag in (result.stdout or "")
    except (subprocess.SubprocessError, OSError):
        return False


def _run_chatlog_init(bridge: Path, capture_mode: str) -> Optional[str]:
    """Run `chatlog_init.py --non-interactive ...` to actualize capture_mode.

    Without this, capture_mode persists in config but no settings.json is
    written and no migrations run. We adapt the flag set to whatever
    chatlog_init in the fetched repo supports, so wheel-vs-tag version skew
    degrades gracefully rather than failing 'unrecognized arguments'.

    Returns a status message for the post-install summary, or None on
    failure (logged separately so the install still completes).
    """
    if capture_mode == "none":
        return "[=] chatlog hooks skipped (capture-mode=none)"

    chatlog_init = bridge.parent / "chatlog_init.py"
    if not chatlog_init.is_file():
        return f"[!] chatlog_init.py missing under {bridge.parent}; skipping hook wiring"

    cmd = [sys.executable, str(chatlog_init), "--non-interactive"]

    # Probe each new flag. If the deployed chatlog_init is older than the
    # wheel, we still get migrations + a saved config (legacy behavior),
    # then fall back to printing manual-paste instructions.
    has_capture_mode = _chatlog_init_supports(chatlog_init, "--capture-mode")
    has_apply_claude = _chatlog_init_supports(chatlog_init, "--apply-claude")
    has_apply_gemini = _chatlog_init_supports(chatlog_init, "--apply-gemini")

    if has_capture_mode:
        cmd += ["--capture-mode", capture_mode]
    if has_apply_claude:
        cmd.append("--apply-claude")
    gemini_present = (
        shutil.which("gemini")
        or (Path.home() / ".npm-global" / "bin" / "gemini").exists()
    )
    if has_apply_gemini and gemini_present:
        cmd.append("--apply-gemini")

    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        last = stderr.splitlines()[-1] if stderr else str(e)
        return f"[!] chatlog init failed: {last}"

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    tail = lines[-1] if lines else "configured"

    # Old chatlog_init that lacks --apply-* flags only saved config + ran
    # migrations. Tell the user to do the rest by hand so they don't think
    # the install is finished.
    if not has_apply_claude:
        return (
            f"[~] chatlog config + migrations applied (capture-mode={capture_mode}); "
            f"settings.json wiring requires manual paste — run "
            f"`mcp-memory chatlog init --enable-stop-hook` and follow the snippet, "
            f"or `mcp-memory update` once the next release ships."
        )
    return f"[+] chatlog wired ({capture_mode}): {tail}"


def _run_main_migrations(bridge: Path) -> Optional[str]:
    """Run `migrate_memory.py up --yes --target main` to initialize the main DB.

    Ensures that a fresh install has a valid agent_memory.db so `doctor`
    reports [OK] instead of [ERROR] Database not found.
    """
    migrate_script = bridge.parent / "migrate_memory.py"
    if not migrate_script.is_file():
        return f"[!] migrate_memory.py missing under {bridge.parent}; skipping main DB init"

    cmd = [sys.executable, str(migrate_script), "up", "--yes", "--target", "main"]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return "[+] main memory DB initialized (migrations applied)"
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        last = stderr.splitlines()[-1] if stderr else str(e)
        return f"[!] main DB init failed: {last}"


def _run_os_install(bridge: Path) -> Optional[str]:
    """Execute the OS-specific installer (install_os.py) in the payload root."""
    # Resolve install_os.py via bin_dir() if available, else fall back to bridge-relative path.
    bin_d = bin_dir()
    if bin_d:
        install_script = bin_d.parent / "install_os.py"
    else:
        install_script = bridge.parent.parent / "install_os.py"
    if not install_script.is_file():
        return None

    # Run using the same python we're currently in
    try:
        subprocess.run([sys.executable, str(install_script)], check=True)
        return "OS-specific environment setup complete."
    except subprocess.CalledProcessError as e:
        return f"OS setup failed (code {e.returncode}). Run it manually from {install_script}."

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
            _run_os_install(bridge),
            _run_main_migrations(bridge),
            _register_gemini_mcp(),
            _register_antigravity_mcp(),
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
    else:
        print()
        print("post-install:")
        for msg in messages:
            print(f"  {msg}")
        print("  run `mcp-memory doctor` to re-check anytime.")

    # Always end with the data-durability reminder — install/upgrade are the
    # natural moments to nudge the user about backups.
    _print_backup_reminder()


def _detect_cdw_target() -> "Optional[str]":
    """Return the configured data-warehouse host (no credentials), or None.

    A CDW is "configured" if PG_URL resolves (env var or the encrypted vault)
    OR a SYNC_TARGET_IP / POSTGRES_SERVER is set. We return only the host so the
    reminder can name where data is auto-syncing — never the password from
    PG_URL.
    """
    from m3_sdk import getenv_compat, resolve_cdw_pg_dsn

    # 1. Explicit warehouse IP/host (sync_all.py uses these).
    host = getenv_compat("M3_POSTGRES_SERVER", "POSTGRES_SERVER") or getenv_compat("M3_SYNC_TARGET_IP", "SYNC_TARGET_IP")
    if host:
        return host.strip()

    # 2. Warehouse DSN (M3_CDW_PG_URL > PG_URL) — from env, or the encrypted
    #    vault. Parse out ONLY the host. This is the WAREHOUSE host, not the
    #    primary store, so it uses the CDW resolver (never M3_PG_URL).
    pg_url = (resolve_cdw_pg_dsn("") or "").strip()
    if not pg_url:
        try:
            # Resolve via the SDK's secret vault without spinning up a full
            # context if it's not importable in this environment.
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
            from m3_sdk import M3Context  # type: ignore
            pg_url = (M3Context.for_db().get_secret("PG_URL") or "").strip()
        except Exception:  # noqa: BLE001 — vault not available / not installed yet
            pg_url = ""
    if pg_url:
        try:
            from urllib.parse import urlparse
            return urlparse(pg_url).hostname  # host only — drops user:pass
        except Exception:  # noqa: BLE001
            return "your configured PostgreSQL warehouse"
    return None


def _print_backup_reminder() -> None:
    """Remind the user to back up their databases regularly.

    Branches on whether a CDW (PostgreSQL data warehouse) is configured:
      - No CDW: a plain "back up your local DBs" reminder.
      - CDW configured: note that auto-sync to the warehouse exists (and where),
        but that it is NOT a substitute for backups — the warehouse itself, and
        the local DBs, should be backed up to the user's risk tolerance.
    """
    cdw = _detect_cdw_target()
    print()
    print("  " + "-" * 68)
    print("  DATA SAFETY — back up your databases regularly")
    print("  Your memories, chatlog, and file index live in SQLite DBs under your")
    print("  engine root (M3_ENGINE_ROOT, default ~/.m3/engine). They are NOT")
    print("  backed up automatically by install/upgrade.")
    if cdw:
        print()
        print(f"  • You DO have auto-syncing configured to a data warehouse ({cdw}).")
        print("    That replicates your memories across machines, but it is NOT a")
        print("    backup: a deletion or corruption syncs too, and the warehouse")
        print("    itself is a single store. Back up BOTH the warehouse and your")
        print("    local DBs on a cadence that matches your risk tolerance.")
    else:
        print()
        print("  • Copy ~/.m3/engine/*.db somewhere safe periodically (or set up an")
        print("    automated backup). Tip: a data warehouse for cross-machine sync")
        print("    can be configured later (see docs/SYNC.md).")
    print("  " + "-" * 68)


def install_m3(
    repo_path: Optional[Path] = None,
    tag: Optional[str] = None,
    force: bool = False,
    interactive: Optional[bool] = None,
    endpoint: Optional[str] = None,
    capture_mode: Optional[str] = None,
    cognitive_loop: bool = False,
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

    # Fail LOUD if the deprecated, role-ambiguous PG_URL is set ANYWHERE we can
    # see. Install/upgrade is the one moment the operator is present to fix config,
    # so we force the rename here (runtime paths only warn). The scan sweeps EVERY
    # common location and reports ALL of them at once, so the fix is one pass, not
    # a re-run per hidden copy.
    _assert_no_deprecated_pg_url_anywhere()

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
    cognitive_loop_choice = _prompt_cognitive_loop(interactive, cognitive_loop)
    del cognitive_loop_choice  # placeholder: wired downstream once the cognitive-loop install path lands

    # Preserve user data across --force / update. The repo tree under
    # repo_path/memory/ holds chatlog DBs, the chatlog config, and the
    # migration-tracking schema_version table — wiping them on every update
    # would discard captured turns and force a re-init. Stash anything that
    # looks like user data, wipe the code tree, then restore.
    preserved_dir: Optional[Path] = None
    if repo_path.exists():
        if not force:
            raise RuntimeError(
                "m3 is already installed — nothing to do.\n"
                "  • To reconfigure (re-wire agents, change options):  m3 setup\n"
                "  • To upgrade to the current version:                m3 update\n"
                "  • To re-fetch the system files in place:            m3 install-m3 --force\n"
                f"  (installed at {repo_path}) Your memories and chatlog are preserved either way."
            )
        memory_dir = repo_path / "memory"
        if memory_dir.is_dir():
            preserved_dir = Path(tempfile.mkdtemp(prefix="m3-preserve-"))
            for item in memory_dir.iterdir():
                # Keep .db / .json (chatlog config + state) / .jsonl (cursor).
                # The migrations/ subdir ships with the repo and will be
                # restored by the new clone, so we don't preserve it.
                if not (item.is_file() and item.suffix in (".db", ".json", ".jsonl")):
                    continue
                dst = preserved_dir / item.name
                if item.suffix == ".db":
                    # A plain file copy of a live SQLite DB can miss in-WAL pages
                    # or capture a torn write if the server has the DB open.
                    # Use the SQLite Online Backup API, which produces a
                    # transactionally-consistent snapshot including the WAL.
                    _safe_copy_sqlite(item, dst)
                else:
                    shutil.copy2(item, dst)
            print(f"  preserving {sum(1 for _ in preserved_dir.iterdir())} user-data file(s) across update")
        print(f"  removing existing {repo_path}")
        # Robust delete: a bare rmtree aborts the whole install if a git pack
        # file under repo_path/.git is read-only or momentarily locked (WinError
        # 5) — which made a *successful* update report as a failure. Retry with
        # read-only-bit clearing + short backoff.
        _robust_rmtree(repo_path)

    # Guard: if the bin payload is packaged in the wheel and not forced,
    # skip the network fetch. The post-install setup still runs.
    if bin_dir() is not None and not force:
        print("[install-m3] payload already present (packaged in the wheel); skipping fetch.")
        bridge = bin_dir() / "memory_bridge.py"
        if not bridge.is_file():
            raise RuntimeError(
                f"payload bin_dir exists but {bridge} not found. This is unexpected; "
                f"check your installation."
            )
    else:
        print(f"fetching m3-memory {tag} -> {repo_path}")
        # _git_clone returns False only when git is missing; it RAISES on any other
        # failure (network, bad tag, exit 128). Because we already rmtree'd the old
        # repo above, an uncaught raise here would leave the user with a vanished
        # repo and no replacement (the 2026-06-08 incident). So fall back to the
        # GitHub tarball on EITHER path — missing git OR a failed clone.
        cloned = False
        try:
            cloned = _git_clone(tag, repo_path)
        except Exception as e:  # noqa: BLE001 — any clone failure -> try the tarball
            print(f"  git clone failed ({type(e).__name__}: {e}); falling back to GitHub tarball")
            # A partial clone may have left a dir behind; clear it so the tarball
            # extracts into a clean path.
            shutil.rmtree(repo_path, ignore_errors=True)
        if not cloned:
            print("  falling back to GitHub tarball")
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
        print("will remove:")
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



def status_summary() -> dict:
    """Compute a single-glance health verdict + the facts a user cares about.

    Returns {verdict, installed, memories, embedder, chatlog, headline}.
    verdict ∈ {healthy, degraded, broken}. Best-effort and fast; never raises.
    Used by `m3 status` (one-liner) and as the lead line of `m3 doctor`.
    """
    out: dict = {"verdict": "broken", "installed": False, "memories": None,
                 "embedder": "?", "chatlog": "?", "headline": ""}
    # 1. Is the payload installed/resolvable?
    bridge = find_bridge()
    out["installed"] = bool(bridge and bridge.is_file())

    cfg = load_config()
    # 2. Memory count (main DB), best-effort.
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
        from m3_sdk import get_m3_engine_root
        main_db = Path(get_m3_engine_root()) / "agent_memory.db"
        if main_db.is_file():
            conn = sqlite3.connect(f"file:{main_db.as_posix()}?mode=ro", uri=True, timeout=1.0)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM memory_items WHERE COALESCE(is_deleted,0)=0"
                ).fetchone()
                out["memories"] = int(row[0]) if row else 0
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        pass

    # 3. Embedder tier.
    try:
        from m3_memory.rust_core_install import active_embedder_tier
        out["embedder"] = "native (in-process)" if active_embedder_tier().get("native") \
            else "pure-Python (HTTP)"
    except Exception:  # noqa: BLE001
        pass

    # 4. Chatlog activity.
    db_path = _resolve_chatlog_db(cfg)
    if db_path is not None:
        stats = _chatlog_db_stats(db_path)
        if stats["ok"]:
            out["chatlog"] = f"active ({stats['rows']} rows)" if stats["rows"] else "wired (0 rows yet)"
        elif stats["error"] == "file not found":
            out["chatlog"] = "no captures yet"
        else:
            out["chatlog"] = "unreadable"

    # Verdict: broken if not installed; degraded if installed but a subsystem is
    # off; healthy otherwise.
    if not out["installed"]:
        out["verdict"] = "broken"
        out["headline"] = "NOT installed — run `m3 setup`"
    else:
        degraded = out["embedder"].startswith("pure-Python") or out["chatlog"] in ("unreadable",)
        out["verdict"] = "degraded" if degraded else "healthy"
        mem = "?" if out["memories"] is None else out["memories"]
        out["headline"] = (
            f"{out['verdict'].upper()} · {mem} memories · embedder: {out['embedder']} · "
            f"chatlog: {out['chatlog']}"
        )
    return out


def status() -> int:
    """`m3 status` — one-glance health line. Returns 0 healthy, 1 degraded/broken."""
    s = status_summary()
    icon = {"healthy": "[OK]", "degraded": "[~]", "broken": "[X]"}.get(s["verdict"], "[?]")
    print(f"{icon} m3 {s['headline']}")
    if s["verdict"] != "healthy":
        print("     run `m3 doctor` for details.")
    return 0 if s["verdict"] == "healthy" else 1


def _known_agent_settings() -> "list[tuple[str, Path]]":
    """Known agent MCP settings files, by host label.

    Only files that exist are worth scanning; callers filter. Kept in one place
    so doctor's scan and ``--fix`` heal cover the same set of hosts.
    """
    home = Path.home()
    return [
        ("Claude Code", home / ".claude" / "settings.json"),
        ("Gemini CLI",  home / ".gemini" / "settings.json"),
        ("Antigravity", home / ".gemini" / "antigravity-cli" / "settings.json"),
        ("OpenCode",    home / ".opencode" / "settings.json"),
        ("Aider",       home / ".aider" / "settings.json"),
    ]


def _scan_agent_configs() -> "list[tuple[str, Path, bool]]":
    """Return (label, path, needs_repoint) for every existing agent config that
    declares a ``memory`` MCP entry. ``needs_repoint`` is True when its paths are
    dead/moved — the split-brain signature ``doctor --fix`` repairs.
    """
    out = []
    for label, path in _known_agent_settings():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            out.append((label, path, True))  # unreadable counts as a problem
            continue
        entry = (data.get("mcpServers") or {}).get("memory")
        if entry is None:
            continue  # host present but m3 not wired there — not our concern here
        out.append((label, path, _memory_entry_needs_repoint(entry)))
    return out


def _client_config_sources() -> "dict[str, list[Path]]":
    """Map each MCP CLIENT to the config files IT reads. Duplication that causes a
    double-launch is a server appearing in >1 file THE SAME CLIENT reads — NOT the
    same server across different clients (Claude/Gemini/Antigravity each legitimately
    registering `memory` is correct multi-client setup, not a bug).

    Claude Code reads BOTH its global ~/.claude/settings.json AND a project-local
    .mcp.json (cwd / repo root) — that pair is the axis that double-launched the
    memory bridge (2026-07-02). Other clients read a single settings.json each."""
    home = Path.home()
    claude_sources = [home / ".claude" / "settings.json"]
    for d in (Path.cwd(), default_repo_path().parent):
        cand = d / ".mcp.json"
        if cand not in claude_sources:
            claude_sources.append(cand)
    return {
        "Claude Code": claude_sources,
        "Gemini CLI":  [home / ".gemini" / "settings.json"],
        "Antigravity": [home / ".gemini" / "antigravity-cli" / "settings.json"],
        "OpenCode":    [home / ".opencode" / "settings.json"],
        "Aider":       [home / ".aider" / "settings.json"],
    }


def _duplicate_mcp_registration() -> "dict[str, dict[str, list[Path]]]":
    """Return {client: {server_name: [files...]}} for servers declared in MORE THAN
    ONE file THE SAME CLIENT reads — the real double-launch signature. Same server
    across different clients is NOT flagged (that's normal multi-client use).
    Read-only, best-effort (unreadable files skipped)."""
    out: "dict[str, dict[str, list[Path]]]" = {}
    for client, sources in _client_config_sources().items():
        seen: "dict[str, list[Path]]" = {}
        for path in sources:
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError):
                continue
            for name in (data.get("mcpServers") or {}):
                seen.setdefault(name, []).append(path)
        client_dupes = {n: fs for n, fs in seen.items() if len(fs) > 1}
        if client_dupes:
            out[client] = client_dupes
    return out


def _dedupe_mcp_registration(*, apply: bool = False) -> "list[str]":
    """Resolve same-client duplicate MCP registrations by keeping ONE copy and
    removing the redundant ones. For each client + duplicated server, keep the
    file whose def is most complete (has an ``env`` block; tie-break: the first
    source = the user settings.json), and drop the server from the other files.

    apply=False (default): dry-run, returns human-readable "would remove ..." lines.
    apply=True: rewrites the affected files (a .bak is written first) and returns
    "removed ..." lines. Idempotent — a clean config yields [].

    This is the automated cure `m3 doctor --fix` runs for the double-launch bug.
    """
    actions: list[str] = []
    dupes = _duplicate_mcp_registration()
    for client, servers in dupes.items():
        for name, files in servers.items():
            # Pick the keeper: prefer a file whose entry has an 'env' block.
            keeper = None
            for f in files:
                try:
                    entry = (json.loads(f.read_text(encoding="utf-8")).get("mcpServers") or {}).get(name) or {}
                except (OSError, json.JSONDecodeError):
                    entry = {}
                if entry.get("env"):
                    keeper = f
                    break
            if keeper is None:
                keeper = files[0]  # tie-break: first source (user settings.json)
            for f in files:
                if f == keeper:
                    continue
                verb = "removed" if apply else "would remove"
                actions.append(f"[+] {verb} duplicate '{name}' from {f} (kept {keeper})")
                if apply:
                    try:
                        data = json.loads(f.read_text(encoding="utf-8")) or {}
                        f.with_suffix(f.suffix + ".bak").write_text(
                            f.read_text(encoding="utf-8"), encoding="utf-8"
                        )
                        (data.get("mcpServers") or {}).pop(name, None)
                        f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                    except (OSError, json.JSONDecodeError) as e:
                        actions[-1] = f"[X] could not dedup '{name}' in {f}: {e}"
    return actions


def _deprecated_env_in_config() -> "dict[Path, dict[str, str]]":
    """Return {file: {OLD: NEW}} for on-disk config that still uses a deprecated
    (pre-M3_ namespacing) env var name. Scans the SAME config files
    ``_client_config_sources()`` reads (settings.json per client, plus Claude's
    .mcp.json), walking every ``mcpServers.<server>.env`` block, plus a plain
    ``.env`` (KEY=VALUE) at the cwd if present. Uses ``all_env_renames()``
    (bin/m3_core/paths.py — the union of the pure-namespacing map and the
    role-split map, e.g. PG_URL -> M3_CDW_PG_URL) as the sole source of truth for
    what counts as an old name — never a second hardcoded copy.

    Only files with >=1 old name are included. Tolerant: unreadable/unparseable
    files are skipped, never raised.
    """
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
    from m3_core.paths import all_env_renames  # type: ignore

    # Union of pure-namespacing + role-split (e.g. PG_URL -> M3_CDW_PG_URL) renames.
    DEPRECATED_ENV_RENAMES = all_env_renames()

    out: "dict[Path, dict[str, str]]" = {}

    seen_files: "set[Path]" = set()
    for sources in _client_config_sources().values():
        for path in sources:
            seen_files.add(path)

    for path in seen_files:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            continue
        found: "dict[str, str]" = {}
        for entry in (data.get("mcpServers") or {}).values():
            env_block = (entry or {}).get("env") or {}
            for key in env_block:
                if key in DEPRECATED_ENV_RENAMES:
                    found[key] = DEPRECATED_ENV_RENAMES[key]
        if found:
            out[path] = found

    dotenv = Path.cwd() / ".env"
    if dotenv.is_file():
        try:
            found = {}
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip()
                if key in DEPRECATED_ENV_RENAMES:
                    found[key] = DEPRECATED_ENV_RENAMES[key]
            if found:
                out[dotenv] = found
        except OSError:
            pass

    return out


# Windows persistent-environment registry scopes. Each entry:
#   (winreg-hive-attr-name, subkey path, User|Machine label, whether writable
#    without admin). HKCU is User-writable; HKLM needs admin so --fix reports it.
_WIN_ENV_REG_SCOPES = (
    ("HKEY_CURRENT_USER", r"Environment", "User", True),
    ("HKEY_LOCAL_MACHINE",
     r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "Machine", False),
)


def _all_env_renames() -> "dict[str, str]":
    """The union deprecation map (pure-namespacing + role-split). Best-effort:
    returns {} if the payload SDK isn't importable (thin pip pre-payload)."""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
        from m3_core.paths import all_env_renames  # type: ignore

        return all_env_renames()
    except Exception:  # noqa: BLE001
        return {}


def _scan_registry_env_deprecations(names: "dict[str, str]") -> "list[dict]":
    """Return one record per deprecated env var found in a Windows registry env
    scope. Each record: {scope, label, subkey, hive_name, old, new, writable}.
    Non-Windows or unreadable → []. Read-only; never raises."""
    if sys.platform != "win32" or not names:
        return []
    hits: "list[dict]" = []
    try:
        import winreg  # noqa: PLC0415 — Windows-only, lazy
    except Exception:  # noqa: BLE001
        return hits
    for hive_name, subkey, scope, writable in _WIN_ENV_REG_SCOPES:
        hive = getattr(winreg, hive_name)
        try:
            with winreg.OpenKey(hive, subkey) as k:
                for old, new in names.items():
                    try:
                        winreg.QueryValueEx(k, old)
                    except FileNotFoundError:
                        continue  # this var not set at this scope
                    hits.append({
                        "scope": scope, "label": f"Windows {scope} env ({hive_name}\\{subkey})",
                        "subkey": subkey, "hive_name": hive_name,
                        "old": old, "new": new, "writable": writable,
                    })
        except FileNotFoundError:
            continue  # scope key absent
        except OSError:
            continue  # e.g. no read permission on HKLM — best-effort
    return hits


def _find_deprecated_pg_url_locations() -> "list[str]":
    """Return a human-readable location for EVERY place a deprecated PG_URL is set.

    Sweeps all common locations and collects them all (does NOT stop at the first
    hit — PG_URL can be set in several places at once, and the operator should see
    them all in one pass rather than re-running install per hidden copy):

      1. the live process environment (``os.environ``);
      2. every MCP-client config file's ``mcpServers.<server>.env`` block plus the
         cwd ``.env`` — via ``_deprecated_env_in_config`` (already multi-file);
      3. shell startup files in ``$HOME`` (``.zshenv``, ``.zshrc``, ``.bashrc``,
         ``.bash_profile``, ``.profile``, ``.zprofile``, ``.bash_login``) that
         ``export``/set ``PG_URL`` — the usual home of a persistent DSN.

    Best-effort and never raises: an unreadable file or a config-scan import
    failure is skipped, not fatal. Returns a de-duplicated, sorted list of
    location strings (empty when clean)."""
    import re as _re

    locations: "set[str]" = set()

    # 1. Live process env.
    if os.environ.get("PG_URL") is not None:
        locations.add("environment (os.environ['PG_URL'])")

    # 2. MCP client config env blocks + cwd .env — reuse the existing multi-file
    #    scanner, which already walks every client's settings.json and .mcp.json.
    try:
        for path, renames in _deprecated_env_in_config().items():
            if "PG_URL" in renames:
                locations.add(str(path))
    except Exception:  # noqa: BLE001 — informational scan, never fatal
        pass

    # 3. Shell startup files that set PG_URL. Match `PG_URL=` optionally preceded
    #    by `export ` / `set ` / whitespace, at a line start, so `M3_CDW_PG_URL=`
    #    and comments don't false-positive.
    _pg = _re.compile(r'^\s*(?:export\s+|set\s+)?PG_URL\s*=', _re.MULTILINE)
    home = Path.home()
    for name in (".zshenv", ".zshrc", ".zprofile", ".bashrc",
                 ".bash_profile", ".bash_login", ".profile"):
        f = home / name
        try:
            if f.is_file() and _pg.search(f.read_text(encoding="utf-8", errors="ignore")):
                locations.add(str(f))
        except OSError:
            continue

    # 4. Windows PERSISTENT env vars live in the registry, not a profile file, so
    #    a User/Machine-scope PG_URL wouldn't be caught by (1)-(3) beyond the
    #    inherited process copy. Read HKCU\Environment (User) and the Session
    #    Manager Environment (Machine) so the operator is told WHERE the persistent
    #    value lives (unsetting the process copy alone wouldn't stick).
    for hit in _scan_registry_env_deprecations({"PG_URL": "M3_CDW_PG_URL"}):
        locations.add(hit["label"])

    return sorted(locations)


def _assert_no_deprecated_pg_url_anywhere() -> None:
    """Hard-fail install/upgrade if a deprecated PG_URL is set in ANY scanned
    location, listing every one so the operator fixes them in a single pass.

    PG_URL was split by role — ``M3_CDW_PG_URL`` (data-warehouse / pg_sync) vs
    ``M3_PRIMARY_PG_URL`` (a PostgreSQL PRIMARY store) — so an ambiguous PG_URL
    must be renamed to the correct one before proceeding."""
    locations = _find_deprecated_pg_url_locations()
    if not locations:
        return
    bullet = "\n  - ".join(locations)
    raise RuntimeError(
        "Deprecated env var PG_URL is set. It has been split by role and renamed: "
        "use M3_CDW_PG_URL for the data-warehouse (pg_sync) DSN, or "
        "M3_PRIMARY_PG_URL for a PostgreSQL PRIMARY store.\n"
        f"Found PG_URL in {len(locations)} location(s) — fix ALL of them, then "
        f"re-run install/update:\n  - {bullet}\n"
        "(See CHANGELOG: 'PG_URL split by role'.)"
    )


def _migrate_env_names(*, apply: bool = False) -> "list[str]":
    """Rename deprecated env var KEYS to their M3_ names in on-disk config.
    Mirrors ``_dedupe_mcp_registration``'s contract exactly.

    apply=False (default): dry-run, returns "would rename ..." lines, no writes.
    apply=True: rewrites each affected file (a .bak is written FIRST with the
    original contents), renaming the env KEY in place while preserving its
    VALUE, then returns "renamed ..." lines. Idempotent — a clean config (or a
    second run right after) yields [].

    Conflict rule: if BOTH the old and new name are already present in the same
    env block, the old key is dropped WITHOUT touching the new value (matches
    getenv_compat's new > old precedence) — the old value is discarded, not
    merged in, since the new value already wins at read time.

    Never let one bad file abort the rest (try/except per file, error noted).
    """
    actions: list[str] = []
    affected = _deprecated_env_in_config()

    for path, renames in affected.items():
        try:
            if path.name == ".env":
                actions.extend(_migrate_dotenv_file(path, renames, apply=apply))
            else:
                actions.extend(_migrate_json_config_file(path, renames, apply=apply))
        except (OSError, json.JSONDecodeError) as e:
            actions.append(f"[X] could not migrate env names in {path}: {e}")

    # Windows persistent registry env vars (User scope written; Machine reported).
    actions.extend(_migrate_registry_env_names(apply=apply))

    return actions


def _migrate_registry_env_names(*, apply: bool = False) -> "list[str]":
    """Rename deprecated Windows registry env vars to their new names, User scope.

    Config-file renames (``_migrate_env_names``) can't reach a persistent env var
    set in the Windows registry; without this, ``m3 doctor --fix`` would report a
    clean run while a registry ``PG_URL`` still shadows behavior and install still
    hard-fails. This closes that gap.

    Scope policy: **HKCU (User) is rewritten** — it needs no admin. **HKLM
    (Machine) is REPORTED, never written** (would need elevation); the operator
    gets the exact command. Same conflict rule as the config path: if the NEW name
    already exists at that scope, the OLD one is DROPPED (new wins), else RENAMED.
    The old value is logged (backup) before any delete. Non-Windows → []. After a
    write, a WM_SETTINGCHANGE broadcast asks running shells to reload env.

    apply=False: dry-run ("would rename/drop ..."). apply=True: performs HKCU
    writes. Best-effort and per-var isolated: a failure on one var is noted, never
    aborts the rest. Idempotent."""
    actions: list[str] = []
    names = _all_env_renames()
    hits = _scan_registry_env_deprecations(names)
    if not hits:
        return actions
    try:
        import winreg  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        actions.append(f"[X] winreg unavailable, cannot migrate registry env: {e}")
        return actions

    for hit in hits:
        old, new, scope = hit["old"], hit["new"], hit["scope"]
        label = hit["label"]
        if not hit["writable"]:
            # HKLM — report the manual, admin-required command; never auto-write.
            actions.append(
                f"[!] {label}: {old} must be renamed to {new} manually (admin): "
                f"setx /M {new} \"%{old}%\" && REG delete "
                f"\"HKLM\\{hit['subkey']}\" /v {old} /f"
            )
            continue
        hive = getattr(winreg, hit["hive_name"])
        try:
            with winreg.OpenKey(hive, hit["subkey"], 0, winreg.KEY_READ) as k:
                old_val, old_type = winreg.QueryValueEx(k, old)
                new_exists = True
                try:
                    winreg.QueryValueEx(k, new)
                except FileNotFoundError:
                    new_exists = False
        except OSError as e:
            actions.append(f"[X] {label}: could not read {old}: {e}")
            continue

        if new_exists:
            verb = "dropped" if apply else "would drop"
            actions.append(
                f"[+] {verb} superseded {old} ({new} already set) in {label} "
                f"(old value preserved under {new}; {old} removed)"
            )
            if apply:
                try:
                    with winreg.OpenKey(hive, hit["subkey"], 0, winreg.KEY_SET_VALUE) as k:
                        winreg.DeleteValue(k, old)
                    _broadcast_env_change()
                except OSError as e:
                    actions.append(f"[X] {label}: could not delete {old}: {e}")
        else:
            verb = "renamed" if apply else "would rename"
            actions.append(
                f"[+] {verb} {old} -> {new} in {label} (value carried over)"
            )
            if apply:
                try:
                    with winreg.OpenKey(hive, hit["subkey"], 0, winreg.KEY_SET_VALUE) as k:
                        winreg.SetValueEx(k, new, 0, old_type, old_val)
                        winreg.DeleteValue(k, old)
                    _broadcast_env_change()
                except OSError as e:
                    actions.append(f"[X] {label}: could not rename {old}->{new}: {e}")
    return actions


def _broadcast_env_change() -> None:
    """Ask running processes to reload the environment after a registry env write
    (Windows broadcasts WM_SETTINGCHANGE with 'Environment'). Best-effort — the
    change is already persisted; this only nudges live shells. Never raises."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x1A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()),
        )
    except Exception:  # noqa: BLE001
        pass


def _migrate_json_config_file(path: Path, renames: "dict[str, str]", *, apply: bool) -> "list[str]":
    """Rename OLD->NEW env keys inside every mcpServers.<name>.env block of a
    single JSON config file. Raises on I/O or parse error (caller handles)."""
    actions: list[str] = []
    original = path.read_text(encoding="utf-8")
    data = json.loads(original) or {}
    changed = False
    for entry in (data.get("mcpServers") or {}).values():
        env_block = (entry or {}).get("env")
        if not isinstance(env_block, dict):
            continue
        for old, new in renames.items():
            if old not in env_block:
                continue
            if new in env_block:
                verb = "dropped" if apply else "would drop"
                actions.append(f"[+] {verb} superseded {old} ({new} already set) in {path}")
                if apply:
                    env_block.pop(old, None)
                    changed = True
            else:
                verb = "renamed" if apply else "would rename"
                actions.append(f"[+] {verb} {old} -> {new} in {path}")
                if apply:
                    env_block[new] = env_block.pop(old)
                    changed = True
    if apply and changed:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return actions


# The m3 MCP server names whose env blocks must never carry M3_EMBED_GGUF (it
# forces a per-process CUDA embedder — the hang footgun). Only the memory server
# reads it, but scrub any m3-owned block defensively.
_M3_MCP_SERVER_NAMES = ("memory",)


def _scrub_embed_gguf_from_settings(path: Path, *, apply: bool) -> "list[str]":
    """Remove M3_EMBED_GGUF from m3 MCP-server env blocks in one settings file.

    An env-var GGUF makes the bridge open its OWN CUDA context, which can hang
    the read/write path indefinitely (§3/§6). Shared mode (.embed_config.json)
    is the correct home for the model path. This auto-heals installs already in
    the bad state, on every upgrade and via `m3 doctor --fix`. Idempotent: a
    clean file yields no actions. Backs up to <file>.bak before writing.
    Raises on I/O or parse error (caller handles)."""
    actions: list[str] = []
    if not path.exists():
        return actions
    original = path.read_text(encoding="utf-8")
    data = json.loads(original) or {}
    changed = False
    servers = data.get("mcpServers") or {}
    for name, entry in servers.items():
        env_block = (entry or {}).get("env")
        if not isinstance(env_block, dict) or "M3_EMBED_GGUF" not in env_block:
            continue
        # Scrub the m3 memory server (the one that reads it) — and any block that
        # points at a bridge/embed script, to be safe on renamed servers.
        args_blob = " ".join((entry or {}).get("args") or [])
        is_m3 = name in _M3_MCP_SERVER_NAMES or "memory_bridge" in args_blob or "m3" in name.lower()
        if not is_m3:
            continue
        verb = "scrubbed" if apply else "would scrub"
        actions.append(f"[+] {verb} M3_EMBED_GGUF from mcpServers.{name}.env in {path}")
        if apply:
            env_block.pop("M3_EMBED_GGUF", None)
            changed = True
    if apply and changed:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return actions


def _heal_embed_gguf_env_leak(*, apply: bool) -> "list[str]":
    """Scrub M3_EMBED_GGUF from every known agent settings file AND seed the
    shared config, so an upgrade auto-heals the per-process-CUDA hang footgun.
    Best-effort per file (a bad file is warned, not fatal). Returns actions."""
    actions: list[str] = []
    for _label, path in _known_agent_settings():
        try:
            actions.extend(_scrub_embed_gguf_from_settings(path, apply=apply))
        except Exception as e:  # noqa: BLE001 — one bad file must not abort the sweep
            actions.append(f"[!] could not scrub {path}: {type(e).__name__}: {e}")
    # Seed the shared config so clients have somewhere to defer to after the scrub.
    if apply:
        try:
            from m3_memory.embedder_admin import seed_shared_config
            _p, wrote = seed_shared_config()
            if wrote:
                actions.append(f"[+] seeded shared embedder config: {_p}")
        except Exception as e:  # noqa: BLE001
            actions.append(f"[!] could not seed shared config: {type(e).__name__}: {e}")
    return actions


def _migrate_dotenv_file(path: Path, renames: "dict[str, str]", *, apply: bool) -> "list[str]":
    """Rename OLD->NEW KEY= tokens line-wise in a plain .env file, preserving
    values and comments. Raises on I/O error (caller handles)."""
    actions: list[str] = []
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    existing_keys: "set[str]" = set()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            existing_keys.add(stripped.split("=", 1)[0].strip())

    new_lines: "list[str]" = []
    changed = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        key, _, rest = stripped.partition("=")
        key = key.strip()
        new = renames.get(key)
        if new is None:
            new_lines.append(line)
            continue
        if new in existing_keys:
            verb = "dropped" if apply else "would drop"
            actions.append(f"[+] {verb} superseded {key} ({new} already set) in {path}")
            changed = True
            continue  # old line dropped entirely; new's own line already carries the value
        verb = "renamed" if apply else "would rename"
        actions.append(f"[+] {verb} {key} -> {new} in {path}")
        changed = True
        ending = "\n" if line.endswith("\n") else ""
        new_lines.append(f"{new}={rest}".rstrip("\n") + ending)

    if apply and changed:
        path.with_suffix(path.suffix + ".bak").write_text(original, encoding="utf-8")
        path.write_text("".join(new_lines), encoding="utf-8")
    return actions


def _deprecated_env_config_section() -> None:
    """Doctor section: flag on-disk config still using deprecated env var names.

    Mirrors ``_duplicate_registration_section``. Distinct from
    ``m3_memory.install.sections._deprecated_env_section`` (which reports names
    actually READ by THIS process via getenv_compat) — this one scans config
    FILES so it catches names sitting unread in someone else's settings.json.
    """
    affected = _deprecated_env_in_config()
    if not affected:
        return  # clean — no noise
    print()
    print("  [!] deprecated env var NAMES found in on-disk config (still work via "
          "back-compat, but should migrate to the M3_ namespace):")
    for path, renames in sorted(affected.items(), key=lambda kv: str(kv[0])):
        for old, new in sorted(renames.items()):
            print(f"        {path}: {old}  ->  {new}")
    print("      → Run `m3 doctor --fix` to rename these into the M3_ namespace")
    print("        automatically (a .bak is written; the old names still work")
    print("        until you do).")


def _live_bridge_counts() -> "dict[str, int]":
    """Count live m3 bridge processes by script name (memory_bridge, etc.). >1 of
    any is the process-level signature of double-launch. Returns {} if psutil is
    unavailable (best-effort — the config check above is the primary signal)."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return {}
    counts: "dict[str, int]" = {}
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
        except Exception:  # noqa: BLE001
            continue
        for script in ("memory_bridge.py", "grok_bridge.py", "web_research_bridge.py",
                       "debug_agent_bridge.py", "custom_tool_bridge.py", "mcp_proxy.py"):
            if script in cmd:
                counts[script] = counts.get(script, 0) + 1
    return {s: n for s, n in counts.items() if n > 1}


def _duplicate_registration_section() -> None:
    """Doctor section: flag MCP servers registered in >1 config, or >1 live bridge.

    Duplicate registration is invisible until a call hangs (a memory_write routed
    to a redundant/mis-configured bridge blocks with no response — the 2h43m hang,
    2026-07-02). Surfacing it here makes the recurrence loud, not silent."""
    dupes = _duplicate_mcp_registration()
    live = _live_bridge_counts()
    if not dupes and not live:
        return  # clean — no noise
    print()
    if dupes:
        print("  [!] DUPLICATE MCP registration — a server is declared in MULTIPLE "
              "config files the SAME client reads, so that client launches it TWICE:")
        for client, servers in sorted(dupes.items()):
            for name, files in sorted(servers.items()):
                print(f"        {client} / {name}: " + " AND ".join(str(f) for f in files))
        print("      → Run `m3 doctor --fix` to remove the duplicate automatically "
              "(keeps the complete def with its env block, backs up the edited file).")
        print("        Then restart the MCP client so it relaunches a single bridge.")
    if live:
        print("  [!] Multiple live bridge processes (double-launch signature):")
        for script, n in sorted(live.items()):
            print(f"        {n}x {script}")
        print("      → A duplicate bridge can wedge a tool call indefinitely on old "
              "(no-timeout) code. Run `m3 doctor --fix` to de-duplicate the config,")
        print("        then restart the MCP client (that clears the extra processes).")


def _agent_config_section() -> None:
    """Doctor section: per-host m3 ``memory`` MCP config health (read-only)."""
    scanned = _scan_agent_configs()
    print()
    print("agent MCP configs:")
    if not scanned:
        print("  (no agent config declares an m3 'memory' server)")
        return
    any_bad = False
    for label, path, bad in scanned:
        if bad:
            any_bad = True
            print(f"  [X] {label:<12} {path}  -> bridge/root paths are dead or moved")
        else:
            print(f"  [OK] {label:<12} {path}")
    if any_bad:
        print("  [i] Run `m3 doctor --fix` to repoint the broken configs to the live install.")


def _heal_all_agents(*, force: bool = False) -> int:
    """Repoint every broken (or all, if force) agent ``memory`` config. Returns
    the count of files changed. Used by ``m3 doctor --fix`` and ``m3 setup``.
    """
    changed = 0
    for label, path in _known_agent_settings():
        msg = _heal_agent_settings(path, force=force)
        if msg:
            print(f"  {msg}")
            if msg.lstrip().startswith("[+]"):
                changed += 1
    # Auto-remove same-client duplicate registrations (the double-launch cause).
    for line in _dedupe_mcp_registration(apply=True):
        print(f"  {line}")
        if line.lstrip().startswith("[+]"):
            changed += 1
    # Auto-migrate deprecated env var names to the M3_ namespace.
    for line in _migrate_env_names(apply=True):
        print(f"  {line}")
        if line.lstrip().startswith("[+]"):
            changed += 1
    # Auto-heal the per-process-CUDA hang footgun: scrub M3_EMBED_GGUF from m3
    # MCP-server env blocks and seed the shared config so clients defer to :8082.
    for line in _heal_embed_gguf_env_leak(apply=True):
        print(f"  {line}")
        if line.lstrip().startswith("[+]"):
            changed += 1
    return changed


def doctor(fix: bool = False, brief: bool = False) -> int:
    """Print diagnostic info and return 0 on healthy, 1 on missing payload.

    brief=True prints only the high-yield verdict + agent-wiring lines and the
    resolved-bridge check, skipping the verbose path/version block — for
    `m3 doctor --brief` and the GUI's compact health view.
    """
    from m3_memory import __version__

    # Lead with the verdict — the one thing the user actually wants to know.
    _s = status_summary()
    _icon = {"healthy": "[OK]", "degraded": "[~]", "broken": "[X]"}.get(_s["verdict"], "[?]")
    print(f"{_icon} m3 {_s['headline']}")
    print()

    # Brief: verdict (above) + agent wiring + bridge check; skip path/version wall.
    if brief:
        if fix:
            n = _heal_all_agents()
            print(f"agent MCP configs: {n} repointed." if n
                  else "agent MCP configs: all healthy.")
        else:
            _agent_config_section()
        _duplicate_registration_section()
        _deprecated_env_config_section()
        bridge = find_bridge()
        if bridge and bridge.is_file():
            print(f"[OK] resolved bridge: {bridge}")
            return 0
        print("[X] no bridge found. Run `mcp-memory install-m3` to fetch the system.")
        print("\nFor full detail, run:  m3 doctor --verbose")
        return 1

    root = os.environ.get("M3_MEMORY_ROOT")
    root_src = "(M3_MEMORY_ROOT env)" if root else "(default)"

    print(f"m3-memory package version: {__version__}")
    print(f"M3 root directory:         {config_dir()} {root_src}")
    print(f"config file:               {config_file()}")
    cfg = load_config()
    # The config's version/tag/installed_at describe the LAST `install-m3` fetch
    # — which is NOT necessarily what's running if the live bridge resolves via
    # M3_BRIDGE_PATH or the developer-sibling (a different checkout). Detect that
    # divergence so we don't present a stale version as if it's live.
    resolved = find_bridge()
    cfg_bridge = cfg.get("bridge_path") if cfg else None
    bridge_matches_config = bool(
        resolved and cfg_bridge
        and Path(resolved).resolve() == Path(cfg_bridge).expanduser().resolve()
    )
    if cfg:
        # When the live bridge matches the config, these ARE the installed
        # version. When it diverges (M3_BRIDGE_PATH / dev checkout), they're just
        # the record of the last fetch — label them so, and point at the live code.
        if bridge_matches_config:
            print(f"  installed version:       {cfg.get('version', '?')}")
            print(f"  installed tag:           {cfg.get('tag', '?')}")
            print(f"  installed at:            {cfg.get('installed_at', '?')}")
        else:
            print(f"  last fetch version:      {cfg.get('version', '?')}  (NOT the live code — see below)")
            print(f"  last fetch tag:          {cfg.get('tag', '?')}")
            print(f"  last fetch at:           {cfg.get('installed_at', '?')}")
        print(f"  repo_path:               {cfg.get('repo_path', '?')}")
        if not bridge_matches_config and resolved:
            print("  [i] LIVE bridge differs from the fetch record above:")
            print(f"      running code:        {resolved}")
            print(f"      live package version: {__version__}")
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

    _roots_section()

    _deprecated_env_section()

    _chatlog_section(cfg)

    _embedder_tier_section()

    _crypto_section()

    if fix:
        print()
        print("agent MCP configs: applying --fix (repointing broken configs)")
        n = _heal_all_agents()
        print(f"  {n} config(s) repointed." if n else "  nothing to repoint — all healthy.")
    else:
        _agent_config_section()

    _duplicate_registration_section()

    _deprecated_env_config_section()

    print()
    bridge = find_bridge()
    if bridge and bridge.is_file():
        print(f"[OK] resolved bridge: {bridge}")
        return 0
    print("[X] no bridge found. Run `mcp-memory install-m3` to fetch the system.")
    return 1
