import json
import os
import shutil


def generate_configs():
    """Generates gemini-settings.json and claude-settings.json from templates."""
    # m3_repo_root  = the repo directory (where bin/ lives)
    # m3_state_root = parent of repo = M3_MEMORY_ROOT for bridge env vars
    m3_repo_root  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    m3_state_root = os.path.dirname(m3_repo_root)
    config_dir    = os.path.join(m3_repo_root, "config")

    # Resolve the interpreter to the repo's own venv so hooks/MCP don't depend on
    # whatever "python" happens to be on PATH (the venv may not be activated when a
    # hook fires). venv layout differs by OS: Windows = .venv/Scripts/python.exe,
    # macOS/Linux = .venv/bin/python. Always forward-slash: Claude Code runs hook
    # commands through a shell (Git Bash on Windows) where backslashes are escapes.
    if os.name == "nt":
        venv_py = os.path.join(m3_repo_root, ".venv", "Scripts", "python.exe")
    else:
        venv_py = os.path.join(m3_repo_root, ".venv", "bin", "python")
    if os.path.exists(venv_py):
        python_cmd = venv_py.replace("\\", "/")
    else:
        # No venv found — fall back to PATH (python3 preferred off-Windows).
        python_cmd = "python" if os.name == "nt" else (
            "python3" if shutil.which("python3") else "python"
        )

    # M3_EMBED_GGUF: use env override, else auto-detect the standard LMStudio path.
    embed_gguf = os.environ.get("M3_EMBED_GGUF", "")
    if not embed_gguf:
        candidate = os.path.expanduser(
            "~/.lmstudio/models/deepsweet/bge-m3-GGUF-Q4_K_M/bge-m3-GGUF-Q4_K_M.gguf"
        )
        if os.path.exists(candidate):
            embed_gguf = candidate

    def repo(path):
        return os.path.join(m3_repo_root, path).replace("\\", "/")

    def mcp_server(script, extra_env=None):
        env = {"M3_MEMORY_ROOT": m3_state_root.replace("\\", "/")}
        if extra_env:
            env.update(extra_env)
        return {"command": python_cmd, "args": [repo(f"bin/{script}")], "env": env}

    # Invoke the .py hook directly with the venv interpreter — no /bin/sh, which
    # doesn't exist on native Windows (it only works today because Claude Code
    # routes hooks through Git Bash). The .py is the cross-platform entry point.
    hook_cmd = f"{python_cmd} {repo('bin/hooks/chatlog/claude_code_precompact.py')}"
    hook_entry = [{"hooks": [{"type": "command", "command": hook_cmd}]}]

    session_start_cmd = (
        f"{python_cmd} {repo('bin/hooks/chatlog/session_start_capture_check.py')}"
    )
    session_start_entry = [{"hooks": [{
        "type": "command",
        "command": session_start_cmd,
        "timeout": 15,
        "statusMessage": "Checking m3 chatlog capture...",
    }]}]

    memory_env = {}
    if embed_gguf:
        memory_env["M3_EMBED_GGUF"] = embed_gguf.replace("\\", "/")

    mcp_servers = {
        "custom_pc_tool": mcp_server("custom_tool_bridge.py"),
        "grok_intel":     mcp_server("grok_bridge.py"),
        "web_research":   mcp_server("web_research_bridge.py"),
        "memory":         mcp_server("memory_bridge.py", memory_env),
        "debug_agent":    mcp_server("debug_agent_bridge.py"),
    }

    # ── claude-settings.json ──────────────────────────────────────────────────
    claude = {
        "model": "opus",
        "hooks": {
            "SessionStart": session_start_entry,
            "PreCompact":   hook_entry,
            "Stop":         hook_entry,
        },
        "statusLine": {
            "type":    "command",
            "command": f"{python_cmd} {repo('bin/statusline-command.sh')}",
        },
        "enabledPlugins": {"m3@skynetcmd": True},
        "extraKnownMarketplaces": {
            "skynetcmd": {"source": {"source": "github", "repo": "skynetcmd/m3-memory"}}
        },
        "skipDangerousModePermissionPrompt": True,
        "mcpServers": mcp_servers,
    }
    _write_json(os.path.join(config_dir, "claude-settings.json"), claude)
    print(f"Generated claude-settings.json ({python_cmd}, M3_MEMORY_ROOT={m3_state_root})")
    generate_configs._last_claude = claude  # reused by install_claude_settings()

    # ── gemini-settings.json ──────────────────────────────────────────────────
    gemini_path = os.path.join(config_dir, "gemini-settings.json")
    if os.path.exists(gemini_path):
        with open(gemini_path) as f:
            try:
                gemini = json.load(f)
            except json.JSONDecodeError:
                gemini = {}
    else:
        gemini = {}

    gemini["mcpServers"] = mcp_servers
    if "general" not in gemini:
        gemini["general"] = {
            "sessionRetention": {"enabled": True, "maxAge": "30d", "warningAcknowledged": True}
        }
    if "security" not in gemini:
        gemini["security"] = {"auth": {"selectedType": "gemini-api-key"}}

    _write_json(gemini_path, gemini)
    print(f"Generated gemini-settings.json ({python_cmd})")

    # ── .mcp.json (Claude Code project-level MCP registration) ───────────────
    mcp_data = {"mcpServers": {
        name: {"command": python_cmd, "args": [repo(f"bin/{script}")]}
        for name, script in [
            ("custom_pc_tool", "custom_tool_bridge.py"),
            ("memory",         "memory_bridge.py"),
            ("grok_intel",     "grok_bridge.py"),
            ("web_research",   "web_research_bridge.py"),
            ("debug_agent",    "debug_agent_bridge.py"),
        ]
    }}
    _write_json(os.path.join(m3_repo_root, ".mcp.json"), mcp_data)
    print(f"Generated .mcp.json ({python_cmd})")

    # ── .aider.conf.yml ───────────────────────────────────────────────────────
    aider_path = os.path.join(m3_repo_root, ".aider.conf.yml")
    if os.path.exists(aider_path):
        with open(aider_path) as f:
            content = f.read()
        new_content = content.replace("[M3_MEMORY_ROOT]", m3_repo_root.replace("\\", "/"))
        if new_content != content:
            with open(aider_path, "w") as f:
                f.write(new_content)
            print("Updated [M3_MEMORY_ROOT] in .aider.conf.yml")


def _write_json(path, data):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except Exception as e:
        print(f"Failed to write {os.path.basename(path)}: {e}")
        if os.path.exists(tmp):
            os.remove(tmp)


def _m3_repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_m3_command(cmd, repo_root_fwd):
    """True if a hook/statusLine command string belongs to m3 (so an upgrade can
    replace it in place instead of appending a duplicate). Matches on the repo
    root path or the known m3 script markers — never on a hardcoded user path."""
    if not isinstance(cmd, str):
        return False
    markers = (repo_root_fwd, "bin/hooks/chatlog/", "bin/statusline-command",
               "session_start_capture_check", "claude_code_precompact")
    return any(m and m in cmd for m in markers)


def _strip_m3_hook_entries(hook_list, repo_root_fwd):
    """Return hook_list with any m3-managed entries removed. A Claude hooks-list
    entry looks like {"hooks": [{"type": "command", "command": "..."}]}. We drop an
    entry if ANY of its inner commands is an m3 command — preserves user hooks."""
    kept = []
    for entry in hook_list or []:
        inner = entry.get("hooks", []) if isinstance(entry, dict) else []
        if any(_is_m3_command(h.get("command", ""), repo_root_fwd)
               for h in inner if isinstance(h, dict)):
            continue  # drop stale/previous m3 entry
        kept.append(entry)
    return kept


def install_claude_settings(settings_path=None, assume_yes=False, dry_run=False,
                            keep_status_line=False):
    """Idempotently merge m3's hooks + statusLine + mcpServers into the user's live
    Claude Code settings.json. Safe to re-run (upgrades): m3-owned entries are
    replaced in place — never duplicated. User-owned keys/hooks are preserved.

    statusLine consent: we never silently replace a status line that differs from
    our own. If the live one differs, the user is asked (default YES — adopt m3's
    statusline-command.sh); pass keep_status_line=True to decline non-interactively.
    When we DO replace it, the prior statusLine JSON is saved verbatim to a sidecar
    m3_prior_statusline_{YYYY.MM.DD}-{HH.MM.SS}.md beside settings.json before the
    overwrite, so the previous setup is preserved and restorable. settings.json is
    strict JSON (no // comments), so the prior config is stashed in a file, not
    inline-commented.

    Returns a dict: {"changed": bool, "path": str, "diff": str}. Generic across
    OSes and users — all paths derive from this file's location.
    """
    import difflib
    from datetime import datetime

    repo_root = _m3_repo_root()
    repo_root_fwd = repo_root.replace("\\", "/")

    # Build the canonical m3 settings via the generator (writes the template too).
    generate_configs()
    m3 = getattr(generate_configs, "_last_claude", None)
    if not m3:
        raise RuntimeError("generate_configs did not produce claude settings")

    if settings_path is None:
        settings_path = os.path.join(
            os.path.expanduser("~"), ".claude", "settings.json"
        )

    # Load existing live settings (preserve everything we don't own).
    if os.path.exists(settings_path):
        with open(settings_path, encoding="utf-8") as f:
            try:
                live = json.load(f)
            except json.JSONDecodeError:
                live = {}
    else:
        live = {}
    before = json.dumps(live, indent=2, sort_keys=True)

    # 1. hooks — replace m3-owned list entries in place, keep user hooks.
    live_hooks = live.get("hooks", {}) if isinstance(live.get("hooks"), dict) else {}
    for event, m3_entries in m3.get("hooks", {}).items():
        cleaned = _strip_m3_hook_entries(live_hooks.get(event, []), repo_root_fwd)
        live_hooks[event] = cleaned + m3_entries
    if live_hooks:
        live["hooks"] = live_hooks

    # 2. statusLine — never silently replace a status line that differs from ours.
    #    Adopt m3's default (statusline-command.sh) when: nothing is set, OR the
    #    current one is already m3's exact default (idempotent path upgrade), OR the
    #    user consents. The prior statusLine is preserved to a sidecar file first.
    cur_status = live.get("statusLine")
    cur_cmd = cur_status.get("command", "") if isinstance(cur_status, dict) else ""
    m3_status = m3["statusLine"]
    m3_cmd = m3_status.get("command", "")

    if not cur_status:
        live["statusLine"] = m3_status            # none set — just adopt ours
    elif cur_cmd == m3_cmd:
        pass                                       # already exactly ours — no-op
    else:
        # Differs from ours. Decide whether to adopt — default YES, but ask unless
        # told otherwise; never replace when the user opted to keep theirs.
        if keep_status_line:
            adopt = False
        elif assume_yes or dry_run:
            adopt = True                           # default yes for headless/dry-run
        else:
            try:
                resp = input(
                    "\nReplace your current status line with m3's "
                    "(statusline-command.sh)? [Y/n] "
                ).strip().lower()
            except EOFError:
                resp = ""
            adopt = resp in ("", "y", "yes")       # default yes on empty
        if adopt:
            # Preserve the prior statusLine to a timestamped sidecar before swap.
            if not dry_run:
                ts = datetime.now().strftime("%Y.%m.%d-%H.%M.%S")
                sidecar = os.path.join(
                    os.path.dirname(os.path.abspath(settings_path)),
                    f"m3_prior_statusline_{ts}.md",
                )
                body = (
                    f"# Prior Claude statusLine (replaced by m3 install {ts})\n\n"
                    "Your previous `statusLine` was replaced by m3's "
                    "`statusline-command.sh`. To restore it, copy the JSON below "
                    "back into the `statusLine` key of your settings.json.\n\n"
                    "```json\n"
                    + json.dumps(cur_status, indent=2) + "\n```\n"
                )
                with open(sidecar, "w", encoding="utf-8") as f:
                    f.write(body)
                print(f"Saved prior status line to {sidecar}")
            live["statusLine"] = m3_status

    # 3. mcpServers — merge by key: m3 keys overwrite, foreign servers preserved.
    live_mcp = live.get("mcpServers", {}) if isinstance(live.get("mcpServers"), dict) else {}
    live_mcp.update(m3.get("mcpServers", {}))
    live["mcpServers"] = live_mcp

    after = json.dumps(live, indent=2, sort_keys=True)
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile="settings.json (current)", tofile="settings.json (after install)",
    ))
    changed = before != after

    if dry_run or not changed:
        return {"changed": changed, "path": settings_path, "diff": diff}

    print(f"\nThe following changes will be merged into {settings_path}:\n")
    print(diff or "(no textual diff)")
    if not assume_yes:
        try:
            resp = input("\nApply these changes? [y/N] ").strip().lower()
        except EOFError:
            resp = "n"
        if resp not in ("y", "yes"):
            print("Skipped — no changes written.")
            return {"changed": False, "path": settings_path, "diff": diff}

    # Back up, then write atomically.
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    if os.path.exists(settings_path):
        bak = settings_path + ".bak"
        with open(settings_path, encoding="utf-8") as f:
            backup = f.read()
        with open(bak, "w", encoding="utf-8") as f:
            f.write(backup)
        print(f"Backed up existing settings to {bak}")
    _write_json(settings_path, live)
    print(f"Installed m3 hooks + statusLine + mcpServers into {settings_path}")
    return {"changed": True, "path": settings_path, "diff": diff}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Generate m3 configs / install Claude hooks")
    ap.add_argument("--install-claude", action="store_true",
                    help="Merge hooks+statusLine+mcpServers into ~/.claude/settings.json")
    ap.add_argument("--settings-path", default=None,
                    help="Override target settings.json path")
    ap.add_argument("--yes", action="store_true", help="Apply without prompting")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show the diff but write nothing")
    ap.add_argument("--keep-status-line", action="store_true",
                    help="Don't replace an existing custom status line (default is "
                         "to adopt m3's statusline-command.sh, preserving the prior "
                         "one to a timestamped sidecar file)")
    a = ap.parse_args()

    if a.install_claude:
        install_claude_settings(a.settings_path, assume_yes=a.yes, dry_run=a.dry_run,
                                keep_status_line=a.keep_status_line)
    else:
        generate_configs()
