"""End-of-run summary rendering for the setup wizard.

Extracted verbatim from setup_wizard.py. Pure rendering — not monkeypatched
by any test (confirmed via grep) and does not call any of the 7 patched
functions, so it's safe to live in a submodule.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from .ui import _ok, _warn


def _summary(plan, governor_result: Optional[dict] = None) -> None:
    """End-of-run summary so the user knows exactly what to do next."""
    print()
    _ok("Setup complete.")
    print()
    restart_lines = []
    if plan.targets.claude:
        restart_lines.append("  • Claude Code              — restart the CLI (or run `/plugin reload`)")
    if plan.targets.gemini:
        restart_lines.append("  • Gemini CLI               — restart the CLI")
    if plan.targets.antigravity:
        restart_lines.append("  • Antigravity CLI/Desktop  — restart the CLI/Desktop")
    if plan.targets.opencode:
        restart_lines.append("  • OpenCode                 — restart the CLI")
    if plan.targets.openclaw:
        restart_lines.append("  • OpenClaw                 — start `m3 proxy start`, then set base URL")
    if restart_lines:
        print("Next step — restart your agent so it picks up the new MCP server:")
        for line in restart_lines:
            print(line)
    else:
        print("No agents were wired. Run `m3 setup` again or wire one by hand.")
    print()
    if plan.decouple_roots or plan.fips_mode:
        import os as _os
        import sys as _sys
        _is_win = _sys.platform == "win32"
        # Emit COPY-PASTE-USABLE commands for the actual shell: `setx` (persists
        # to the user environment) on Windows, `export` on POSIX. And show NATIVE
        # paths — normpath so Windows gets all-backslash (not the mixed
        # "<HOME>\.m3/config" mixed-separator form that expanduser leaves).
        def _p(path: str) -> str:
            return _os.path.normpath(path) if path else path

        print("Security & Path Configuration:")
        if _is_win:
            print("  To persist these across sessions (visible to your agents), run in a")
            print("  Command Prompt or PowerShell (setx writes them to your user environment;")
            print("  open a NEW terminal afterwards for them to take effect):")
            if plan.decouple_roots:
                print(f'    setx M3_CONFIG_ROOT "{_p(plan.config_root)}"')
                print(f'    setx M3_ENGINE_ROOT "{_p(plan.engine_root)}"')
            if plan.fips_mode:
                print('    setx M3_FIPS_MODE 1')
                if plan.fips_strict:
                    print('    setx M3_FIPS_STRICT 1   & REM requires the CMVP-validated wolfCrypt')
                print("    REM FIPS needs wolfSSL present (build: m3 fips install-wolfssl).")
                print("    REM Verify + get the SHA-256 to pin: m3 doctor  (crypto section)")
        else:
            print("  To persist these across sessions (visible to your agents), add to your")
            print("  shell profile (~/.bashrc, ~/.zshrc, or ~/.profile), then open a new shell:")
            if plan.decouple_roots:
                print(f'    export M3_CONFIG_ROOT="{_p(plan.config_root)}"')
                print(f'    export M3_ENGINE_ROOT="{_p(plan.engine_root)}"')
            if plan.fips_mode:
                print('    export M3_FIPS_MODE=1')
                if plan.fips_strict:
                    print('    export M3_FIPS_STRICT=1   # requires the CMVP-validated wolfCrypt')
                print("    # FIPS needs wolfSSL present (build: m3 fips install-wolfssl).")
                print("    # Verify + get the SHA-256 to pin: m3 doctor  (crypto section)")
        print()

    # ── governor migration results ─────────────────────────────────────────
    if governor_result:
        removed = governor_result.get("removed", [])
        failed = governor_result.get("failed", [])
        cmds = governor_result.get("privileged_cmds", [])
        not_migratable = governor_result.get("not_migratable", [])

        if removed or failed or not_migratable:
            print("Background Workload Governor:")
        if removed:
            print(f"  Migrated to the governor (removed {len(removed)} legacy scheduled task(s)):")
            for name in removed:
                print(f"    • {name}")
        if not_migratable:
            print("  Left on their schedule (the governor cannot take these over):")
            for line in not_migratable:
                print(line)
        if failed:
            print()
            _warn(f"Could not remove {len(failed)} scheduled task(s) — insufficient privilege.")
            print("  Run these PRIVILEGED, OS-specific commands to remove them cleanly,")
            print("  then the governor (already active in-process) fully owns that work:")
            print()
            if _os_name_for_summary() == "Windows":
                print("  → Open an ELEVATED (Administrator) PowerShell or Command Prompt and run:")
            else:
                print("  → Run in your shell (prefix with sudo only if it's a system/root crontab):")
            for c in cmds:
                print(f"      {c}")
        if removed or failed or not_migratable:
            print()

    # ── embedder tier (Project Oxidation status) ────────────────────────────
    try:
        from m3_memory.rust_core_install import active_embedder_tier
        tier = active_embedder_tier()
        print("Embedder (Project Oxidation):")
        if tier.get("native"):
            _ok(f"  {tier['summary']}")
        else:
            _warn(f"  {tier['summary']}")
        print()
    except Exception:  # noqa: BLE001 — summary is best-effort
        pass

    # ── clear "you're done" closer ──────────────────────────────────────────
    print("─" * 60)
    if plan.targets.any():
        _ok("M3 is installed and live. Restart your agent (above) and your")
        print("    memory + chatlog start working immediately — nothing else to do.")
    else:
        _ok("M3 is installed. No agents were wired — run `m3 setup` again and")
        print("    pick at least one agent, or add the MCP server by hand.")
    print()
    print("  Try it:   m3 status      # one-line health check")
    print("            m3 doctor      # full diagnostics")
    print("            m3 --help      # every command")
    print("─" * 60)
    print()


def _os_name_for_summary() -> str:
    """Thin OS branch for summary phrasing (avoids importing governor_migration
    just for the OS check)."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"
