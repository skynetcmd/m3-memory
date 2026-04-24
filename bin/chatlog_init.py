#!/usr/bin/env python3
"""
chatlog_init.py — interactive setup CLI for the chat log subsystem.

Guides the user through:
  - Choosing a chatlog DB path (defaults to a dedicated file; set it equal
    to the main DB to keep everything in one place)
  - Enabling host agents and showing wiring instructions
  - Configuring cost tracking and redaction
  - Running migrations and installing schedules
  - Showing Claude Code settings snippet

The prior integrated/separate/hybrid mode selection has been removed: the
same behaviors are now selected by setting the chatlog DB path equal to (or
different from) the main DB. Promote semantics switch automatically based on
path equality.
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
    # Forward-slash paths on Windows too — Claude Code's Stop hook chain
    # eats backslash escapes, and PowerShell accepts forward slashes
    # everywhere. Consistent with the settings.json snippet emitted by
    # show_claude_code_settings_snippet().
    sh_path = os.path.join(BASE_DIR, "bin", "hooks", "chatlog", f"{base_name}.sh").replace("\\", "/")
    ps1_path = os.path.join(BASE_DIR, "bin", "hooks", "chatlog", f"{base_name}.ps1").replace("\\", "/")
    return sh_path, ps1_path, desc


def show_hook_wiring_instructions(agent: str, hook_spec: HookSpec) -> None:
    """Print instructions for wiring up a hook for the given agent."""
    sh_path, ps1_path, desc = get_hook_path_for_agent(agent)
    is_windows = sys.platform == "win32"
    print(f"\n  {agent} ({desc}):")
    if agent == "claude-code":
        print("    - Follow the 'Claude Code Settings' section below to add hooks to ~/.claude/settings.json")
    elif agent == "gemini-cli":
        print("    - Register the hook in ~/.gemini/settings.json (see docs/CHATLOG.md for snippet)")
        if is_windows:
            print(f"    - Command: powershell -NoProfile -ExecutionPolicy Bypass -File {ps1_path}")
        else:
            print(f"    - Command: /bin/sh {sh_path}")
    else:
        print(f"    - macOS/Linux: source '{sh_path}' in your shell startup")
        print(f"    - Windows PowerShell: . '{ps1_path}' in your profile")


def interactive_db_path() -> str:
    """Prompt for chat log DB path. Unified with main DB if the user types its path."""
    print_section("Database Path")
    print("Choose where chat logs are stored:")
    print(f"  Default (dedicated file):  {DEFAULT_DB_PATH}")
    print(f"  Unified (main memory DB):  {MAIN_DB_PATH}")
    print("  (Type a different absolute path to use your own.)")
    custom = input("DB path (leave blank for dedicated default): ").strip()

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
        if not enabled:
            host_agents[agent] = HookSpec(enabled=False)
            continue

        sh_path, ps1_path, desc = get_hook_path_for_agent(agent)
        spec = HookSpec(enabled=True, hook_path=sh_path)

        # Claude Code has two hook points: PreCompact (fires when Claude
        # summarizes its context, sporadic) and Stop (fires on every
        # assistant turn). PreCompact alone is cheap but can leave gaps
        # of hours or days between captures; Stop captures everything at
        # the cost of one Python subprocess per turn (~50-150ms). Ask
        # the user rather than defaulting because the tradeoff depends
        # on workload.
        if agent == "claude-code":
            print()
            print("  Claude Code supports two hook points:")
            print("    PreCompact only (default) - fires when Claude compacts")
            print("                                context. Light touch, but some")
            print("                                sessions never compact and so")
            print("                                never capture.")
            print("    + Stop hook               - also fires on every assistant")
            print("                                turn. Real-time capture; adds")
            print("                                ~50-150ms per turn.")
            spec.stop_hook = prompt_yes_no(
                "  Enable Stop hook for per-turn capture?", default=False
            )

        host_agents[agent] = spec
        show_hook_wiring_instructions(agent, spec)

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


def show_claude_code_settings_snippet(config: ChatlogConfig) -> None:
    """Print the hooks + statusLine snippet for ~/.claude/settings.json.

    Includes the PreCompact hook unconditionally and the Stop hook only when
    config.host_agents['claude-code'].stop_hook is True. The two hooks share
    the same PS1 entry point — it derives the variant from hook_event_name.
    """
    print_section("Claude Code Settings (optional)")
    print("Add the following to ~/.claude/settings.json under `hooks` and `statusLine`:")
    print()

    # Use forward slashes on Windows. PowerShell accepts them, and unlike
    # backslash-escaped paths they survive whatever shell interpretation
    # layer Claude Code uses when invoking Stop / PreCompact hooks.
    # Observed: C:\\Users\\bhaba\\... paths showed up stripped in the
    # Claude Code hook error as "CUsersbhaba..." — the shell chain ate
    # the escape sequences. Forward slashes sidestep the whole class.
    ps1 = os.path.join(BASE_DIR, "bin", "hooks", "chatlog",
                       "claude_code_precompact.ps1").replace("\\", "/")
    sh = os.path.join(BASE_DIR, "bin", "hooks", "chatlog",
                      "claude_code_precompact.sh").replace("\\", "/")
    # Emit the snippet that matches the current OS; show the alternate below.
    is_windows = sys.platform == "win32"
    if is_windows:
        hook_cmd = f"powershell -NoProfile -ExecutionPolicy Bypass -File {ps1}"
        alt_label, alt_cmd = "macOS/Linux", f"/bin/sh {sh}"
    else:
        hook_cmd = f"/bin/sh {sh}"
        alt_label, alt_cmd = "Windows", (
            f"powershell -NoProfile -ExecutionPolicy Bypass -File {ps1}"
        )

    hooks_block: dict = {
        "PreCompact": [
            {"hooks": [{"type": "command", "command": hook_cmd}]}
        ],
    }
    cc = config.host_agents.get("claude-code")
    stop_enabled = bool(cc and cc.stop_hook)
    if stop_enabled:
        hooks_block["Stop"] = [
            {"hooks": [{"type": "command", "command": hook_cmd}]}
        ]

    status_script = os.path.join(BASE_DIR, "bin", "chatlog_status_line.py").replace("\\", "/")
    snippet = {
        "hooks": hooks_block,
        "statusLine": {
            "type": "command",
            "command": f"python {status_script}",
        },
    }
    print(json.dumps(snippet, indent=2))
    print()
    print(f"Stop hook: {'ENABLED' if stop_enabled else 'disabled (PreCompact only)'}")
    print("Toggle with: chatlog_init.py --enable-stop-hook | --disable-stop-hook")
    print(f"{alt_label} equivalent command: {alt_cmd}")
    print()
    print("Do NOT auto-edit settings.json. Copy-paste the above manually.")


def apply_stop_hook_toggle(enable: bool) -> int:
    """Flip host_agents['claude-code'].stop_hook, persist, re-print snippet."""
    cfg = resolve_config()
    cc = cfg.host_agents.setdefault("claude-code", HookSpec())
    cc.stop_hook = enable
    save_config(cfg)
    state = "enabled" if enable else "disabled"
    print(f"Claude Code Stop hook {state} in {CONFIG_PATH}")
    print("Update ~/.claude/settings.json to match:")
    show_claude_code_settings_snippet(cfg)
    return 0


def print_summary(config: ChatlogConfig) -> None:
    """Print final configuration summary."""
    print_section("Configuration Summary")
    unified = os.path.abspath(config.db_path) == os.path.abspath(MAIN_DB_PATH)
    print(f"DB Path:            {config.db_path}" + (" (unified with main)" if unified else ""))

    enabled_agents = [a for a, spec in config.host_agents.items() if spec.enabled]
    if enabled_agents:
        print(f"Enabled Agents:     {', '.join(enabled_agents)}")
        cc = config.host_agents.get("claude-code")
        if cc and cc.enabled:
            mode = "per-turn (PreCompact + Stop)" if cc.stop_hook else "PreCompact only"
            print(f"  claude-code:      {mode}")
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
    unified = os.path.abspath(cfg.db_path) == os.path.abspath(MAIN_DB_PATH)
    print_section("Existing Configuration")
    print(f"DB Path:          {cfg.db_path}" + (" (unified with main)" if unified else ""))
    enabled = [a for a, s in cfg.host_agents.items() if s.enabled]
    print(f"Enabled Agents:   {', '.join(enabled) if enabled else '(none)'}")
    cc = cfg.host_agents.get("claude-code")
    if cc and cc.enabled:
        mode = "per-turn (PreCompact + Stop)" if cc.stop_hook else "PreCompact only"
        print(f"  claude-code:    {mode}")
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
        "--db-path",
        default=None,
        help=(
            "Chat log database path. Default: memory/agent_chatlog.db. "
            "Set equal to the main DB (memory/agent_memory.db) to keep all "
            "data in a single file."
        ),
    )
    hook_group = parser.add_mutually_exclusive_group()
    hook_group.add_argument(
        "--enable-stop-hook",
        action="store_true",
        help=("Enable per-turn capture via Claude Code's Stop hook in addition "
              "to PreCompact. Writes config and prints an updated settings.json "
              "snippet. Default is PreCompact-only."),
    )
    hook_group.add_argument(
        "--disable-stop-hook",
        action="store_true",
        help="Disable the Stop hook (revert to PreCompact-only capture).",
    )

    args = parser.parse_args()

    # Standalone toggle actions: apply and exit without reconfigure prompts.
    if args.enable_stop_hook:
        return apply_stop_hook_toggle(enable=True)
    if args.disable_stop_hook:
        return apply_stop_hook_toggle(enable=False)

    try:
        # Check if config exists
        if os.path.exists(CONFIG_PATH) and not args.reconfigure and not args.non_interactive:
            show_existing_config()
            return 0

        # Non-interactive mode
        if args.non_interactive:
            db_path = args.db_path or DEFAULT_DB_PATH

            config = ChatlogConfig(
                db_path=db_path,
                host_agents={a: HookSpec() for a in VALID_HOST_AGENTS},
                cost_tracking=CostTrackingSpec(enabled=True),
                redaction=RedactionSpec(enabled=False),
                embed_sweeper=EmbedSweeperSpec(),
            )
            save_config(config)
            print(f"Configuration saved to {CONFIG_PATH}")

            # Run migrations even in non-interactive mode — without them the
            # chatlog DB is an empty SQLite file and any hook fire will error
            # with 'no such table: memory_items'. The prompt-skipping flag
            # shouldn't mean a broken install.
            migrate_script = os.path.join(BASE_DIR, "bin", "migrate_memory.py")
            try:
                subprocess.run(
                    [sys.executable, migrate_script, "up", "--target", "chatlog", "-y"],
                    check=True,
                )
                print("Migrations applied.")
            except subprocess.CalledProcessError as e:
                print(f"Warning: migrations failed ({e}). Run manually with:")
                print(f"  python {migrate_script} up --target chatlog -y")
                # Don't fail the install — migrations can be retried.
            return 0

        # Interactive mode
        db_path = args.db_path or interactive_db_path()
        host_agents = interactive_host_agents()
        cost_tracking_enabled = interactive_cost_tracking()
        redaction = interactive_redaction()

        # Build config
        config = ChatlogConfig(
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
        show_claude_code_settings_snippet(config)

        print()
        print("Setup complete!")
        return 0

    except KeyboardInterrupt:
        print("\nAborted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
