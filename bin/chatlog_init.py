#!/usr/bin/env python3
"""
chatlog_init.py — interactive setup CLI for the chat log subsystem.

Guides the user through:
  - Choosing a mode (separate, integrated, or hybrid)
  - Setting DB path (if separate/hybrid)
  - Enabling host agents and showing wiring instructions
  - Configuring cost tracking and redaction
  - Running migrations and installing schedules
  - Showing Claude Code settings snippet
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys

# Import config module from same directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from chatlog_config import (
    CONFIG_PATH,
    DEFAULT_DB_PATH,
    MAIN_DB_PATH,
    VALID_HOST_AGENTS,
    VALID_MODES,
    ChatlogConfig,
    CostTrackingSpec,
    EmbedSweeperSpec,
    HookSpec,
    RedactionSpec,
    resolve_config,
    save_config,
)

logger = logging.getLogger("chatlog_init")
logging.basicConfig(level=logging.WARNING)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no, return bool. Ctrl-C raises KeyboardInterrupt."""
    default_str = "[Y/n]" if default else "[y/N]"
    while True:
        response = input(f"{question} {default_str}: ").strip().lower()
        if response == "":
            return default
        if response in ("y", "yes"):
            return True
        if response in ("n", "no"):
            return False
        print("Please enter 'y' or 'n'.")


def prompt_choice(question: str, choices: list[str], default: str) -> str:
    """Prompt for one of several choices. Return the chosen value."""
    choices_str = "/".join(choices)
    choices.index(default) if default in choices else 0
    while True:
        response = input(f"{question} ({choices_str}): ").strip().lower()
        if response == "":
            return default
        if response in choices:
            return response
        print(f"Please choose from: {', '.join(choices)}")


def validate_path_writable(path: str) -> bool:
    """Check if parent directory of path exists and is writable."""
    parent = os.path.dirname(path)
    if not parent:
        parent = "."
    return os.path.isdir(parent) and os.access(parent, os.W_OK)


def print_section(title: str) -> None:
    """Print a section header with blank lines."""
    print()
    print(title)
    print("=" * len(title))


def get_hook_path_for_agent(agent: str) -> tuple[str, str]:
    """Return (sh_path, ps1_path) for a host agent."""
    agent_map = {
        "claude-code": ("claude_code_precompact", "claude-code pre-compaction hook"),
        "gemini-cli": ("gemini_cli_onexit", "Gemini CLI session exit hook"),
        "opencode": ("opencode_session_end", "OpenCode session end hook"),
        "aider": ("aider_chat_watcher", "Aider chat watcher hook"),
    }
    base_name, desc = agent_map.get(agent, ("unknown", "unknown hook"))
    sh_path = os.path.join(BASE_DIR, "bin", "hooks", "chatlog", f"{base_name}.sh")
    ps1_path = os.path.join(BASE_DIR, "bin", "hooks", "chatlog", f"{base_name}.ps1")
    return sh_path, ps1_path, desc


def show_hook_wiring_instructions(agent: str, hook_spec: HookSpec) -> None:
    """Print instructions for wiring up a hook for the given agent."""
    sh_path, ps1_path, desc = get_hook_path_for_agent(agent)
    print(f"\n  {agent} ({desc}):")
    print(f"    - macOS/Linux: source '{sh_path}' in your shell startup")
    print(f"    - Windows PowerShell: . '{ps1_path}' in your profile")


def interactive_mode() -> str:
    """Prompt for deployment mode."""
    print_section("Deployment Mode")
    print("Choose how chat logs are stored:")
    print("  separate   - Dedicated DB for chat logs (recommended)")
    print("  integrated - Share the main agent_memory.db")
    print("  hybrid     - Separate DB with syncing to main")
    mode = prompt_choice("Select mode", ["separate", "integrated", "hybrid"], "separate")
    return mode


def interactive_db_path(mode: str) -> str:
    """Prompt for DB path if separate or hybrid."""
    if mode == "integrated":
        return MAIN_DB_PATH

    print_section("Database Path")
    print(f"Default: {DEFAULT_DB_PATH}")
    custom = input("Custom path (leave blank for default): ").strip()

    if not custom:
        return DEFAULT_DB_PATH

    if not validate_path_writable(custom):
        print(f"Warning: {os.path.dirname(custom)} is not writable. Using default.")
        return DEFAULT_DB_PATH

    return custom


def interactive_host_agents() -> dict[str, HookSpec]:
    """Prompt which host agents to enable."""
    print_section("Host Agent Hooks")
    print("Select which host agents to enable for chat logging:")

    host_agents = {}
    for agent in sorted(VALID_HOST_AGENTS):
        enabled = prompt_yes_no(f"Enable {agent}?", default=False)
        if enabled:
            sh_path, ps1_path, desc = get_hook_path_for_agent(agent)
            host_agents[agent] = HookSpec(enabled=True, hook_path=sh_path)
            show_hook_wiring_instructions(agent, host_agents[agent])
        else:
            host_agents[agent] = HookSpec(enabled=False)

    return host_agents


def interactive_cost_tracking() -> bool:
    """Prompt for cost tracking."""
    print_section("Cost Tracking")
    print("Track token usage and costs for chat logs? (zero user-visible cost)")
    return prompt_yes_no("Enable cost tracking?", default=True)


def interactive_redaction() -> RedactionSpec:
    """Prompt for redaction settings."""
    print_section("Redaction (opt-in)")
    print("Redact sensitive data before storing chat logs?")
    enabled = prompt_yes_no("Enable redaction?", default=False)

    if not enabled:
        return RedactionSpec(enabled=False)

    patterns = [
        "api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens"
    ]
    selected_patterns = []
    for pattern in patterns:
        if prompt_yes_no(f"Redact {pattern}?", default=True):
            selected_patterns.append(pattern)

    redact_pii = prompt_yes_no("Redact PII (names, emails, IPs)?", default=False)
    store_original_hash = prompt_yes_no(
        "Store hash of original (for audit)?", default=True
    )

    return RedactionSpec(
        enabled=True,
        patterns=selected_patterns,
        redact_pii=redact_pii,
        store_original_hash=store_original_hash,
    )


def run_migrations() -> bool:
    """Ask and run migrations if user agrees."""
    print_section("Database Migrations")
    if not prompt_yes_no("Run migrations now?", default=True):
        return False

    migrate_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")
    try:
        result = subprocess.run(
            [sys.executable, migrate_script, "up", "--target", "chatlog", "-y"],
            shell=False,
            check=False,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Migration failed: {e}")
        return False


def install_schedules() -> bool:
    """Ask and install embed sweeper schedule if user agrees."""
    print_section("Embed Sweeper Schedule")
    if not prompt_yes_no(
        "Install the embed sweeper schedule (~30min cadence)?", default=True
    ):
        return False

    install_script = os.path.join(BASE_DIR, "bin", "install_schedules.py")
    if not os.path.exists(install_script):
        print("Warning: install_schedules.py not found yet; skipping.")
        return False

    try:
        result = subprocess.run(
            [sys.executable, install_script, "--add", "chatlog-embed-sweep"],
            shell=False,
            check=False,
        )
        return result.returncode == 0
    except Exception as e:
        print(f"Schedule install failed: {e}")
        return False


def show_status_line_snippet(config_path: str) -> None:
    """Show the snippet for Claude Code settings.json."""
    print_section("Claude Code Settings (optional)")
    print("To show chat log status in Claude Code, add this to ~/.claude/settings.json:")
    print()

    status_script = os.path.join(BASE_DIR, "bin", "chatlog_status_line.py")
    snippet = {
        "statusLine": {
            "type": "command",
            "command": f"python {status_script}"
        }
    }
    print(json.dumps(snippet, indent=2))
    print()
    print("Do NOT auto-edit settings.json. Copy-paste the above manually.")


def print_summary(config: ChatlogConfig) -> None:
    """Print final configuration summary."""
    print_section("Configuration Summary")
    print(f"Mode:               {config.mode}")
    print(f"DB Path:            {config.db_path}")

    enabled_agents = [a for a, spec in config.host_agents.items() if spec.enabled]
    if enabled_agents:
        print(f"Enabled Agents:     {', '.join(enabled_agents)}")
    else:
        print("Enabled Agents:     (none)")

    print(f"Cost Tracking:      {'ON' if config.cost_tracking.enabled else 'OFF'}")
    print(f"Redaction:          {'ON' if config.redaction.enabled else 'OFF'}")
    if config.redaction.enabled:
        print(f"  Patterns:         {', '.join(config.redaction.patterns)}")
        print(f"  PII:              {'ON' if config.redaction.redact_pii else 'OFF'}")

    print(f"Config File:        {CONFIG_PATH}")


def show_existing_config() -> None:
    """Show existing config and exit."""
    cfg = resolve_config()
    print_section("Existing Configuration")
    print(f"Mode:             {cfg.mode}")
    print(f"DB Path:          {cfg.db_path}")
    enabled = [a for a, s in cfg.host_agents.items() if s.enabled]
    print(f"Enabled Agents:   {', '.join(enabled) if enabled else '(none)'}")
    print(f"Cost Tracking:    {'ON' if cfg.cost_tracking.enabled else 'OFF'}")
    print(f"Redaction:        {'ON' if cfg.redaction.enabled else 'OFF'}")
    print()
    print("Use --reconfigure to change settings.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Set up the chat log subsystem"
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Reconfigure even if config exists",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Use defaults, skip prompts and post-setup steps",
    )
    parser.add_argument(
        "--mode",
        choices=list(VALID_MODES),
        default=None,
        help="Deployment mode (separate, integrated, hybrid)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Database path (for separate/hybrid mode)",
    )

    args = parser.parse_args()

    try:
        # Check if config exists
        if os.path.exists(CONFIG_PATH) and not args.reconfigure and not args.non_interactive:
            show_existing_config()
            return 0

        # Non-interactive mode
        if args.non_interactive:
            mode = args.mode or "separate"
            db_path = args.db_path or DEFAULT_DB_PATH

            config = ChatlogConfig(
                mode=mode,  # type: ignore[assignment]
                db_path=db_path,
                host_agents={a: HookSpec() for a in VALID_HOST_AGENTS},
                cost_tracking=CostTrackingSpec(enabled=True),
                redaction=RedactionSpec(enabled=False),
                embed_sweeper=EmbedSweeperSpec(),
            )
            save_config(config)
            print(f"Configuration saved to {CONFIG_PATH}")
            return 0

        # Interactive mode
        mode = args.mode or interactive_mode()
        db_path = args.db_path or interactive_db_path(mode)
        host_agents = interactive_host_agents()
        cost_tracking_enabled = interactive_cost_tracking()
        redaction = interactive_redaction()

        # Build config
        config = ChatlogConfig(
            mode=mode,  # type: ignore[assignment]
            db_path=db_path,
            host_agents=host_agents,
            cost_tracking=CostTrackingSpec(enabled=cost_tracking_enabled),
            redaction=redaction,
            embed_sweeper=EmbedSweeperSpec(),
        )

        save_config(config)
        print_summary(config)

        # Post-setup steps
        run_migrations()
        install_schedules()
        show_status_line_snippet(CONFIG_PATH)

        print()
        print("Setup complete!")
        return 0

    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
