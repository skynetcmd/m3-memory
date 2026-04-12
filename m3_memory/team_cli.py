"""
m3-team CLI — entry point for the multi-agent team orchestrator.

Usage:
    m3-team init [path]         # write a starter team.yaml (LM Studio, no API keys)
    m3-team check [path]        # validate yaml + ping each provider
    m3-team run [path]          # run the orchestrator (default subcommand)
    m3-team --version

The CLI is a thin wrapper around examples/multi-agent-team/orchestrator.py.
It exists so users who installed via `pip install m3-memory` get a
discoverable command without having to know the example layout.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

DEFAULT_TEAM_FILE = "team.yaml"

MINIMAL_TEMPLATE = """\
# m3-team minimal starter — single LM Studio agent, zero API keys.
#
# Prereq: LM Studio (or any local OpenAI-compat server) running at
# http://localhost:1234. Load any chat model with tool-calling support.
#
# Once it's running:
#     m3-team check     # confirms the provider answers
#     m3-team run       # starts polling
#
# Then queue work from another terminal (Claude Code, Gemini CLI, or any
# MCP client connected to m3-memory):
#     task_create("Summarize the README", created_by="you")
#     task_assign(<task_id>, "local-agent")

agents:
  - name: local-agent
    provider: lm-studio
    model: local-model            # whatever your LM Studio loaded
    role: generalist
    capabilities: [code, write, summarize, plan]
    system_prompt: |
      You are a helpful local agent with access to m3-memory tools.
      Read the task, do the work, write the result to memory with
      memory_write, and mark the task complete with task_update.

providers:
  lm-studio:
    format: openai_compat
    base_url: http://localhost:1234/v1/chat/completions
    api_key_env: LM_STUDIO_API_KEY    # any non-empty value works

orchestrator:
  poll_interval_seconds: 5
  max_iterations: 0
  max_concurrent_dispatches: 2
  notification_kinds:
    - task_assigned
    - handoff
  dispatch_limits:
    max_turns: 8
    max_tool_calls: 24
    max_seconds: 120
    max_tokens_per_call: 4096
    provider_retries: 3
"""


def _example_dir() -> Path:
    """Locate examples/multi-agent-team relative to this installed package."""
    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parent
    return repo_root / "examples" / "multi-agent-team"


def _add_orchestrator_to_path() -> None:
    sys.path.insert(0, str(_example_dir()))
    sys.path.insert(0, str(_example_dir().parent.parent / "bin"))


def cmd_init(path: Path) -> int:
    if path.exists():
        print(f"refusing to overwrite existing {path}", file=sys.stderr)
        return 1
    path.write_text(MINIMAL_TEMPLATE, encoding="utf-8")
    print(f"wrote {path}")
    print("next: start LM Studio, then run `m3-team check` and `m3-team run`")
    return 0


def cmd_check(path: Path) -> int:
    if not path.exists():
        print(f"team file not found: {path}", file=sys.stderr)
        return 1

    try:
        import yaml
    except ImportError:
        print("PyYAML is required: pip install pyyaml", file=sys.stderr)
        return 1

    try:
        team = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        print(f"yaml parse error in {path}: {e}", file=sys.stderr)
        return 1

    errors: list[str] = []
    if not isinstance(team, dict):
        print(f"{path}: top-level must be a mapping", file=sys.stderr)
        return 1

    agents = team.get("agents") or []
    providers = team.get("providers") or {}
    orch = team.get("orchestrator") or {}

    if not agents:
        errors.append("no agents defined")
    if not providers:
        errors.append("no providers defined")
    if "notification_kinds" not in orch:
        errors.append("orchestrator.notification_kinds is required")

    valid_formats = {"openai_compat", "anthropic", "gemini"}
    for pname, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            errors.append(f"provider {pname}: not a mapping")
            continue
        fmt = pcfg.get("format")
        if fmt not in valid_formats:
            errors.append(f"provider {pname}: format must be one of {sorted(valid_formats)}")
        for required in ("base_url", "api_key_env"):
            if required not in pcfg:
                errors.append(f"provider {pname}: missing {required}")

    for agent in agents:
        if not isinstance(agent, dict):
            errors.append("agent entry is not a mapping")
            continue
        for required in ("name", "provider", "model", "role", "system_prompt"):
            if required not in agent:
                errors.append(f"agent {agent.get('name', '?')}: missing {required}")
        if agent.get("provider") not in providers:
            errors.append(
                f"agent {agent.get('name', '?')}: provider "
                f"{agent.get('provider')!r} is not defined under providers"
            )

    if errors:
        print(f"{path}: {len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"{path}: ok ({len(agents)} agent(s), {len(providers)} provider(s))")

    # Soft API key check — warn but don't fail.
    missing = [
        p["api_key_env"] for p in providers.values()
        if p.get("api_key_env") and not os.getenv(p["api_key_env"])
    ]
    if missing:
        print(f"  note: these env vars are unset: {', '.join(sorted(set(missing)))}")
        print("        local providers (LM Studio, Ollama) accept any non-empty value.")

    return 0


def cmd_run(path: Path) -> int:
    if not path.exists():
        print(f"team file not found: {path}", file=sys.stderr)
        print("hint: run `m3-team init` to create a starter file", file=sys.stderr)
        return 1

    example_dir = _example_dir()
    if not (example_dir / "orchestrator.py").exists():
        print(
            f"orchestrator.py not found at {example_dir}\n"
            "If you installed m3-memory via pip, clone the repo and run from there:\n"
            "  git clone https://github.com/skynetcmd/m3-memory\n"
            "  cd m3-memory/examples/multi-agent-team && python orchestrator.py",
            file=sys.stderr,
        )
        return 1

    _add_orchestrator_to_path()
    from orchestrator import main as orchestrator_main  # type: ignore
    asyncio.run(orchestrator_main(path))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="m3-team",
        description="Multi-agent team orchestrator for m3-memory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  m3-team init                  # write a starter team.yaml in cwd
  m3-team check team.yaml       # validate the file and provider config
  m3-team run team.yaml         # start polling and dispatching

Docs: https://github.com/skynetcmd/m3-memory/tree/main/examples/multi-agent-team
""",
    )
    parser.add_argument("--version", action="version", version="m3-team 2026.4.8")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="write a starter team.yaml (LM Studio, no API keys)")
    p_init.add_argument("path", nargs="?", default=DEFAULT_TEAM_FILE)

    p_check = sub.add_parser("check", help="validate team.yaml")
    p_check.add_argument("path", nargs="?", default=DEFAULT_TEAM_FILE)

    p_run = sub.add_parser("run", help="run the orchestrator (default)")
    p_run.add_argument("path", nargs="?", default=DEFAULT_TEAM_FILE)

    args = parser.parse_args()
    cmd = args.cmd or "run"
    path = Path(getattr(args, "path", DEFAULT_TEAM_FILE))

    if cmd == "init":
        sys.exit(cmd_init(path))
    if cmd == "check":
        sys.exit(cmd_check(path))
    if cmd == "run":
        sys.exit(cmd_run(path))
    parser.print_help()
    sys.exit(2)


if __name__ == "__main__":
    main()
