import json
import os
import shutil
import sys

def generate_configs():
    """Generates gemini-settings.json and claude-settings.json from templates."""
    # m3_repo_root  = the repo directory (where bin/ lives)
    # m3_state_root = parent of repo = M3_MEMORY_ROOT for bridge env vars
    m3_repo_root  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    m3_state_root = os.path.dirname(m3_repo_root)
    config_dir    = os.path.join(m3_repo_root, "config")

    # Prefer python3 on non-Windows; fall back to python only if python3 absent.
    if os.name == "nt":
        python_cmd = "python"
    else:
        python_cmd = "python3" if shutil.which("python3") else "python"

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

    hook_cmd = f"/bin/sh {repo('bin/hooks/chatlog/claude_code_precompact.sh')}"
    hook_entry = [{"hooks": [{"type": "command", "command": hook_cmd}]}]

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
            "PreCompact": hook_entry,
            "Stop":       hook_entry,
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


if __name__ == "__main__":
    generate_configs()
