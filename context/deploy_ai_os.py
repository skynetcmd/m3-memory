import os
import json

# --- CONFIGURATION ---
PROJECT_NAME = os.path.basename(os.getcwd())
MEMORY_FILE = ".ai_context_memory.json"
EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'dist', 'build'}

def setup_mcp_configs():
    """Configures Claude Code and Gemini CLI to see your Local MLX and Web Tools."""
    print("🔧 Configuring MCP Bridges...")
    
    # Claude Code Config (Standard Path)
    claude_config_path = os.path.expanduser("~/.claude/settings.json")
    claude_config = {
        "mcpServers": {
            "local_mlx": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-openai", "--base-url", "http://localhost:1234/v1"]
            },
            "web_intel": {
                "command": "npx",
                "args": ["-y", "@agentify/mcp-server"]
            }
        }
    }
    
    os.makedirs(os.path.dirname(claude_config_path), exist_ok=True)
    with open(claude_config_path, 'w') as f:
        json.dump(claude_config, f, indent=2)
    print(f"✅ Claude Code configured at {claude_config_path}")

def create_project_manifest():
    """Uses Gemini's context window to index the project for $0.01."""
    print("🧠 Generating Project Manifest via Gemini...")
    
    file_structure = []
    for root, dirs, files in os.walk("."):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for file in files:
            file_structure.append(os.path.join(root, file))
    
    # This manifest is what we feed to Gemini CLI to 'prime' its 2M context
    manifest = {
        "project": PROJECT_NAME,
        "files": file_structure,
        "instructions": "Use this map to find files. Do not read them all at once; only read what is needed for the specific task."
    }
    
    with open(MEMORY_FILE, 'w') as f:
        json.dump(manifest, f, indent=2)
    print(f"✅ Created {MEMORY_FILE}. Feed this to Gemini CLI for instant project awareness.")

def generate_bash_shortcuts():
    """Creates the 'Smart Handoff' commands for your terminal."""
    print("📜 Creating terminal shortcuts...")
    
    shortcuts = """
# AI Agentic Shortcuts
alias ai-research="gemini 'Using Perplexity, research the latest version of my project dependencies and save to RESEARCH.md'"
alias ai-audit="pbpaste | lms prompt --model deepseek-r1-70b 'Review this code for bugs and security flaws.'"
alias ai-do="claude --model claude-3-7-sonnet 'Read RESEARCH.md and the project manifest, then implement the changes.'"
    """
    
    with open("ai_shortcuts.sh", "w") as f:
        f.write(shortcuts)
    print("✅ Created ai_shortcuts.sh. Run 'source ai_shortcuts.sh' to activate.")

if __name__ == "__main__":
    setup_mcp_configs()
    create_project_manifest()
    generate_bash_shortcuts()
    print("\n🚀 SYSTEM READY.")
    print("1. Start LM Studio (Port 1234) with DeepSeek-R1 70B.")
    print("2. Source your new shortcuts: 'source ai_shortcuts.sh'")
    print("3. Use 'ai-research' to start your first loop!")
