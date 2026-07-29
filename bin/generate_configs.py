import json
import os
import shutil
import sys


def _is_installed_layout(m3_repo_root: str) -> bool:
    """True when m3_repo_root is an INSTALLED package dir (…/site-packages/… or
    …/dist-packages/…), as opposed to a source checkout. A `.venv` beside an
    installed wheel is a stray leftover, never the canonical interpreter."""
    r = m3_repo_root.replace("\\", "/")
    return "/site-packages/" in r or "/dist-packages/" in r


def _resolve_python_cmd(m3_repo_root: str) -> str:
    """Resolve an ABSOLUTE interpreter that actually has m3's dependencies, so
    hooks/MCP/statusline don't depend on whatever "python" is on PATH (the venv
    may not be activated when a hook fires). Always forward-slash: Claude Code
    runs hook commands through a shell (Git Bash on Windows) where backslashes
    are escapes.

    Order:
      1. A repo-local .venv — ONLY for a genuine SOURCE checkout. A `.venv`
         sitting beside an INSTALLED wheel (…/site-packages/m3_memory/.venv) is a
         stray leftover, NOT the canonical interpreter; using it reintroduces the
         exact footgun this resolver avoids (an interpreter that may lack deps or
         later vanish). Skip the .venv guess whenever the root is an installed
         layout.
      2. sys.executable — the interpreter running this code (the pipx interpreter
         under `m3 setup`; the repo .venv when a dev runs it). Definitionally has
         m3's deps. (A bare `python3` on PATH was the original bug: no m3 deps, so
         every generated bridge/hook died ModuleNotFound.)
      3. Bare PATH python only as a last resort.
    """
    if os.name == "nt":
        venv_py = os.path.join(m3_repo_root, ".venv", "Scripts", "python.exe")
    else:
        venv_py = os.path.join(m3_repo_root, ".venv", "bin", "python")
    if os.path.exists(venv_py) and not _is_installed_layout(m3_repo_root):
        return venv_py.replace("\\", "/")
    if sys.executable and os.path.isabs(sys.executable) and os.path.exists(sys.executable):
        return sys.executable.replace("\\", "/")
    return "python" if os.name == "nt" else (
        "python3" if shutil.which("python3") else "python"
    )


def generate_configs():
    """Generates gemini-settings.json and claude-settings.json from templates."""
    # m3_repo_root  = the repo directory (where bin/ lives)
    # m3_state_root = parent of repo = M3_MEMORY_ROOT for bridge env vars
    m3_repo_root  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    m3_state_root = os.path.dirname(m3_repo_root)
    config_dir    = os.path.join(m3_repo_root, "config")

    python_cmd = _resolve_python_cmd(m3_repo_root)

    # Embedder GGUF: discover a bge-m3 model, but do NOT push it into the MCP
    # server env. An env var forces every MCP-server process to open its OWN CUDA
    # context (a per-process hang risk; see §3 "headless knob -> config file, not
    # env var"). Instead seed it into the SHARED config so the single :8082 server
    # owns the one CUDA context and all clients defer to it.
    embed_gguf = os.environ.get("M3_EMBED_GGUF", "")
    if not embed_gguf:
        candidate = os.path.expanduser(
            "~/.lmstudio/models/deepsweet/bge-m3-GGUF-Q4_K_M/bge-m3-GGUF-Q4_K_M.gguf"
        )
        if os.path.exists(candidate):
            embed_gguf = candidate
    try:
        from m3_memory.embedder_admin import seed_shared_config
        _cfg_path, _wrote = seed_shared_config(gguf_path=embed_gguf or None)
        if _wrote:
            print(f"[generate_configs] seeded shared embedder config: {_cfg_path}")
    except Exception as _e:  # noqa: BLE001 — config gen must not hard-fail on this
        print(f"[generate_configs] WARN: could not seed shared embedder config: {_e}")

    # Root bin/ scripts at the AUTHORITATIVE payload — the wheel-packaged bin/
    # when installed (the same resolver the core memory bridge uses), a
    # dev-checkout bin/ otherwise. Generated hooks/bridges/statusline MUST point
    # here, never at a ~/.m3/repo clone that `pip install -U` leaves stale: that
    # split (core bridge on the wheel, aux bridges + capture hooks on the clone)
    # silently ran old hook/bridge code after an upgrade. Rooting every generated
    # command at bin_dir() makes all servers agree on one fresh payload.
    payload_bin = None
    try:
        from m3_memory.installer import bin_dir as _resolve_bin_dir
        _bd = _resolve_bin_dir()
        if _bd is not None:
            payload_bin = str(_bd)
    except Exception:
        payload_bin = None
    if not payload_bin:
        payload_bin = os.path.join(m3_repo_root, "bin")

    def repo(path):
        # Every caller passes "bin/<...>"; root those at the authoritative payload
        # bin so a stale clone can never shadow the installed wheel.
        if path.startswith("bin/"):
            return os.path.join(payload_bin, path[4:]).replace("\\", "/")
        return os.path.join(m3_repo_root, path).replace("\\", "/")

    # Decoupled roots (CLAUDE.md "Split-brain hazard"): the MCP server does NOT
    # inherit the shell env, so its env block AND the capture hooks must carry the
    # DATA roots explicitly. Resolve them the way the rest of m3 does — NEVER from
    # the code location (m3_state_root is the wheel's parent = site-packages under
    # an installed layout, which would point the server at a nonexistent
    # site-packages/engine and silently orphan the real DB on the next restart).
    try:
        from m3_sdk import get_m3_config_root, get_m3_engine_root, get_m3_root
        memory_root = get_m3_root()
        engine_root = get_m3_engine_root()
        config_root = get_m3_config_root()
    except Exception:  # noqa: BLE001 — SDK unavailable: env vars, then ~/.m3 defaults
        _home = os.path.expanduser("~")
        memory_root = os.environ.get("M3_MEMORY_ROOT") or os.path.join(_home, ".m3")
        engine_root = os.environ.get("M3_ENGINE_ROOT") or os.path.join(memory_root, "engine")
        config_root = os.environ.get("M3_CONFIG_ROOT") or os.path.join(memory_root, "config")
    memory_root = str(memory_root).replace("\\", "/")
    engine_root = str(engine_root).replace("\\", "/")
    config_root = str(config_root).replace("\\", "/")

    def mcp_server(script, extra_env=None):
        # All three roots so the server resolves its DBs/config correctly without
        # the shell env (which it does not see).
        env = {
            "M3_MEMORY_ROOT": memory_root,
            "M3_ENGINE_ROOT": engine_root,
            "M3_CONFIG_ROOT": config_root,
        }
        if extra_env:
            env.update(extra_env)
        return {"command": python_cmd, "args": [repo(f"bin/{script}")], "env": env}

    # Inline-pin the engine + config roots on every capture hook. The hook
    # inherits Claude Code's PROCESS env (NOT the server env block above), so
    # without these it resolves a different engine root than the server and the
    # two chatlog halves diverge (CLAUDE.md "Split-brain hazard"). The shell
    # `VAR=val cmd` prefix is honored because Claude Code runs hooks through a
    # shell (Git Bash on Windows). m3 roots are space-free by convention, matching
    # the prior working format.
    _root_pins = f"M3_ENGINE_ROOT={engine_root} M3_CONFIG_ROOT={config_root} "

    # Invoke the .py hook directly with the venv interpreter — no /bin/sh, which
    # doesn't exist on native Windows (it only works today because Claude Code
    # routes hooks through Git Bash). The .py is the cross-platform entry point.
    hook_cmd = f"{_root_pins}{python_cmd} {repo('bin/hooks/chatlog/claude_code_precompact.py')}"
    hook_entry = [{"hooks": [{"type": "command", "command": hook_cmd}]}]

    session_start_cmd = (
        f"{_root_pins}{python_cmd} {repo('bin/hooks/chatlog/session_start_capture_check.py')}"
    )
    session_start_entry = [{"hooks": [{
        "type": "command",
        "command": session_start_cmd,
        "timeout": 15,
        "statusMessage": "Checking m3 chatlog capture...",
    }]}]

    # No M3_EMBED_GGUF here — it lives in the shared config (seeded above) so the
    # memory server defers to the single :8082 embedder instead of loading its own.
    memory_env: dict[str, str] = {}

    mcp_servers = {
        "custom_pc_tool": mcp_server("custom_tool_bridge.py"),
        "grok_intel":     mcp_server("grok_bridge.py"),
        "web_research":   mcp_server("web_research_bridge.py"),
        "memory":         mcp_server("memory_bridge.py", memory_env),
        "debug_agent":    mcp_server("debug_agent_bridge.py"),
    }

    # Respect the capture config: PreCompact + SessionStart are always wired, but
    # Stop is the per-turn toggle — write it only when the claude-code stop_hook is
    # enabled ('both' mode), else capture is PreCompact-only and a Stop hook would
    # fire against the user's explicit choice. Best-effort — default to the
    # recommended 'both' (Stop on) if the config can't be read.
    stop_enabled = True
    try:
        import chatlog_config
        _cc = chatlog_config.resolve_config().host_agents.get("claude-code")
        stop_enabled = bool(_cc.stop_hook) if _cc is not None else True
    except Exception:  # noqa: BLE001 — config unreadable: keep the recommended default
        stop_enabled = True
    claude_hooks = {
        "SessionStart": session_start_entry,
        "PreCompact":   hook_entry,
    }
    if stop_enabled:
        claude_hooks["Stop"] = hook_entry

    # ── claude-settings.json ──────────────────────────────────────────────────
    claude = {
        "model": "opus",
        "hooks": claude_hooks,
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

    # ── .mcp.json (project-level) ────────────────────────────────────────────
    # Deliberately EMPTY of the m3 servers. Claude Code reads BOTH its user
    # settings.json (claude-settings.json above, which carries the COMPLETE defs
    # with env blocks) AND a project-local .mcp.json — so declaring the same
    # servers in both made Claude Code launch each bridge TWICE. A call routed to
    # the redundant (bare, env-less) bridge could hang indefinitely on code
    # without a per-tool timeout — the 2h43m memory_write hang, 2026-07-02.
    # Single registration source = single launch. If you WANT a server scoped to
    # only this project, add it here (with a full env block) AND remove it from
    # settings.json — never both.
    mcp_data = {
        "//": (
            "m3 MCP servers are registered once, in your Claude Code user "
            "settings.json (with env blocks). Do NOT also list them here — "
            "duplicate registration double-launches the bridge and can hang a "
            "tool call. See `m3 doctor` (flags duplicates) / `m3 doctor --fix`."
        ),
        "mcpServers": {},
    }
    _write_json(os.path.join(m3_repo_root, ".mcp.json"), mcp_data)
    print("Generated .mcp.json (empty — servers live in settings.json to avoid double-launch)")

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
