"""One-command setup wizard for m3-memory.

After `pip install m3-memory`, the user runs `m3 setup`. This module asks a
short series of questions (which agents to wire, capture mode, GPU embedder
y/n), then drives every install step end-to-end:

    install-m3 -> CPU-HTTP fallback service (always) -> per-agent MCP wiring
    -> chatlog hooks -> optional GPU in-process embedder -> doctor

Goal: user pastes one command, answers a handful of questions, restarts their
agent. That's it.

Non-interactive mode mirrors every prompt with a flag so `install.sh` and
`install.ps1` can drive the same logic unattended.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── small UI helpers ──────────────────────────────────────────────────────────

def _color(code: str, msg: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return msg
    return f"\033[{code}m{msg}\033[0m"

def _say(msg: str) -> None:
    print(f"{_color('36', '==>')} {msg}", flush=True)

def _ok(msg: str) -> None:
    print(_color("32", f"[OK] {msg}"), flush=True)

def _warn(msg: str) -> None:
    print(_color("33", f"[!] {msg}"), flush=True)

def _err(msg: str) -> None:
    print(_color("31", f"[X] {msg}"), file=sys.stderr, flush=True)


def _ask_yes_no(question: str, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        ans = input(question + suffix + " ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def _ask_choice(question: str, choices: list[str], default: str) -> str:
    pretty = "/".join(c if c != default else c.upper() for c in choices)
    while True:
        ans = input(f"{question} [{pretty}] ").strip().lower()
        if not ans:
            return default
        if ans in choices:
            return ans
        print(f"  please answer one of {', '.join(choices)}")


# ── agent detection ───────────────────────────────────────────────────────────

@dataclass
class AgentTargets:
    claude: bool = False
    gemini: bool = False
    antigravity: bool = False
    opencode: bool = False
    openclaw: bool = False

    def any(self) -> bool:
        return any((self.claude, self.gemini, self.antigravity, self.opencode, self.openclaw))


def _detect_agents() -> AgentTargets:
    """Probe PATH (and well-known fallback locations) for each agent CLI."""
    claude = bool(shutil.which("claude"))
    # Gemini may be installed via npm-global and not on PATH yet.
    gemini = bool(
        shutil.which("gemini")
        or (Path.home() / ".npm-global" / "bin" / "gemini").exists()
    )
    # Antigravity CLI (agy)
    antigravity = bool(
        shutil.which("agy")
        or (Path.home() / ".local" / "bin" / "agy").exists()
        or (Path.home() / ".gemini" / "antigravity-cli").is_dir()
    )
    opencode = bool(shutil.which("opencode"))
    # OpenClaw has no native MCP, so detection drives the proxy default rather
    # than direct wiring. Signals: openclaw CLI on PATH (npm-global), the
    # well-known npm-global fallback, the user's workspace dir, or the gateway
    # token env var from a prior setup.
    openclaw = bool(
        shutil.which("openclaw")
        or (Path.home() / ".npm-global" / "bin" / "openclaw").exists()
        or (Path.home() / ".openclaw").is_dir()
        or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    )
    return AgentTargets(
        claude=claude, gemini=gemini, antigravity=antigravity,
        opencode=opencode, openclaw=openclaw
    )


# ── plan dataclass ────────────────────────────────────────────────────────────

@dataclass
class SetupPlan:
    targets: AgentTargets = field(default_factory=AgentTargets)
    capture_mode: str = "both"      # both | stop | precompact | none
    install_gpu_embedder: bool = False   # CPU fallback always installs; GPU is the choice
    endpoint: Optional[str] = None
    cognitive_loop: bool = False
    # B15: GGUF path discovered + accepted in preflight. Used by the embedder
    # install step to pin tier-1 into the service config.toml so it persists.
    embed_gguf: Optional[str] = None


# ── prompt phase ──────────────────────────────────────────────────────────────

def _gather_plan(detected: AgentTargets, args: argparse.Namespace) -> SetupPlan:
    """Interactive (or flag-driven) construction of the SetupPlan.

    Every prompt is mirrored by a flag so install.sh / install.ps1 can drive
    the same logic with --non-interactive.
    """
    plan = SetupPlan()
    plan.endpoint = args.endpoint
    plan.cognitive_loop = bool(args.cognitive_loop)

    if args.non_interactive:
        # Honor explicit --agent flags; otherwise wire whatever we detected.
        if args.agents:
            for a in args.agents.split(","):
                setattr(plan.targets, a.strip().lower(), True)
        else:
            plan.targets = detected
        plan.capture_mode = args.capture_mode or "both"
        plan.install_gpu_embedder = bool(args.install_gpu_embedder)
        return plan

    # ── interactive prompts ───────────────────────────────────────────────────
    print()
    _say("m3-memory setup — answer a few quick questions, then sit back.")
    print()
    print("  Agents detected on PATH:")
    print(f"    {'[x]' if detected.claude      else '[ ]'} Claude Code          (claude)")
    print(f"    {'[x]' if detected.gemini      else '[ ]'} Gemini CLI           (gemini)")
    print(f"    {'[x]' if detected.antigravity else '[ ]'} Antigravity CLI/Desktop (antigravity)")
    print(f"    {'[x]' if detected.opencode    else '[ ]'} OpenCode             (opencode)")
    print(f"    {'[x]' if detected.openclaw    else '[ ]'} OpenClaw             (no native MCP; wired via local proxy)")
    print()

    if detected.claude:
        plan.targets.claude = _ask_yes_no("  Wire m3 into Claude Code?", default=True)
    if detected.gemini:
        plan.targets.gemini = _ask_yes_no("  Wire m3 into Gemini CLI?", default=True)
    if detected.antigravity:
        plan.targets.antigravity = _ask_yes_no("  Wire m3 into Antigravity CLI/Desktop?", default=True)
    if detected.opencode:
        plan.targets.opencode = _ask_yes_no("  Wire m3 into OpenCode?", default=True)
    plan.targets.openclaw = _ask_yes_no(
        "  Set up OpenClaw proxy (localhost:9000)?", default=detected.openclaw
    )

    print()
    print("  Chatlog capture mode for Claude Code:")
    print("    both       = Stop + PreCompact (recommended — zero-gap)")
    print("    stop       = per-turn capture only")
    print("    precompact = capture only before context compaction")
    print("    none       = no chatlog capture")
    plan.capture_mode = _ask_choice(
        "  Capture mode?",
        choices=["both", "stop", "precompact", "none"],
        default="both",
    )

    print()
    print("  Embedder:")
    print("    Sovereign baseline: our own BGE-M3 CPU embedder on port 8082.")
    print("    ALWAYS installs — works with no GPU, no LM Studio, no Ollama,")
    print("    no internet, no GPU drivers, no model server.")
    print("    GPU acceleration is opt-in and gives ~10-50x faster embeddings.")
    plan.install_gpu_embedder = _ask_yes_no(
        "  Install GPU-accelerated in-process embedder too? (auto-detects CUDA/Vulkan/Metal)",
        default=False,
    )

    return plan


# ── execution phase ───────────────────────────────────────────────────────────

def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Shell out, streaming output. Returns the completed process."""
    return subprocess.run(cmd, check=check)


def _step_preflight(plan: SetupPlan, args: argparse.Namespace) -> bool:
    """B15: pre-install probes that catch the failure modes seen during the
    2026-05-27 wizard-hardening session.

    Each probe is best-effort and prints a clear warning on detection.
    Returns False ONLY for fatal issues that would make install-m3 hang
    (running mcp-memory.exe + non-interactive without --force-kill-mcp).
    """
    _say("Step 0/5: pre-install checks (B15)")
    ok = True

    # ── Probe 1: stale m3_memory/ package shadowing ─────────────────────
    # If another repo (e.g. an old m3-lme-s clone) has a top-level
    # `m3_memory/` directory on sys.path BEFORE the venv's installed
    # m3-memory, Python will resolve `import m3_memory.installer` to the
    # stale copy. That broke bridge resolution on 2026-05-27.
    try:
        import m3_memory as _mm
        installed_path = Path(_mm.__file__).resolve().parent
        canonical = Path(__file__).resolve().parent
        if installed_path != canonical:
            _warn(
                f"shadowing: `import m3_memory` resolves to {installed_path} "
                f"but this wizard is at {canonical}. Stale package copy on "
                f"sys.path will break bridge resolution. Delete the stale dir."
            )
            ok = False
    except Exception as e:
        _warn(f"  could not verify package resolution: {type(e).__name__}: {e}")
    else:
        _ok(f"  package resolution: {installed_path}")

    # ── Probe 2: running mcp-memory.exe will lock the venv binary ──────
    # On Windows, pip install -e cannot overwrite mcp-memory.exe if a
    # process is using it. Detect + offer to kill (interactive) or warn
    # (non-interactive).
    if platform.system() == "Windows":
        running = _find_running_mcp_memory_processes()
        if running:
            _warn(f"  {len(running)} mcp-memory.exe process(es) running: {running}")
            if args.non_interactive:
                if getattr(args, "force_kill_mcp", False):
                    for pid in running:
                        _kill_process_windows(pid)
                    _ok(f"  killed {len(running)} mcp-memory.exe process(es)")
                else:
                    _err("  refusing to install with mcp-memory.exe locked. "
                         "Re-run with --force-kill-mcp or stop the agent first.")
                    return False
            else:
                if _ask_yes_no("  Kill running mcp-memory.exe so install can proceed?", default=True):
                    for pid in running:
                        _kill_process_windows(pid)
                    _ok(f"  killed {len(running)} mcp-memory.exe process(es)")
                else:
                    _warn("  install may fail with 'file in use' until you stop the agent")
        else:
            _ok("  no running mcp-memory.exe locks")
    else:
        # Unix: rename-into-place during pip install means a running binary
        # doesn't block reinstall the way Windows file-locking does. Probe
        # is best-effort informational only.
        _ok("  Unix: running mcp-memory does not block reinstall (rename-in-place)")

    # ── Probe 3: stale __pycache__ in the repo (developer pip-install -e) ──
    # When the install is editable and pycache contains .pyc files newer
    # than .py source from a previous version, Python may load the stale
    # bytecode. Idempotent wipe — safe to do anytime.
    canonical_root = Path(__file__).resolve().parent.parent
    pycache_dirs = list(canonical_root.rglob("__pycache__"))
    if pycache_dirs:
        _say(f"  found {len(pycache_dirs)} __pycache__ dirs in {canonical_root}")
        if getattr(args, "clean_cache", False) or (
            not args.non_interactive
            and _ask_yes_no("  Wipe __pycache__ before install? (recommended)", default=True)
        ):
            wiped = 0
            for d in pycache_dirs:
                try:
                    shutil.rmtree(d)
                    wiped += 1
                except Exception:
                    pass
            _ok(f"  wiped {wiped} __pycache__ dirs")
        else:
            _warn("  skipped __pycache__ wipe — stale bytecode may load")
    else:
        _ok("  no stale __pycache__ to wipe")

    # ── Probe 4: tier-1 GGUF auto-discovery + prompt ────────────────────
    # If the operator doesn't set M3_EMBED_GGUF, they fall back to tier-2
    # (the :8082 service) which is slower. Discover a BGE-M3 GGUF and
    # offer to wire it in.
    discovered = _discover_bge_m3_gguf()
    if discovered:
        env_set = bool(os.environ.get("M3_EMBED_GGUF"))
        if env_set:
            _ok(f"  M3_EMBED_GGUF already set: {os.environ['M3_EMBED_GGUF']}")
        else:
            _say(f"  discovered BGE-M3 GGUF: {discovered}")
            _say("  setting it via M3_EMBED_GGUF gives ~10-100x faster embeds "
                 "on the hot path (tier-1 in-proc vs tier-2 HTTP)")
            if args.non_interactive or _ask_yes_no(
                "  Use this GGUF for tier-1 in-proc embedder?", default=True
            ):
                # Set for THIS process so cpu_embedder install picks it up,
                # and stash on the plan so per-agent wiring records it.
                os.environ["M3_EMBED_GGUF"] = discovered
                plan.embed_gguf = discovered
                _ok(f"  M3_EMBED_GGUF set for this session: {discovered}")
                _say("  (to persist: add to your shell rc OR "
                     "let the wizard's m3-embed-server install pin it)")
    else:
        _say("  no BGE-M3 GGUF auto-discovered; tier-2 (:8082) will serve all embeds")
        _say("  (set M3_EMBED_GGUF later to enable tier-1; see EMBEDDER_ARCHITECTURE.md)")

    return ok


# ── B15 helpers ──────────────────────────────────────────────────────────

def _find_running_mcp_memory_processes() -> list[int]:
    """Return PIDs of any running mcp-memory.exe processes. Windows only."""
    if platform.system() != "Windows":
        return []
    try:
        out = subprocess.run(
            ["tasklist", "/fi", "imagename eq mcp-memory.exe", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        pids: list[int] = []
        for line in out.stdout.splitlines():
            # CSV: "mcp-memory.exe","PID","...","..."
            parts = [p.strip('" ') for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    pids.append(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return []


def _kill_process_windows(pid: int) -> bool:
    """Force-kill a process by PID. Returns True on success."""
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return True
    except Exception:
        return False


def _discover_bge_m3_gguf() -> str | None:
    """Mirror of m3-embed-server's discovery cascade (B5). Probes the same
    paths so the wizard's auto-discovery matches what `m3-embed-server`
    will pick up at install time.
    """
    home = Path.home()
    candidate_dirs = [
        home / ".lmstudio" / "models",
        home / "Library" / "Application Support" / "LM Studio" / "models",
        home / ".cache" / "m3" / "models",
        home / ".m3-memory" / "_assets" / "embedder",
        home / "models",
    ]
    for d in candidate_dirs:
        if not d.is_dir():
            continue
        # Walk up to depth 4 looking for *bge[-_]m3*.gguf (case-insensitive)
        for path in d.rglob("*.gguf"):
            name = path.name.lower()
            if "bge-m3" in name or "bge_m3" in name:
                return str(path)
    return None


def _step_install_m3(plan: SetupPlan) -> bool:
    """Run install-m3 with the wizard's chosen capture-mode."""
    _say("Step 1/5: fetching m3-memory system payload (install-m3)")
    cmd = [sys.executable, "-m", "m3_memory.cli", "install-m3",
           "--non-interactive", "--capture-mode", plan.capture_mode]
    if plan.endpoint:
        cmd += ["--endpoint", plan.endpoint]
    if plan.cognitive_loop:
        cmd.append("--cognitive-loop")
    try:
        _run(cmd)
        _ok("payload installed")
        return True
    except subprocess.CalledProcessError as e:
        _err(f"install-m3 failed (exit {e.returncode}); see output above")
        return False


def _step_cpu_sovereign_embedder() -> bool:
    """Install the sovereign baseline embedder: BGE-M3 CPU on port 8082.

    Always runs. This is the new 'works with no LM Studio, no Ollama, no GPU,
    no internet' default. Concurrency=2; OpenAI-compatible HTTP endpoint.

    Delegates to `m3 embedder install` which:
      1. fetches bge-m3 Q4_K_M.gguf into ~/.m3-memory/models/ (one-time, ~300MB)
      2. locates the m3-embed-server binary (from the m3-core-rs `oxidation` extra)
      3. registers it as a systemd / launchd / Windows Service with concurrency=2
      4. starts it
    """
    _say("Step 2/5: installing sovereign CPU embedder (BGE-M3 on port 8082)")
    cmd = [sys.executable, "-m", "m3_memory.cli", "embedder", "install",
           "--concurrency", "2"]
    try:
        _run(cmd)
        _ok("sovereign CPU embedder registered and running on port 8082")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        _warn(f"CPU embedder install did not complete: {e}")
        # m3 still works without it (the embed cascade falls through to the
        # Python/HTTP tier) — so this is non-fatal. But print clear, per-OS
        # instructions for getting the always-on embedder, because the most
        # common cause is OS-specific (Windows needs elevation; mac/Linux may
        # need the m3-embed-server binary or a linger setting).
        print()
        print("  To get the always-on CPU embedder, follow the steps for your OS:")
        if sys.platform == "win32":
            print("    Windows — the embedder registers as a Windows Service, which")
            print("    needs Administrator rights. Open an *Administrator* terminal and run:")
            print("        m3 embedder install")
        elif sys.platform == "darwin":
            print("    macOS — the embedder installs as a launchd user agent (no sudo).")
            print("    Re-run, and if it still fails the m3-embed-server binary is")
            print("    likely missing — install the Rust core, then retry:")
            print("        m3 embedder install")
        else:
            print("    Linux — the embedder installs as a `systemd --user` unit (no sudo).")
            print("    Re-run `m3 embedder install`. A `--user` service stops at logout;")
            print("    to keep it running across logout / on a headless box also run:")
            print("        loginctl enable-linger \"$USER\"")
        print()
        print("  Until then, m3 still embeds via its Python/HTTP fallback tier.")
        return True  # non-fatal


def _step_gpu_embedder() -> bool:
    """Install the in-process GPU embedder (CUDA / Vulkan / Metal autodetected).

    Builds m3-core-rs with the appropriate `embedded-<gpu>` feature. Requires
    a Rust toolchain. Non-fatal: failure falls back to the CPU embedder.
    """
    _say("Step 3/5: installing GPU-accelerated in-process embedder")
    cmd = [sys.executable, "-m", "m3_memory.cli", "embedder", "install-gpu"]
    try:
        _run(cmd)
        _ok("GPU in-process embedder installed")
        return True
    except subprocess.CalledProcessError as e:
        _warn(
            f"GPU embedder install failed (exit {e.returncode}); "
            "continuing — CPU embedder on port 8082 serves all embeddings."
        )
        return True  # non-fatal


# ── per-agent wiring ──────────────────────────────────────────────────────────

def _wire_claude(capture_mode: str) -> bool:
    """Register the m3 MCP in Claude Code via `claude mcp add`, then run chatlog
    hook init for Claude. Skips silently if `claude` CLI isn't present."""
    if not shutil.which("claude"):
        _warn("Claude CLI not on PATH; skipping Claude wiring")
        return False
    _say("  · registering m3 MCP in Claude Code")
    try:
        # `claude mcp add` is idempotent; an existing entry just prints a warning.
        subprocess.run(["claude", "mcp", "add", "memory", "m3"], check=False)
    except FileNotFoundError:
        _warn("`claude` CLI failed to invoke; manual: `claude mcp add memory m3`")
        return False
    return True


def _wire_gemini() -> bool:
    """Write the m3 MCP entry into ~/.gemini/settings.json. Reuses the
    installer's helper so the wizard never reimplements JSON-merge semantics."""
    from m3_memory.installer import _register_gemini_mcp
    msg = _register_gemini_mcp()
    if msg:
        _say(f"  · {msg.lstrip('[+=!]').strip()}")
    return True


def _wire_opencode() -> bool:
    """Append an `m3` MCP entry to the user's opencode.json. Idempotent."""
    if platform.system() == "Windows":
        cfg_path = Path(os.environ.get("APPDATA", "")) / "opencode" / "opencode.json"
    else:
        cfg_path = Path.home() / ".config" / "opencode" / "opencode.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if cfg_path.is_file():
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            _warn(f"{cfg_path} is unreadable; skipping OpenCode wiring")
            return False
    mcp = existing.setdefault("mcp", {})
    if "memory" in mcp:
        _say(f"  · OpenCode already wired ({cfg_path})")
        return True
    mcp["memory"] = {"type": "local", "command": ["m3"], "enabled": True}
    existing.setdefault("$schema", "https://opencode.ai/config.json")
    cfg_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    _say(f"  · wrote OpenCode config to {cfg_path}")
    return True


def _wire_antigravity() -> bool:
    """Write the m3 MCP entry into ~/.gemini/antigravity-cli/settings.json."""
    from m3_memory.installer import _register_antigravity_mcp
    msg = _register_antigravity_mcp()
    if msg:
        _say(f"  · {msg.lstrip('[+=!]').strip()}")
    return True


def _wire_openclaw_note() -> bool:
    """OpenClaw can't speak MCP natively — print proxy instructions."""
    _say("  · OpenClaw needs the local proxy on http://localhost:9000/v1")
    print("    Start with: m3 proxy start  (or `python bin/mcp_proxy.py`)")
    print("    Point OpenClaw's OpenAI base URL at http://localhost:9000/v1")
    return True


def _step_wire_agents(plan: SetupPlan) -> bool:
    """Wire MCP entries for every selected agent."""
    if not plan.targets.any():
        _say("Step 4/5: no agents selected — skipping wiring")
        return True
    _say("Step 4/5: wiring selected agents")
    if plan.targets.claude:
        _wire_claude(plan.capture_mode)
    if plan.targets.gemini:
        _wire_gemini()
    if plan.targets.antigravity:
        _wire_antigravity()
    if plan.targets.opencode:
        _wire_opencode()
    if plan.targets.openclaw:
        _wire_openclaw_note()
    return True


def _step_doctor() -> bool:
    """Final verification."""
    _say("Step 5/5: running doctor")
    try:
        _run([sys.executable, "-m", "m3_memory.cli", "doctor"])
        _ok("doctor passed")
        return True
    except subprocess.CalledProcessError:
        _warn("doctor reported warnings — review above, then re-run `m3 doctor`")
        return True  # non-fatal — warnings ≠ broken install


# ── orchestrator ──────────────────────────────────────────────────────────────

def _summary(plan: SetupPlan) -> None:
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
    print("Quick checks:")
    print("  m3 doctor           # verify everything")
    print("  m3 --help           # see every subcommand")
    print()


def run_setup(args: argparse.Namespace) -> int:
    """Top-level entry point invoked by `m3 setup`."""
    detected = _detect_agents()
    plan = _gather_plan(detected, args)

    print()
    _say("Plan:")
    targets = [n for n, v in {
        "Claude Code": plan.targets.claude,
        "Gemini CLI": plan.targets.gemini,
        "Antigravity CLI/Desktop": plan.targets.antigravity,
        "OpenCode": plan.targets.opencode,
        "OpenClaw": plan.targets.openclaw,
    }.items() if v]
    print(f"  agents       : {', '.join(targets) if targets else '(none)'}")
    print(f"  capture mode : {plan.capture_mode}")
    print(f"  Embedder     : sovereign CPU (BGE-M3 on :8082) — always installed")
    print(f"  GPU add-on   : {'yes' if plan.install_gpu_embedder else 'no'}")
    if plan.endpoint:
        print(f"  LLM endpoint : {plan.endpoint}")
    if plan.cognitive_loop:
        print(f"  cognitive loop: enabled")
    print()

    if not args.non_interactive and not _ask_yes_no("Proceed?", default=True):
        _warn("aborted by user — no changes made")
        return 1

    # Execute. Step 0 (preflight) and Step 1 (install-m3) can hard-abort.
    if not _step_preflight(plan, args):
        _err("setup aborted by preflight")
        return 2
    if not _step_install_m3(plan):
        _err("setup aborted")
        return 2
    _step_cpu_sovereign_embedder()
    if plan.install_gpu_embedder:
        _step_gpu_embedder()
    _step_wire_agents(plan)
    _step_doctor()
    _summary(plan)
    return 0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add `m3 setup` flags to an argparse subparser."""
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Run unattended with flag-driven choices (used by install.sh/install.ps1).",
    )
    parser.add_argument(
        "--agents", default=None,
        help="Comma-separated list of agents to wire in non-interactive mode "
             "(any of: claude,gemini,opencode,openclaw). "
             "Default: every agent detected on PATH.",
    )
    parser.add_argument(
        "--capture-mode", default=None, choices=("both", "stop", "precompact", "none"),
        help="Chatlog capture mode for Claude Code. Default: both.",
    )
    # ── B15 preflight flags ─────────────────────────────────────────────
    parser.add_argument(
        "--clean-cache", action="store_true",
        help="Wipe __pycache__ dirs before install (non-interactive default: skip).",
    )
    parser.add_argument(
        "--force-kill-mcp", action="store_true",
        help="Kill any running mcp-memory.exe before install (Windows only; "
             "required if MCP is currently in use and you're running "
             "non-interactively).",
    )

    parser.add_argument(
        "--install-gpu-embedder", action="store_true",
        help="Also build and install the GPU-accelerated in-process embedder "
             "(CUDA / Vulkan / Metal autodetected). Default: CPU fallback only.",
    )
    parser.add_argument(
        "--endpoint", default=None,
        help="Pin LLM_ENDPOINTS_CSV (forwarded to install-m3).",
    )
    parser.add_argument(
        "--cognitive-loop", action="store_true",
        help="Enable the background cognitive loop worker.",
    )
    parser.set_defaults(func=run_setup)
