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
    hermes: bool = False

    def any(self) -> bool:
        return any((self.claude, self.gemini, self.antigravity, self.opencode,
                    self.openclaw, self.hermes))


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
    # Hermes Agent uses a file-based plugin (not MCP wiring): detection drives
    # an offer to COPY the m3 provider into the user's hermes-agent checkout.
    hermes = bool(_find_hermes_plugins_dir())
    return AgentTargets(
        claude=claude, gemini=gemini, antigravity=antigravity,
        opencode=opencode, openclaw=openclaw, hermes=hermes
    )


def _find_hermes_plugins_dir() -> Optional[Path]:
    """Locate the hermes-agent install's plugins/memory/ dir, or None.

    Hermes Agent has no fixed install root, so we probe the common spots. Two
    layouts exist: the checkout root holds plugins/memory directly, OR an
    app-data home dir (e.g. %LOCALAPPDATA%\\hermes) contains a `hermes-agent/`
    checkout one level down. We test both `<root>/plugins/memory` and
    `<root>/hermes-agent/plugins/memory` for every candidate. The
    plugins/memory subtree is the single-select memory-provider slot — its
    presence is what lets us drop the m3 provider into place.
    """
    roots = []
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        roots.append(Path(env_home))
    # Windows app-data location (the `hermes` CLI's default home).
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        roots.append(Path(localappdata) / "hermes")
    roots += [
        Path.home() / "AppData" / "Local" / "hermes",  # explicit Windows fallback
        Path.home() / ".hermes",
        Path.home() / "hermes-agent",
        Path.home() / "hermes",
        Path.home() / "src" / "hermes-agent",
        Path.home() / "code" / "hermes-agent",
    ]
    for root in roots:
        for pm in (root / "plugins" / "memory",
                   root / "hermes-agent" / "plugins" / "memory"):
            try:
                if pm.is_dir():
                    return pm
            except OSError:
                continue
    return None


def _find_m3_hermes_plugin_src() -> Optional[Path]:
    """Locate the bundled m3 Hermes provider source (the directory the wizard
    copies into a user's hermes-agent checkout).

    Vendored at m3_memory/integrations/hermes/ as package-data, so it resolves
    the same way whether installed from a pip wheel (site-packages) or a
    source/editable checkout (repo). The provider files are DATA here — they
    import hermes-agent modules and are not imported in-place.
    """
    src = Path(__file__).resolve().parent / "integrations" / "hermes"
    if (src / "__init__.py").exists():
        return src
    return None


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
    decouple_roots: bool = False
    config_root: Optional[str] = None
    engine_root: Optional[str] = None
    fips_mode: bool = False
    # Replace governor-eligible cron/schtasks entries with the Adaptive
    # Background Workload Governor. Default on; gated by --no-governor-migration.
    migrate_to_governor: bool = True


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
        plan.decouple_roots = bool(getattr(args, "decouple_roots", False))
        plan.config_root = getattr(args, "config_root", None)
        plan.engine_root = getattr(args, "engine_root", None)
        if plan.decouple_roots:
            if not plan.config_root:
                plan.config_root = os.path.expanduser("~/.m3/config")
            if not plan.engine_root:
                plan.engine_root = os.path.expanduser("~/.m3/engine")
        plan.fips_mode = bool(getattr(args, "fips_mode", False))
        # Default ON; --no-governor-migration sets args.no_governor_migration=True.
        plan.migrate_to_governor = not bool(getattr(args, "no_governor_migration", False))
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
    print(f"    {'[x]' if detected.hermes      else '[ ]'} Hermes Agent         (file-based memory-provider plugin)")
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
    if detected.hermes:
        print()
        print("  [Probing] Hermes Agent detected on system!")
        print("  You can configure M3 to:")
        print("    - OPTIMALLY REPLACE Hermes' default memory system for unified, rich SOTA recall, or")
        print("    - RUN ALONGSIDE default memories to extend Hermes' capability with long-term vector search.")
        print("  For complete setup guidance, see the newly created documentation at:")
        try:
            from pathlib import Path as _Path

            from m3_sdk import get_m3_root
            _doc_path = f"file:///{_Path(get_m3_root()).resolve().as_posix()}/docs/HERMES.md"
        except Exception:
            _doc_path = "docs/HERMES.md"
        print(f"  docs/HERMES.md ({_doc_path})")
        print()
        plan.targets.hermes = _ask_yes_no(
            "  Install the m3 SOTA memory-provider plugin into Hermes Agent?",
            default=True,
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

    print()
    print("  Decoupled Directory Scheme:")
    print("    Configure separate folders for configurations (~/.m3/config) and")
    print("    databases (~/.m3/engine) to ensure clean security boundaries.")
    plan.decouple_roots = _ask_yes_no(
        "  Configure decoupled directories (~/.m3/config and ~/.m3/engine)?",
        default=True,
    )
    if plan.decouple_roots:
        plan.config_root = os.path.expanduser("~/.m3/config")
        plan.engine_root = os.path.expanduser("~/.m3/engine")
        _say(f"    Config root: {plan.config_root}")
        _say(f"    Engine root: {plan.engine_root}")

    print()
    print("  Strict FIPS 140-3 Security Lock:")
    print("    Enables strict wolfSSL-based FIPS compliance boundaries for crypto operations.")
    plan.fips_mode = _ask_yes_no(
        "  Enable strict FIPS 140-3 compliance execution mode (M3_FIPS_MODE=1)?",
        default=False,
    )

    # Offer to replace legacy scheduled tasks with the governor — but only if
    # any governor-eligible scheduler entries are actually installed, so we
    # never prompt about a no-op.
    found = _detect_governor_eligible_tasks()
    if found:
        print()
        print("  Adaptive Background Workload Governor:")
        print("    Found existing m3 scheduled task(s) that the governor can take over:")
        for name in found:
            print(f"      • {name}")
        print("    The governor paces these by host load + idle time instead of a rigid")
        print("    clock — it never competes with you, spreads work over idle time, and")
        print("    needs no external scheduler. Replacing the cron/schtasks entries")
        print("    prevents them from double-firing alongside the governor.")
        plan.migrate_to_governor = _ask_yes_no(
            "  Replace these scheduled tasks with the governor?", default=True,
        )

    return plan


def _detect_governor_eligible_tasks() -> list[str]:
    """Read-only probe for installed governor-eligible scheduled tasks. Never
    raises — a missing scheduler tool or import yields an empty list."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
        import governor_migration
        return governor_migration.detect_scheduled_tasks().get("eligible", [])
    except Exception:
        return []


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

    # ── Probe 0: decoupled paths and FIPS configuration ─────────────────
    if plan.decouple_roots:
        _ok(f"  decoupled config root: {plan.config_root}")
        _ok(f"  decoupled engine root: {plan.engine_root}")
    if plan.fips_mode:
        _ok("  strict FIPS 140-3 execution mode: ENABLED")

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
    if sys.platform == "win32":
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
                # Persist so every new shell and every MCP server spawn picks
                # it up automatically. Without this, tier-1 falls back to
                # tier-2 the next time anything reads the env.
                _persist_embed_gguf(discovered, non_interactive=args.non_interactive)
    else:
        _say("  no BGE-M3 GGUF auto-discovered; tier-2 (:8082) will serve all embeds")
        _say("  (set M3_EMBED_GGUF later to enable tier-1; see EMBEDDER_ARCHITECTURE.md)")

    # ── Probe 5: LLM endpoint detection + failover wiring ───────────────
    # Enrichment features (auto-classify, summarize) discover a chat model via
    # bin/llm_failover.py, which only probes endpoints the user opts into.
    # Detect which local runtime is actually reachable and persist the matching
    # opt-in vars, so a non-LM-Studio user (Ollama / llama.cpp / custom) doesn't
    # silently get an unreachable default — and doesn't pay a probe for a
    # provider they don't run.
    _probe_llm_endpoints(plan, args)

    return ok


# Built-in local endpoints the failover layer knows (kept in sync with
# bin/llm_failover.py). Detection here drives which opt-in vars we persist.
_LLM_RUNTIMES = (
    # (label, url, env var that enables it, value to set)
    ("LM Studio", "http://localhost:1234/v1", "M3_ENABLE_LMSTUDIO_FAILOVER", "1"),
    ("Ollama",    "http://localhost:11434/v1", "M3_ENABLE_OLLAMA_FAILOVER", "1"),
)


def _endpoint_reachable(base_url: str, timeout: float = 0.4) -> bool:
    """True if an OpenAI-compatible /v1/models responds at base_url. Fast-fail:
    an absent localhost port should return quickly. An explicit *connect* timeout
    bounds the platform-dependent worst case (on Windows a connect to a dead port
    can otherwise block past a plain total timeout)."""
    try:
        import httpx
    except ImportError:
        return False
    url = base_url.rstrip("/") + "/models"
    try:
        r = httpx.get(url, timeout=httpx.Timeout(timeout, connect=timeout))
        return r.status_code < 500
    except Exception:
        return False


def _probe_llm_endpoints(plan: "SetupPlan", args: argparse.Namespace) -> None:
    """Detect the reachable local LLM runtime(s) and persist the failover opt-in
    vars to match. Mirrors the M3_EMBED_GGUF persistence (shell rc + MCP env)."""
    # If the user already pinned a custom server, honor it — just confirm.
    custom = os.environ.get("M3_LLM_URL", "").strip()
    csv = os.environ.get("LLM_ENDPOINTS_CSV", "").strip()
    if csv:
        _ok(f"  LLM endpoints pinned via LLM_ENDPOINTS_CSV ({csv}); leaving as-is")
        return
    if custom:
        live = _endpoint_reachable(custom)
        _ok(f"  M3_LLM_URL set ({custom}) — {'reachable' if live else 'NOT reachable yet'}")
        return

    reachable = [(label, url, var, val) for (label, url, var, val) in _LLM_RUNTIMES
                 if _endpoint_reachable(url)]
    if not reachable:
        _say("  no local LLM runtime detected on :1234 (LM Studio) or :11434 (Ollama)")
        _say("  enrichment features need a chat model. Point M3 at your server with one of:")
        _say("    LM Studio (default) — just load a model on :1234")
        _say('    Ollama              — export M3_ENABLE_OLLAMA_FAILOVER=1')
        _say('    llama.cpp / vLLM     — export M3_LLM_URL="http://localhost:8080/v1"')
        _say("  (see ENVIRONMENT_VARIABLES.md → Endpoint discovery & failover)")
        return

    for label, url, var, val in reachable:
        _ok(f"  detected {label} reachable at {url}")
        # LM Studio is on by default — only persist the explicit enable for the
        # non-default ones, and an explicit disable for LM Studio if it's absent.
        already = os.environ.get(var, "").strip()
        if already in ("1", "true", "yes"):
            continue
        if var == "M3_ENABLE_LMSTUDIO_FAILOVER":
            continue  # default already on; nothing to persist
        if args.non_interactive or _ask_yes_no(
            f"  Enable {label} for enrichment (persist {var}=1)?", default=True
        ):
            os.environ[var] = val
            _persist_env_var(var, val, non_interactive=args.non_interactive)

    # If LM Studio is NOT reachable but something else is, disable its probe so
    # the user stops paying for a dead :1234 connect on every discovery.
    lmstudio_live = any(label == "LM Studio" for label, *_ in reachable)
    if not lmstudio_live and os.environ.get("M3_ENABLE_LMSTUDIO_FAILOVER", "").strip() not in ("0", "false", "no"):
        _say("  LM Studio (:1234) not reachable — disabling its probe to avoid a dead connect")
        os.environ["M3_ENABLE_LMSTUDIO_FAILOVER"] = "0"
        _persist_env_var("M3_ENABLE_LMSTUDIO_FAILOVER", "0", non_interactive=args.non_interactive)


# ── B15 helpers ──────────────────────────────────────────────────────────

def _find_running_mcp_memory_processes() -> list[int]:
    """Return PIDs of any running mcp-memory.exe processes. Windows only."""
    if sys.platform != "win32":
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


def _persist_embed_gguf(gguf_path: str, *, non_interactive: bool) -> None:
    """Persist M3_EMBED_GGUF so new shells + spawned MCP servers see it.

    Two surfaces, both idempotent and cross-platform:

      1. Shell env — so `mcp-memory doctor` and other CLI invocations see
         the var without the wizard's process being in scope:
           - Unix (macOS / Linux): append `export M3_EMBED_GGUF=...` to
             ~/.zshrc on zsh, ~/.bashrc on bash, ~/.profile fallback.
           - Windows: `setx M3_EMBED_GGUF <path>` writes to the user env
             (HKCU\\Environment), so new cmd / PowerShell sessions inherit
             it. setx does NOT update the current process or other open
             shells — that's fine, the wizard's own os.environ is already set.

      2. The 'memory' MCP server entry's `env` block in
         ~/.claude/settings.json and ~/.gemini/settings.json. MCP servers
         are spawned by Claude Code / Gemini CLI as subprocesses that DO NOT
         inherit the user's interactive shell env on macOS (launchd) or
         Windows (GUI process tree). Without this, the shell rc alone never
         reaches them. Same code path on all 3 platforms — Path.home()
         resolves correctly on each.

    Failure on any surface is reported as a warning but does not abort:
    the GGUF is still set in *this* process, which is enough for the
    rest of setup. Best-effort across surfaces matches the wizard's
    overall posture (post-install steps print warnings, don't crash).
    """
    _persist_embed_gguf_shell(gguf_path, non_interactive=non_interactive)
    _persist_embed_gguf_mcp(gguf_path)


def _persist_embed_gguf_shell(gguf_path: str, *, non_interactive: bool) -> None:
    """Persist M3_EMBED_GGUF for new shell sessions (per-platform mechanism)."""
    if sys.platform == "win32":
        # Windows: setx writes to HKCU\Environment. Persists across reboot;
        # new cmd / PowerShell sessions see it. The current process and
        # other already-open shells are unaffected (by design).
        if not non_interactive and not _ask_yes_no(
            "  Persist M3_EMBED_GGUF to your Windows user environment (setx)?",
            default=True,
        ):
            _warn(f"    skipped — set it later: setx M3_EMBED_GGUF \"{gguf_path}\"")
            return
        try:
            result = subprocess.run(
                ["setx", "M3_EMBED_GGUF", gguf_path],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _warn(f"    setx failed ({e}); set it later: setx M3_EMBED_GGUF \"{gguf_path}\"")
            return
        if result.returncode == 0:
            _ok("    persisted M3_EMBED_GGUF via setx (new shells will see it)")
        else:
            stderr = (result.stderr or result.stdout or "").strip()
            _warn(f"    setx exited {result.returncode}: {stderr}")
        return

    # Unix: append `export M3_EMBED_GGUF=...` to the appropriate shell rc.
    rc_path = _pick_unix_shell_rc()

    if not non_interactive and not _ask_yes_no(
        f"  Persist M3_EMBED_GGUF to {rc_path}?", default=True
    ):
        _warn(f"    skipped — set it later: echo 'export M3_EMBED_GGUF={gguf_path}' >> {rc_path}")
        return

    try:
        existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    except OSError as e:
        _warn(f"    could not read {rc_path} ({e}); skipping shell rc persistence")
        return

    if "M3_EMBED_GGUF" in existing:
        _ok(f"    M3_EMBED_GGUF already present in {rc_path}")
        return

    block = (
        "\n# Added by m3 setup — tier-1 in-process BGE-M3 embedder\n"
        f'export M3_EMBED_GGUF="{gguf_path}"\n'
    )
    try:
        with rc_path.open("a", encoding="utf-8") as f:
            f.write(block)
        _ok(f"    persisted M3_EMBED_GGUF -> {rc_path}")
    except OSError as e:
        _warn(f"    failed to write {rc_path} ({e})")


def _pick_unix_shell_rc() -> Path:
    """Pick the shell rc file most likely to be read on this Unix system.

    Order:
      1. ~/.zshrc if SHELL points at zsh (macOS default since Catalina)
      2. ~/.bashrc if SHELL points at bash (most Linux distros)
      3. First existing among (~/.zshrc, ~/.bashrc, ~/.bash_profile, ~/.profile)
      4. Default to ~/.zshrc (covers fresh macOS Spotlight users)
    """
    home = Path.home()
    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        return home / ".zshrc"
    if "bash" in shell:
        return home / ".bashrc"
    for candidate in (home / ".zshrc", home / ".bashrc",
                      home / ".bash_profile", home / ".profile"):
        if candidate.exists():
            return candidate
    return home / ".zshrc"


def _persist_embed_gguf_mcp(gguf_path: str) -> None:
    """Patch the 'memory' MCP server entry's env block on every platform.

    MCP servers are spawned by Claude Code / Gemini CLI as subprocesses; on
    macOS (launchd) and Windows (GUI process tree) they do not inherit the
    user's interactive shell env. Setting the env on the MCP server entry
    itself is the only reliable way the spawned server sees M3_EMBED_GGUF.

    Same code on all 3 platforms — Path.home() resolves to ~/, %USERPROFILE%,
    or /home/<user> as appropriate.
    """
    for label, settings_path in (
        ("Claude Code", Path.home() / ".claude" / "settings.json"),
        ("Gemini CLI",  Path.home() / ".gemini" / "settings.json"),
    ):
        if not settings_path.is_file():
            continue
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as e:
            _warn(f"    {settings_path} is unreadable ({e}); skipping {label} env wiring")
            continue
        mcp = cfg.get("mcpServers")
        if not isinstance(mcp, dict) or "memory" not in mcp:
            # Memory MCP not yet registered — per-agent wiring step (later
            # in setup) will create it. We don't pre-create here to avoid
            # racing the wiring step's idempotency check.
            continue
        server = mcp["memory"]
        env = server.setdefault("env", {})
        if env.get("M3_EMBED_GGUF") == gguf_path:
            _ok(f"    M3_EMBED_GGUF already set on {label} memory MCP entry")
            continue
        env["M3_EMBED_GGUF"] = gguf_path
        try:
            settings_path.write_text(
                json.dumps(cfg, indent=2) + "\n", encoding="utf-8"
            )
            _ok(f"    set M3_EMBED_GGUF on {label} memory MCP entry ({settings_path})")
        except OSError as e:
            _warn(f"    failed to write {settings_path} ({e})")


def _persist_env_var(name: str, value: str, *, non_interactive: bool) -> None:
    """Generic env-var persistence — shell rc + memory MCP env block, mirroring
    _persist_embed_gguf for arbitrary name/value (e.g. failover opt-in vars).
    Best-effort across surfaces; warnings don't abort."""
    _persist_env_var_shell(name, value, non_interactive=non_interactive)
    _persist_env_var_mcp(name, value)


def _persist_env_var_shell(name: str, value: str, *, non_interactive: bool) -> None:
    """Persist <name>=<value> for new shell sessions (per-platform)."""
    if sys.platform == "win32":
        if not non_interactive and not _ask_yes_no(
            f"  Persist {name} to your Windows user environment (setx)?", default=True
        ):
            _warn(f'    skipped — set it later: setx {name} "{value}"')
            return
        try:
            result = subprocess.run(
                ["setx", name, value], capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            _warn(f'    setx failed ({e}); set it later: setx {name} "{value}"')
            return
        if result.returncode == 0:
            _ok(f"    persisted {name} via setx (new shells will see it)")
        else:
            _warn(f"    setx exited {result.returncode}: {(result.stderr or result.stdout or '').strip()}")
        return

    rc_path = _pick_unix_shell_rc()
    if not non_interactive and not _ask_yes_no(
        f"  Persist {name} to {rc_path}?", default=True
    ):
        _warn(f"    skipped — set it later: echo 'export {name}={value}' >> {rc_path}")
        return
    try:
        existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    except OSError as e:
        _warn(f"    could not read {rc_path} ({e}); skipping shell rc persistence")
        return
    # Idempotent: if the exact assignment is already present, do nothing; if a
    # stale value for the same var exists, append the new one (last wins in sh).
    if f"export {name}={value}" in existing or f'export {name}="{value}"' in existing:
        _ok(f"    {name}={value} already present in {rc_path}")
        return
    block = f'\n# Added by m3 setup — LLM endpoint failover\nexport {name}="{value}"\n'
    try:
        with rc_path.open("a", encoding="utf-8") as f:
            f.write(block)
        _ok(f"    persisted {name} -> {rc_path}")
    except OSError as e:
        _warn(f"    failed to write {rc_path} ({e})")


def _persist_env_var_mcp(name: str, value: str) -> None:
    """Set <name>=<value> on the 'memory' MCP server env block in Claude/Gemini
    settings, so the spawned MCP server (which doesn't inherit shell env on
    macOS/Windows) sees it. Mirrors _persist_embed_gguf_mcp."""
    for label, settings_path in (
        ("Claude Code", Path.home() / ".claude" / "settings.json"),
        ("Gemini CLI",  Path.home() / ".gemini" / "settings.json"),
    ):
        if not settings_path.is_file():
            continue
        try:
            cfg = json.loads(settings_path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError) as e:
            _warn(f"    {settings_path} is unreadable ({e}); skipping {label} env wiring")
            continue
        mcp = cfg.get("mcpServers")
        if not isinstance(mcp, dict) or "memory" not in mcp:
            continue
        env = mcp["memory"].setdefault("env", {})
        if env.get(name) == value:
            _ok(f"    {name} already set on {label} memory MCP entry")
            continue
        env[name] = value
        try:
            settings_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            _ok(f"    set {name} on {label} memory MCP entry ({settings_path})")
        except OSError as e:
            _warn(f"    failed to write {settings_path} ({e})")


def _discover_bge_m3_gguf() -> str | None:
    """Mirror of m3-embed-server's discovery cascade (B5). Probes the same
    paths so the wizard's auto-discovery matches what `m3-embed-server`
    will pick up at install time.

    Cross-platform notes:
      - ~/.lmstudio/models works on macOS and Windows (LM Studio's default
        on both — Path.home() handles the home-dir resolution per-OS).
      - ~/Library/Application Support/LM Studio/models is macOS-specific.
      - ~/.cache/lm-studio/models is LM Studio's Linux default (XDG cache).
    """
    home = Path.home()
    candidate_dirs = [
        home / ".lmstudio" / "models",
        home / "Library" / "Application Support" / "LM Studio" / "models",
        home / ".cache" / "lm-studio" / "models",   # Linux LM Studio default (XDG)
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
    """Run install-m3 with the wizard's chosen capture-mode.

    Always passes --force so re-running `m3 setup` (or `install.sh`) upgrades
    in place instead of aborting with "repo already exists". install_m3()
    preserves user data (chatlog DB, .json/.jsonl state) across --force, so
    this is non-destructive for upgrades and a no-op for fresh installs.
    """
    _say("Step 1/5: fetching m3-memory system payload (install-m3)")
    cmd = [sys.executable, "-m", "m3_memory.cli", "install-m3",
           "--non-interactive", "--force", "--capture-mode", plan.capture_mode]
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
        # Python/HTTP tier) — so this is non-fatal. Print clear, per-OS
        # instructions for getting the always-on embedder.
        print()
        print("  To get the always-on CPU embedder, follow the steps for your OS:")
        if sys.platform == "win32":
            print("    Windows — the embedder registers as a Windows Service, which")
            print("    needs Administrator rights. Open an *Administrator* terminal and run:")
            print("        m3 embedder install-gpu   # installs the binary first")
            print("        m3 embedder install       # registers the service")
        elif sys.platform == "darwin":
            print("    macOS — the embedder installs as a launchd user agent (no sudo).")
            print("    If the binary is missing, install it first, then retry:")
            print("        m3 embedder install-gpu")
            print("        m3 embedder install")
        else:
            # Linux: two common failure modes — missing binary and missing systemd user session.
            has_systemctl = bool(shutil.which("systemctl"))
            has_loginctl  = bool(shutil.which("loginctl"))
            print("    Linux — step 1: install the binary (no Rust needed, prebuilt wheel):")
            print("        m3 embedder install-gpu")
            print()
            if has_systemctl:
                print("    Step 2: register and start via systemd --user:")
                print("        m3 embedder install")
                print()
                print("    If `m3 embedder install` fails with a dbus/systemd error")
                print("    (container, SSH session without a user session bus),")
                print("    run the server directly instead:")
            else:
                print("    systemctl not found — run the server directly:")
            gguf_hint = os.environ.get("M3_EMBED_GGUF", "~/bge-m3-GGUF-Q4_K_M.gguf")
            print(f"        M3_EMBED_GGUF={gguf_hint} \\")
            print( "            nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &")
            print()
            print("    To start automatically on boot (no systemd needed):")
            print("        crontab -e   # then add:")
            print(f"        @reboot M3_EMBED_GGUF={gguf_hint} m3-embed-server >> ~/.m3/engine/embed-server.log 2>&1 &")
            if has_systemctl and has_loginctl:
                print()
                print("    To keep the systemd service running after logout (headless / server):")
                print("        loginctl enable-linger \"$USER\"")
            elif has_systemctl and not has_loginctl:
                print()
                print("    (loginctl not found — linger not available on this system)")
        print()
        print("  Until then, m3 still embeds via its in-process Tier-1 / HTTP fallback tier.")
        return True  # non-fatal


def _step_gpu_embedder() -> bool:
    """Install the in-process GPU embedder (CUDA / Vulkan / Metal autodetected).

    Installs the matching prebuilt m3-core-rs wheel from PyPI
    (``m3-core-rs-<os>-<backend>``); only builds from source (needs a Rust
    toolchain) when no prebuilt wheel matches this platform/Python. Non-fatal:
    failure falls back to the CPU embedder.
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
    _say("  · registering m3 MCP in Claude Code (user scope)")
    try:
        # `--scope user` writes to the user-level config so the MCP is available
        # in every project, not just the one the wizard was run from. (The CLI's
        # default is `local`; there is no `--global` flag — passing it makes
        # `claude mcp add` exit with "unknown option" and register nothing.)
        # `claude mcp add` is idempotent; an existing entry just prints a warning.
        subprocess.run(["claude", "mcp", "add", "--scope", "user", "memory", "m3"], check=False)
    except FileNotFoundError:
        _warn("`claude` CLI failed to invoke; manual: `claude mcp add --scope user memory m3`")
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
    if sys.platform == "win32":
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


# Files copied into the user's hermes-agent plugin dir. README/test stay behind
# in the vendored source — they're dev artifacts, not part of the live plugin.
_HERMES_PLUGIN_FILES = ("__init__.py", "m3client.py", "plugin.yaml")


def _wire_hermes() -> bool:
    """Copy the m3 memory-provider plugin into the user's hermes-agent checkout.

    Hermes Agent loads memory providers from `plugins/memory/<name>/`. We locate
    the vendored source (m3_memory/integrations/hermes/) and the user's hermes
    plugins dir, then copy the plugin files into `plugins/memory/m3/`.
    Non-destructive: if a prior m3 plugin is already there, we ask before
    overwriting.
    """
    import shutil as _shutil

    src = _find_m3_hermes_plugin_src()
    dst_parent = _find_hermes_plugins_dir()
    if not src:
        _warn("  · Hermes: bundled m3 plugin source not found — skipping")
        return False
    if not dst_parent:
        _warn("  · Hermes: no hermes-agent plugins/memory dir found — skipping")
        print(f"    Copy it manually: {src} → <hermes>/plugins/memory/m3/")
        return False

    dst = dst_parent / "m3"
    if dst.exists():
        if not _ask_yes_no(f"  · Hermes: {dst} exists — overwrite?", default=False):
            _say("  · Hermes: left existing m3 plugin untouched")
            return True
        _shutil.rmtree(dst)
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for fname in _HERMES_PLUGIN_FILES:
            _shutil.copy2(src / fname, dst / fname)
    except OSError as e:
        _warn(f"  · Hermes: copy failed ({e}) — skipping")
        return False

    _say(f"  · Hermes: m3 SOTA provider installed at {dst}")
    print("    To complete configuration:")
    print("    1. Add m3-memory's bin/ to PYTHONPATH in Hermes' launch environment.")
    print("    2. Enable and select 'm3' inside `hermes plugins` to replace/run alongside default memory.")
    try:
        from pathlib import Path as _Path

        from m3_sdk import get_m3_root
        _doc_path = f"file:///{_Path(get_m3_root()).resolve().as_posix()}/docs/HERMES.md"
    except Exception:
        _doc_path = "docs/HERMES.md"
    print(f"    For exact instructions and troubleshooting, see docs/HERMES.md ({_doc_path}).")
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
    if plan.targets.hermes:
        _wire_hermes()
    return True


def _step_governor_migration(plan: SetupPlan) -> dict:
    """Replace governor-eligible scheduled tasks with the governor.

    Returns a dict the summary uses to surface results / privileged commands:
        {"removed": [...], "failed": [...], "privileged_cmds": [...],
         "not_migratable": [...]}
    All keys are always present (possibly empty). Never raises — a scheduler
    tool that isn't present just yields empty results.
    """
    result = {"removed": [], "failed": [], "privileged_cmds": [], "not_migratable": []}
    if not plan.migrate_to_governor:
        return result
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
        import governor_migration as gm
    except Exception as e:
        _warn(f"governor migration unavailable: {e}")
        return result

    detected = gm.detect_scheduled_tasks()
    eligible = detected.get("eligible", [])
    result["not_migratable"] = gm.not_migratable_lines()
    if not eligible:
        _say("Governor migration: no governor-eligible scheduled tasks found — nothing to replace.")
        return result

    _say(f"Governor migration: removing {len(eligible)} legacy scheduled task(s) so the governor can take over...")
    removed, failed = gm.try_remove_scheduled_tasks(eligible)
    result["removed"] = removed
    result["failed"] = failed
    for name in removed:
        _ok(f"  removed {name}")
    if failed:
        for name in failed:
            _warn(f"  could not remove {name} (insufficient privilege?) — see end-of-run commands")
        result["privileged_cmds"] = gm.privileged_removal_commands(failed)
    return result


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

def _summary(plan: SetupPlan, governor_result: Optional[dict] = None) -> None:
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
        print("Security & Path Configuration:")
        print("  To ensure these settings persist across shell sessions and are visible to your agents,")
        print("  please add the following environment variables to your shell profile (.bashrc, .zshrc, or Windows Env):")
        if plan.decouple_roots:
            print(f"    export M3_CONFIG_ROOT=\"{plan.config_root}\"")
            print(f"    export M3_ENGINE_ROOT=\"{plan.engine_root}\"")
        if plan.fips_mode:
            print("    export M3_FIPS_MODE=1")
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

    print("Quick checks:")
    print("  m3 doctor           # verify everything")
    print("  m3 --help           # see every subcommand")
    print()


def _os_name_for_summary() -> str:
    """Thin OS branch for summary phrasing (avoids importing governor_migration
    just for the OS check)."""
    if os.name == "nt":
        return "Windows"
    if sys.platform == "darwin":
        return "Darwin"
    return "Linux"


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
        "Hermes Agent": plan.targets.hermes,
    }.items() if v]
    print(f"  agents       : {', '.join(targets) if targets else '(none)'}")
    print(f"  capture mode : {plan.capture_mode}")
    print("  Embedder     : sovereign CPU (BGE-M3 on :8082) — always installed")
    print(f"  GPU add-on   : {'yes' if plan.install_gpu_embedder else 'no'}")
    if plan.endpoint:
        print(f"  LLM endpoint : {plan.endpoint}")
    if plan.cognitive_loop:
        print("  cognitive loop: enabled")
    print(f"  decouple roots: {'yes' if plan.decouple_roots else 'no'}")
    if plan.decouple_roots:
        print(f"    config root: {plan.config_root}")
        print(f"    engine root: {plan.engine_root}")
    print(f"  strict FIPS  : {'yes' if plan.fips_mode else 'no'}")
    print()

    if not args.non_interactive and not _ask_yes_no("Proceed?", default=True):
        _warn("aborted by user — no changes made")
        return 1

    # Set decoupled paths and FIPS compliance mode in environment so child commands inherit
    if plan.decouple_roots:
        os.environ["M3_CONFIG_ROOT"] = plan.config_root
        os.environ["M3_ENGINE_ROOT"] = plan.engine_root
        os.makedirs(plan.config_root, exist_ok=True)
        os.makedirs(plan.engine_root, exist_ok=True)
    if plan.fips_mode:
        os.environ["M3_FIPS_MODE"] = "1"

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
    governor_result = _step_governor_migration(plan)
    _step_doctor()
    _summary(plan, governor_result)
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
    parser.add_argument(
        "--decouple-roots", action="store_true",
        help="Configure decoupled directories (~/.m3/config and ~/.m3/engine).",
    )
    parser.add_argument(
        "--config-root", default=None,
        help="Explicit M3_CONFIG_ROOT path.",
    )
    parser.add_argument(
        "--engine-root", default=None,
        help="Explicit M3_ENGINE_ROOT path.",
    )
    parser.add_argument(
        "--fips-mode", action="store_true",
        help="Enable strict M3_FIPS_MODE=1 execution mode and security locks.",
    )
    parser.add_argument(
        "--no-governor-migration", action="store_true",
        help="Do NOT replace governor-eligible cron/schtasks entries with the "
             "Adaptive Background Workload Governor. By default the wizard offers "
             "to remove legacy scheduled tasks the governor can take over.",
    )
    parser.set_defaults(func=run_setup)
