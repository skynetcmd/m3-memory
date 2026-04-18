import json
import os


def generate_configs():
    """Updates gemini-settings.json and claude-settings.json with current project paths."""
    m3_memory_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_dir = os.path.join(m3_memory_root, "config")

    # Detect the working python command for this platform.
    # Prefer "python3" only if "python" is unavailable (rare on modern installs).
    python_cmd = "python"
    if os.name != "nt":
        import shutil
        if not shutil.which("python") and shutil.which("python3"):
            python_cmd = "python3"
    files_to_update = ["gemini-settings.json", "claude-settings.json"]

    for filename in files_to_update:
        file_path = os.path.join(config_dir, filename)
        if not os.path.exists(file_path):
            print(f"Skipping {filename}: File not found.")
            continue

        with open(file_path, 'r') as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                print(f"Error decoding {filename}. Skipping.")
                continue

        # Update MCP server paths and python command
        if "mcpServers" in data:
            for server_name, server_config in data["mcpServers"].items():
                if "command" in server_config and server_config["command"] in ("python", "python3"):
                    server_config["command"] = python_cmd
                if "args" in server_config and len(server_config["args"]) > 0:
                    script_path = server_config["args"][0]
                    script_name = os.path.basename(script_path)
                    new_path = os.path.join(m3_memory_root, "bin", script_name).replace("\\", "/")
                    server_config["args"][0] = new_path
                    print(f"Updated {server_name} in {filename} to {python_cmd} {new_path}")

        # Update statusLine command in claude-settings
        if filename == "claude-settings.json" and "statusLine" in data:
            cmd = data["statusLine"].get("command", "")
            # Pattern: python3 /path/to/bin/script.sh or script.py
            parts = cmd.split()
            if parts:
                script_name = os.path.basename(parts[-1])
                new_cmd = f"{python_cmd} {os.path.join(m3_memory_root, 'bin', script_name).replace(chr(92), '/')}"
                data["statusLine"]["command"] = new_cmd
                print(f"Updated statusLine in {filename}")

        temp_file = file_path + ".tmp"
        try:
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            os.replace(temp_file, file_path)
            print(f"Successfully saved {filename} (atomically)")
        except Exception as e:
            print(f"Failed to save {filename}: {e}")
            if os.path.exists(temp_file):
                os.remove(temp_file)

    # Generate .mcp.json for Claude Code project-level MCP registration
    mcp_json_path = os.path.join(m3_memory_root, ".mcp.json")
    bridge_scripts = [
        ("custom_pc_tool", "custom_tool_bridge.py"),
        ("memory", "memory_bridge.py"),
        ("grok_intel", "grok_bridge.py"),
        ("web_research", "web_research_bridge.py"),
        ("debug_agent", "debug_agent_bridge.py"),
    ]
    mcp_data = {"mcpServers": {}}
    for name, script in bridge_scripts:
        mcp_data["mcpServers"][name] = {
            "command": python_cmd,
            "args": [os.path.join(m3_memory_root, "bin", script).replace("\\", "/")]
        }
    temp_mcp = mcp_json_path + ".tmp"
    try:
        with open(temp_mcp, 'w') as f:
            json.dump(mcp_data, f, indent=2)
        os.replace(temp_mcp, mcp_json_path)
        print(f"Generated .mcp.json with {python_cmd}")
    except Exception as e:
        print(f"Failed to generate .mcp.json: {e}")
        if os.path.exists(temp_mcp):
            os.remove(temp_mcp)

    # Update .aider.conf.yml (Code Quality Bug #8)
    aider_path = os.path.join(m3_memory_root, ".aider.conf.yml")
    if os.path.exists(aider_path):
        with open(aider_path, 'r') as f:
            content = f.read()

        # Replace placeholder with absolute path
        new_content = content.replace("[M3_MEMORY_ROOT]", m3_memory_root.replace("\\", "/"))

        if new_content != content:
            with open(aider_path, 'w') as f:
                f.write(new_content)
            print("Updated [M3_MEMORY_ROOT] in .aider.conf.yml")

if __name__ == "__main__":
    generate_configs()
