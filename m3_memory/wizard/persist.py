"""Persistence backends for the setup wizard: shell-rc and MCP-server-env
writers for M3_EMBED_GGUF and arbitrary name/value env vars.

Extracted verbatim from setup_wizard.py. These backends are NOT themselves
monkeypatched by tests (only the top-level `_persist_env_var` /
`_persist_embed_gguf` wrappers in setup_wizard.py are) — confirmed via
`grep -rn 'setattr.*"<fn>"' tests/` before moving. setup_wizard.py imports
them back so `setup_wizard._pick_unix_shell_rc` etc. keep resolving (tests
reference `_pick_unix_shell_rc` directly as a setup_wizard attribute).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .ui import _ok, _warn

# _ask_yes_no is a monkeypatch target that must stay resolved through the
# setup_wizard module object at call time (not a bound import), so these
# backends import the setup_wizard module lazily inside each function body
# and call `_wizard_mod._ask_yes_no(...)` rather than importing the name
# directly. This preserves patch visibility for callers of these backends.


def _wizard_mod():
    from m3_memory import setup_wizard as _sw
    return _sw


def _persist_embed_gguf_shell(gguf_path: str, *, non_interactive: bool) -> None:
    """Persist M3_EMBED_GGUF for new shell sessions (per-platform mechanism)."""
    sw = _wizard_mod()
    if sys.platform == "win32":
        # Windows: setx writes to HKCU\Environment. Persists across reboot;
        # new cmd / PowerShell sessions see it. The current process and
        # other already-open shells are unaffected (by design).
        if not non_interactive and not sw._ask_yes_no(
            "  Persist M3_EMBED_GGUF to your Windows user environment (setx)?",
            default=True,
        ):
            _warn(f"    skipped — set it later: setx M3_EMBED_GGUF \"{gguf_path}\"")
            return
        try:
            result = subprocess.run(
                ["setx", "M3_EMBED_GGUF", gguf_path],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _warn(f"    setx failed ({e}); set it later: setx M3_EMBED_GGUF \"{gguf_path}\"")
            return
        if result.returncode == 0:
            _ok("    persisted M3_EMBED_GGUF via setx (new shells will see it)")
        else:
            stderr = (result.stderr or result.stdout or "").strip()
            _warn(f"    setx exited {result.returncode}: {stderr}")
        return

    # Unix: append `export M3_EMBED_GGUF=...` to the appropriate shell rc.
    rc_path = _pick_unix_shell_rc()

    if not non_interactive and not sw._ask_yes_no(
        f"  Persist M3_EMBED_GGUF to {rc_path}?", default=True
    ):
        _warn(f"    skipped — set it later: echo 'export M3_EMBED_GGUF={gguf_path}' >> {rc_path}")
        return

    try:
        existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    except OSError as e:
        _warn(f"    could not read {rc_path} ({e}); skipping shell rc persistence")
        return

    if "M3_EMBED_GGUF" in existing:
        _ok(f"    M3_EMBED_GGUF already present in {rc_path}")
        return

    block = (
        "\n# Added by m3 setup — tier-1 in-process BGE-M3 embedder\n"
        f'export M3_EMBED_GGUF="{gguf_path}"\n'
    )
    try:
        with rc_path.open("a", encoding="utf-8") as f:
            f.write(block)
        _ok(f"    persisted M3_EMBED_GGUF -> {rc_path}")
    except OSError as e:
        _warn(f"    failed to write {rc_path} ({e})")


def _pick_unix_shell_rc() -> Path:
    """Pick the shell rc file most likely to be read on this Unix system.

    Order:
      1. ~/.zshrc if SHELL points at zsh (macOS default since Catalina)
      2. ~/.bashrc if SHELL points at bash (most Linux distros)
      3. First existing among (~/.zshrc, ~/.bashrc, ~/.bash_profile, ~/.profile)
      4. Default to ~/.zshrc (covers fresh macOS Spotlight users)
    """
    home = Path.home()
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    for candidate in (home / ".zshrc", home / ".bashrc",
                      home / ".bash_profile", home / ".profile"):
        if candidate.exists():
            return candidate
    return home / ".zshrc"


def _persist_embed_gguf_mcp(gguf_path: str) -> None:
    """Patch the 'memory' MCP server entry's env block on every platform.

    MCP servers are spawned by Claude Code / Gemini CLI as subprocesses; on
    macOS (launchd) and Windows (GUI process tree) they do not inherit the
    user's interactive shell env. Setting the env on the MCP server entry
    itself is the only reliable way the spawned server sees M3_EMBED_GGUF.

    Same code on all 3 platforms — Path.home() resolves to ~/, %USERPROFILE%,
    or /home/<user> as appropriate.
    """
    for label, settings_path in (
        ("Claude Code", Path.home() / ".claude" / "settings.json"),
        ("Gemini CLI",  Path.home() / ".gemini" / "settings.json"),
    ):
        if not settings_path.is_file():
            continue
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as e:
            _warn(f"    {settings_path} is unreadable ({e}); skipping {label} env wiring")
            continue
        mcp = cfg.get("mcpServers")
        if not isinstance(mcp, dict) or "memory" not in mcp:
            # Memory MCP not yet registered — per-agent wiring step (later
            # in setup) will create it. We don't pre-create here to avoid
            # racing the wiring step's idempotency check.
            continue
        server = mcp["memory"]
        env = server.setdefault("env", {})
        if env.get("M3_EMBED_GGUF") == gguf_path:
            _ok(f"    M3_EMBED_GGUF already set on {label} memory MCP entry")
            continue
        env["M3_EMBED_GGUF"] = gguf_path
        try:
            settings_path.write_text(
                json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
            )
            _ok(f"    set M3_EMBED_GGUF on {label} memory MCP entry ({settings_path})")
        except OSError as e:
            _warn(f"    failed to write {settings_path} ({e})")


def _persist_env_var_shell(name: str, value: str, *, non_interactive: bool) -> None:
    """Persist <name>=<value> for new shell sessions (per-platform)."""
    sw = _wizard_mod()
    if sys.platform == "win32":
        if not non_interactive and not sw._ask_yes_no(
            f"  Persist {name} to your Windows user environment (setx)?", default=True
        ):
            _warn(f'    skipped — set it later: setx {name} "{value}"')
            return
        try:
            result = subprocess.run(
                ["setx", name, value], capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _warn(f'    setx failed ({e}); set it later: setx {name} "{value}"')
            return
        if result.returncode == 0:
            _ok(f"    persisted {name} via setx (new shells will see it)")
        else:
            _warn(f"    setx exited {result.returncode}: {(result.stderr or result.stdout or '').strip()}")
        return

    rc_path = _pick_unix_shell_rc()
    if not non_interactive and not sw._ask_yes_no(
        f"  Persist {name} to {rc_path}?", default=True
    ):
        _warn(f"    skipped — set it later: echo 'export {name}={value}' >> {rc_path}")
        return
    try:
        existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    except OSError as e:
        _warn(f"    could not read {rc_path} ({e}); skipping shell rc persistence")
        return
    # Idempotent: if the exact assignment is already present, do nothing; if a
    # stale value for the same var exists, append the new one (last wins in sh).
    if f"export {name}={value}" in existing or f'export {name}="{value}"' in existing:
        _ok(f"    {name}={value} already present in {rc_path}")
        return
    block = f'\n# Added by m3 setup — LLM endpoint failover\nexport {name}="{value}"\n'
    try:
        with rc_path.open("a", encoding="utf-8") as f:
            f.write(block)
        _ok(f"    persisted {name} -> {rc_path}")
    except OSError as e:
        _warn(f"    failed to write {rc_path} ({e})")


def _persist_env_var_mcp(name: str, value: str) -> None:
    """Set <name>=<value> on the 'memory' MCP server env block in Claude/Gemini
    settings, so the spawned MCP server (which doesn't inherit shell env on
    macOS/Windows) sees it. Mirrors _persist_embed_gguf_mcp."""
    for label, settings_path in (
        ("Claude Code", Path.home() / ".claude" / "settings.json"),
        ("Gemini CLI",  Path.home() / ".gemini" / "settings.json"),
    ):
        if not settings_path.is_file():
            continue
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as e:
            _warn(f"    {settings_path} is unreadable ({e}); skipping {label} env wiring")
            continue
        mcp = cfg.get("mcpServers")
        if not isinstance(mcp, dict) or "memory" not in mcp:
            continue
        env = mcp["memory"].setdefault("env", {})
        if env.get(name) == value:
            _ok(f"    {name} already set on {label} memory MCP entry")
            continue
        env[name] = value
        try:
            settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            _ok(f"    set {name} on {label} memory MCP entry ({settings_path})")
        except OSError as e:
            _warn(f"    failed to write {settings_path} ({e})")
