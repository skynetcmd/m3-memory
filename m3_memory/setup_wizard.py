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
import atexit
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── small UI helpers ──────────────────────────────────────────────────────────
# Pure output helpers (never monkeypatched, never call a patched fn) live in
# wizard/ui.py; re-imported here so `setup_wizard._say` etc. keep resolving.
from .wizard.ui import (  # noqa: F401 — re-exported for setup_wizard.<name> access
    _color,
    _err,
    _ok,
    _progress,
    _progress_done,
    _say,
    _warn,
)


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
    cursor: bool = False
    cline: bool = False

    def any(self) -> bool:
        return any((self.claude, self.gemini, self.antigravity, self.opencode,
                    self.openclaw, self.hermes, self.cursor, self.cline))


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
    # Cursor / Cline are VS Code-family MCP clients wired by the install-m3
    # registrars (installer._register_cursor_mcp / _register_cline_mcp). Detect
    # them by the SAME presence signal those registrars gate on, so the wizard's
    # displayed detection matches what actually gets wired: Cursor -> ~/.cursor
    # exists; Cline -> its VS Code extension globalStorage dir exists.
    cursor = (Path.home() / ".cursor").is_dir()
    try:
        from m3_memory.installer import _cline_config_path
        cline = _cline_config_path().parent.parent.is_dir()
    except Exception:
        cline = False
    return AgentTargets(
        claude=claude, gemini=gemini, antigravity=antigravity,
        opencode=opencode, openclaw=openclaw, hermes=hermes,
        cursor=cursor, cline=cline,
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
    # Default OFF: the SHARED tier-2 embedder (one localhost HTTP server on :8082,
    # see use_shared_embedder below) is the preferred shipped configuration.
    # The reason is architectural, NOT speed — an embed call is already low-µs to
    # tens-of-µs, so tier 1 vs tier 2 latency is typically not discernible. The
    # difference is that tier 1 (native in-process, Project Oxidation) CANNOT be
    # shared: it lives inside the calling process, so every m3 process would load
    # its own model copy / CUDA context (N × memory). Tier 2 is a single shared
    # server that all processes reuse — one model, one context. So auto-installing
    # the tier-1 wheel by default is both redundant (the shared server handles
    # embedding) and a memory-multiplier if processes actually ran it. Tier 1 is
    # OPT-IN via `--install-gpu-embedder` / the interactive prompt. Never
    # auto-compiles from source (allow_native_source_build).
    #
    # DON'T lose why tier 1 still exists: it is ~10-85x faster than the pure-Python
    # no-wheel fallback, which is immaterial per-call at these latencies BUT real
    # for a HIGH-VOLUME embed burst (bulk directory/file ingestion embedding
    # thousands of chunks, where per-call latency aggregates). A single dedicated
    # ingestion process is exactly the case where a self-contained in-process
    # tier-1 embedder beats round-tripping every chunk through the shared server —
    # so it stays a supported opt-in, not a removed feature.
    install_gpu_embedder: bool = False
    # Allow the multi-minute from-source Rust build as the last resort. Default
    # OFF: a no-matching-wheel host gets the graceful pure-Python fallback +
    # build-your-own guidance, never a surprise compile.
    allow_native_source_build: bool = False
    # Route all m3 processes to ONE shared embedder server instead of each loading
    # its own. Writes .embed_config.json + registers the self-healing embed-server
    # task. Default ON — this is the SHIPPED configuration, not an opt-in: one
    # shared embedder (GPU-accelerated where available, CPU-only otherwise — the
    # wheel picks the backend) is always preferable to N per-process embedders
    # (N CUDA contexts / N model copies in host RAM). Not surfaced as a selectable
    # option; --no-shared-embedder remains only as an escape hatch for debugging.
    use_shared_embedder: bool = True
    endpoint: Optional[str] = None
    cognitive_loop: bool = False
    # B15: GGUF path discovered + accepted in preflight. Used by the embedder
    # install step to pin tier-1 into the service config.toml so it persists.
    embed_gguf: Optional[str] = None
    decouple_roots: bool = False
    config_root: Optional[str] = None
    engine_root: Optional[str] = None
    # FIPS crypto tiers (see docs/FIPS_MODULE_BOUNDARY.md):
    #   fips_mode   -> M3_FIPS_MODE=1: route crypto through wolfCrypt, fail-closed
    #                  if absent, accept the open-source build (homelab).
    #   fips_strict -> M3_FIPS_STRICT=1: additionally require the CMVP-validated
    #                  wolfCrypt FIPS module (implies fips_mode).
    fips_mode: bool = False
    fips_strict: bool = False
    # Offer to build+install open-source wolfSSL during setup when FIPS is on,
    # so enabling FIPS doesn't leave the user with a fail-closed crash (the flag
    # requires the lib). Default on in interactive mode.
    install_wolfssl: bool = False
    # Replace governor-eligible cron/schtasks entries with the Adaptive
    # Background Workload Governor. Default on; gated by --no-governor-migration.
    migrate_to_governor: bool = True
    # Install the local web dashboard's deps (the [dashboard] pip extra:
    # fastapi + uvicorn) so `m3 dashboard` works. Default ON in interactive mode;
    # gated by --no-dashboard / --dashboard for headless runs. Backend-agnostic
    # (works on SQLite/PostgreSQL/…); loopback-only, no auth.
    install_dashboard: bool = True
    # Port the dashboard binds / its boot service registers (default 8088).
    dashboard_port: int = 8088


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
        # Tier-1 native wheel is OPT-IN now (shared tier-2 is the default, above):
        # install it only when explicitly requested via --install-gpu-embedder.
        # --no-native-wheel still forces it off (wins over the flag) for scripts
        # that pass both / want to be explicit.
        plan.install_gpu_embedder = (
            bool(args.install_gpu_embedder)
            and not bool(getattr(args, "no_native_wheel", False))
        )
        plan.allow_native_source_build = bool(getattr(args, "allow_native_source_build", False))
        plan.decouple_roots = bool(getattr(args, "decouple_roots", False))
        plan.config_root = getattr(args, "config_root", None)
        plan.engine_root = getattr(args, "engine_root", None)
        if plan.decouple_roots:
            if not plan.config_root:
                plan.config_root = os.path.expanduser("~/.m3/config")
            if not plan.engine_root:
                plan.engine_root = os.path.expanduser("~/.m3/engine")
        # Normalize to native separators. os.path.expanduser("~/.m3/config") on
        # Windows yields "<HOME>\.m3/config" (backslash home) mixed with
        # the literal forward slashes, which is NOT copy-paste-usable as a Windows
        # path. normpath makes it all-backslash on Windows, all-slash on POSIX.
        if plan.config_root:
            plan.config_root = os.path.normpath(plan.config_root)
        if plan.engine_root:
            plan.engine_root = os.path.normpath(plan.engine_root)
        # FIPS: --fips-strict implies --fips-mode. --install-wolfssl opts into
        # building the open-source lib unattended.
        plan.fips_strict = bool(getattr(args, "fips_strict", False))
        plan.fips_mode = plan.fips_strict or bool(getattr(args, "fips_mode", False))
        plan.install_wolfssl = bool(getattr(args, "install_wolfssl", False))
        # Default ON; --no-governor-migration sets args.no_governor_migration=True.
        plan.migrate_to_governor = not bool(getattr(args, "no_governor_migration", False))
        # Shared-embedder mode is the shipped default (one shared embedder for the
        # whole fleet, GPU or CPU); --no-shared-embedder is a debug escape hatch.
        # Applies on a CPU-only host too, so it is NOT gated on install_gpu_embedder.
        plan.use_shared_embedder = not bool(getattr(args, "no_shared_embedder", False))
        # Dashboard: default ON, headless-overridable. --no-dashboard opts out;
        # --dashboard forces it (both default to unset → the True default holds).
        if bool(getattr(args, "no_dashboard", False)):
            plan.install_dashboard = False
        elif bool(getattr(args, "dashboard", False)):
            plan.install_dashboard = True
        if getattr(args, "dashboard_port", None):
            plan.dashboard_port = int(args.dashboard_port)
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
    print(f"    {'[x]' if detected.cursor      else '[ ]'} Cursor               (~/.cursor/mcp.json)")
    print(f"    {'[x]' if detected.cline       else '[ ]'} Cline                (VS Code extension MCP settings)")
    print()

    if detected.claude:
        plan.targets.claude = _ask_yes_no("  Wire m3 into Claude Code?", default=True)
    if detected.gemini:
        plan.targets.gemini = _ask_yes_no("  Wire m3 into Gemini CLI?", default=True)
    if detected.antigravity:
        plan.targets.antigravity = _ask_yes_no("  Wire m3 into Antigravity CLI/Desktop?", default=True)
    if detected.opencode:
        plan.targets.opencode = _ask_yes_no("  Wire m3 into OpenCode?", default=True)
    if detected.cursor:
        plan.targets.cursor = _ask_yes_no("  Wire m3 into Cursor?", default=True)
    if detected.cline:
        plan.targets.cline = _ask_yes_no("  Wire m3 into Cline?", default=True)
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

    # Embedder prompt — only on a FRESH install. On an UPGRADE (the native wheel
    # is already installed + usable) the decision was already made; re-asking is
    # noise. Detect via active_embedder_tier()["native"] and keep the embedder
    # (install step refreshes the wheel) without prompting. The LLM-endpoint
    # question below is NOT skipped — endpoints legitimately change between runs.
    _embedder_already_native = False
    try:
        from m3_memory.rust_core_install import active_embedder_tier
        _embedder_already_native = bool(active_embedder_tier().get("native"))
    except Exception:  # noqa: BLE001 — best-effort; fall through to the prompt
        _embedder_already_native = False

    if _embedder_already_native:
        # Upgrade of a host that already has the tier-1 native wheel: keep it
        # (don't rip out a working embedder), but this is no longer what a FRESH
        # install adds by default — the shared tier-2 server is.
        plan.install_gpu_embedder = True  # keep it; install step refreshes the wheel
        _ok("  Project Oxidation native embedder already installed — keeping it "
            "(skipping the embedder prompt; this is an upgrade).")
    else:
        print()
        print("  Embedder — the default is the SHARED server (tier 2, on :8082);")
        print("  the tier-1 native in-process wheel (Project Oxidation) is OPTIONAL:")
        print("    The shared server is installed either way and is what every m3")
        print("    process uses. Speed is not the reason to choose between them —")
        print("    an embed call is already low-µs, so tier 1 vs tier 2 latency is")
        print("    typically not noticeable. The difference is SHARING: tier 1 runs")
        print("    IN-PROCESS and can't be shared, so each m3 process would load")
        print("    its own model/GPU context. The shared server needs just one.")
        print("    Only install tier 1 if you specifically want a self-contained,")
        print("    no-server in-process embedder. Installing it is a SAFE attempt:")
        print("    if no prebuilt wheel matches, m3 stays fully functional (we")
        print("    never auto-compile from source) and prints how to build one.")
        plan.install_gpu_embedder = _ask_yes_no(
            "  Also install the tier-1 native in-process wheel? (not needed for "
            "the shared server)",
            default=False,
        )
        if plan.install_gpu_embedder:
            # Default OFF — never surprise the user with a multi-minute Rust build.
            plan.allow_native_source_build = _ask_yes_no(
                "    If no prebuilt wheel matches, build it from source? "
                "(needs Rust+cmake+C++; slow)",
                default=False,
            )
        # Shared-embedder mode is the SHIPPED DEFAULT, not a user choice: ONE
        # shared embedder server (GPU-accelerated where available, CPU-only
        # otherwise) that all m3 processes defer to, plus a self-healing task
        # that keeps it up. We deliberately do NOT prompt — a single shared
        # embedder is always preferable to N per-process copies, and leaving it
        # off is the failure mode that silently kills embedding fleet-wide.
        # --no-shared-embedder stays as a debug-only escape hatch (not surfaced).
        # Set outside the install_gpu_embedder branch: shared mode applies to a
        # CPU-only host too, where there is no GPU embedder to install.
        if getattr(args, "no_shared_embedder", False):
            plan.use_shared_embedder = False
        else:
            plan.use_shared_embedder = True

    print()
    print("  Where to store data (recommended: separate folders):")
    print("    Keeps your settings (~/.m3/config) and databases (~/.m3/engine) in")
    print("    tidy, separate folders so they're easy to back up and secure.")
    print("    (If unsure, just say yes — it's the cleanest layout.)")
    plan.decouple_roots = _ask_yes_no(
        "  Use separate config + database folders (~/.m3/config, ~/.m3/engine)?",
        default=True,
    )
    if plan.decouple_roots:
        # normpath -> native separators (see the plan-init note); otherwise
        # Windows shows "<HOME>\.m3/config" mixed-separator, not copy-paste-usable.
        plan.config_root = os.path.normpath(os.path.expanduser("~/.m3/config"))
        plan.engine_root = os.path.normpath(os.path.expanduser("~/.m3/engine"))
        _say(f"    Config root: {plan.config_root}")
        _say(f"    Engine root: {plan.engine_root}")

    print()
    print("  FIPS 140-3 crypto mode (compliance feature — MOST USERS: leave OFF):")
    print("    WHY you'd turn this ON: only if a policy or contract requires")
    print("    FIPS-validated cryptography (e.g. US federal / regulated environments).")
    print("    If that doesn't apply to you, choose 'off' — the default crypto already")
    print("    uses FIPS-approved algorithms; 'off' is not 'insecure'.")
    print()
    print("    off    = default crypto. No extra setup. Recommended for everyone else.")
    print("    mode   = hardened wolfCrypt (M3_FIPS_MODE). Needs the open-source wolfSSL")
    print("             library BUILT FROM SOURCE on this machine — so you MUST have a C")
    print("             build toolchain installed (a C compiler + CMake; on Windows,")
    print("             Visual Studio Build Tools). If wolfSSL is absent at startup, M3")
    print("             fails closed (won't start). The wizard can build it for you below.")
    print("    strict = mode + REQUIRE the CMVP-validated wolfCrypt FIPS module")
    print("             (M3_FIPS_STRICT). That module is COMMERCIAL (paid wolfSSL FIPS")
    print("             license); the open-source build the wizard makes will NOT satisfy")
    print("             strict — it's for testing the plumbing until you swap in the")
    print("             validated module. Choose this only if you specifically need CMVP.")
    fips_choice = _ask_choice(
        "  FIPS mode?", choices=["off", "mode", "strict"], default="off",
    )
    plan.fips_mode = fips_choice in ("mode", "strict")
    plan.fips_strict = fips_choice == "strict"
    if plan.fips_mode:
        # Offer to build+install open-source wolfSSL now so FIPS actually works.
        # (STRICT needs the validated build, which the user obtains commercially;
        # building the open-source one still lets them validate the plumbing.)
        if plan.fips_strict:
            _warn("  strict mode requires the CMVP-validated wolfSSL FIPS module")
            _warn("  (commercial). The build below produces the OPEN-SOURCE build,")
            _warn("  which STRICT will refuse — use it to test, then swap in the")
            _warn("  validated module. See docs/FIPS_MODULE_BOUNDARY.md.")
        plan.install_wolfssl = _ask_yes_no(
            "  Build + install open-source wolfSSL now (so FIPS mode works)?",
            default=True,
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

    # Offer the local web dashboard. Default YES — it's a lightweight, useful
    # local control panel and installing its deps (fastapi+uvicorn) is cheap.
    print()
    print("  Web dashboard (optional):")
    print("    A local control panel — browse memory, explore the knowledge graph,")
    print("    watch the pipeline, see system health. Runs on http://127.0.0.1:8088.")
    print("    Starts automatically on boot as a WINDOWLESS background service (no")
    print("    console window, no flashes) and keeps running after you close your")
    print("    terminal. Control it with `m3 dashboard` / `--stop` / `--status`.")
    print("    Loopback-only (localhost); it is NOT authenticated, so don't expose it.")
    print("    Installs two small deps (fastapi + uvicorn).")
    plan.install_dashboard = _ask_yes_no(
        "  Install the web dashboard (auto-start on boot)?", default=True,
    )
    if plan.install_dashboard:
        # Guard the raw input(): a non-readable stdin (piped/headless run, or a
        # test with captured stdin) must degrade to the default port, not crash
        # or hang. EOFError/OSError → keep the default.
        try:
            ans = input(f"    Dashboard port [{plan.dashboard_port}]: ").strip()
        except (EOFError, OSError):
            ans = ""
        if ans:
            try:
                p = int(ans)
                if 1 <= p <= 65535:
                    plan.dashboard_port = p
                else:
                    print(f"    (port out of range; keeping {plan.dashboard_port})")
            except ValueError:
                print(f"    (not a number; keeping {plan.dashboard_port})")

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

def _run(cmd: list[str], *, check: bool = True, env: "dict | None" = None) -> subprocess.CompletedProcess:
    """Shell out, streaming output. Returns the completed process.

    `env`, when given, replaces the child's environment (used to strip
    PYTHONPATH so a stale repo payload can't shadow the installed package)."""
    return subprocess.run(cmd, check=check, env=env)


def _import_m3_halt():
    """Import the bin/m3_halt module (adds bin/ to path like other bin imports).
    Returns None if unavailable (e.g. payload not yet installed) so the caller
    degrades gracefully rather than crashing setup."""
    try:
        from m3_memory.installer import bin_dir
        bd = bin_dir()
        if bd and str(bd) not in sys.path:
            sys.path.insert(0, str(bd))
        import m3_halt  # type: ignore
        return m3_halt
    except Exception as e:  # noqa: BLE001 — coordination is best-effort
        _warn(f"  quiesce: m3_halt unavailable ({type(e).__name__}) — "
              "skipping cooperative DB-writer quiesce")
        return None


def _quiesce_db_writers(args: argparse.Namespace) -> bool:
    """Cooperatively quiesce autonomous m3 DB-writers before a DB-exclusive
    install/upgrade step, via the HALT_m3 protocol (docs/design/HALT_PROTOCOL.md).

    Sets HALT_m3, waits for the PID registry to empty, and — if a writer is still
    holding on after the timeout — asks the human (interactive) or honors the
    --force-quiesce/--force-kill-mcp opt-in (non-interactive), never killing
    silently. Returns True to proceed with install, False to abort. Always clears
    HALT_m3 before returning so writers are never left wedged.
    """
    halt = _import_m3_halt()
    if halt is None:
        return True  # can't coordinate; the mcp-memory.exe file-lock probe still guards Windows

    # Union of registered writers AND a cmdline scan — so an UPGRADE from an
    # older m3 (whose loop/embed/MCP predate the PID registry + HALT protocol and
    # thus don't register or poll) still detects the processes holding the DB.
    # Without the scan, list_live_processes() would be empty on that path and we'd
    # migrate right under the old writers.
    live = halt.list_all_db_writers()
    if not live:
        _ok("  no autonomous m3 DB-writers running (nothing to quiesce)")
        return True

    roles = ", ".join(f"{p.role}(pid {p.pid})" for p in live)
    _say(f"  quiescing {len(live)} m3 DB-writer(s) before install: {roles}")
    timeout = float(getattr(args, "quiesce_timeout", 30.0) or 30.0)
    # Raise the halt. It stays up through the exclusive install step and is
    # cleared by run_setup's finally (so writers stay paused for the whole
    # migration, then resume). On ANY early return below we clear it ourselves,
    # because there is no install step to protect if we abort.
    halt.set_halt(owner="setup_wizard", reason="install/upgrade DB-exclusive step")
    # Belt-and-suspenders: also clear on interpreter exit. The install path is not
    # a single straight-line function — a step may sys.exit(), re-exec (Windows
    # UTF-8 re-exec), or hand off to a subprocess before run_setup's finally runs,
    # leaking a raised HALT_m3. (Observed once on a FIPS/wolfSSL upgrade: the
    # installer PID exited without the finally firing; the self-void-on-dead-owner
    # guard reaped it so no writer was actually wedged, but a leaked halt shouldn't
    # depend on that net.) atexit fires on normal exit AND sys.exit, closing the
    # window. Idempotent with the finally + the abort-path clears.
    atexit.register(halt.clear_halt)
    result = halt.wait_for_quiesce(timeout=timeout)
    while not result.ok:
        stuck = ", ".join(f"{p.role}(pid {p.pid})" for p in result.stuck)
        _warn(f"  {len(result.stuck)} writer(s) still holding the DB after "
              f"{timeout:.0f}s: {stuck}")
        force = getattr(args, "force_quiesce", False) or getattr(args, "force_kill_mcp", False)
        gui = getattr(args, "gui_child", False)
        # GUI-triggered elevation is WINDOWS-ONLY: UAC is a GUI dialog that works
        # from a windowless subprocess. macOS/Linux elevation here is `sudo kill`,
        # which prompts for a password ON THE CONSOLE — a GUI child has no console,
        # so it would HANG. So only enable the elevated retry under --gui-child on
        # Windows; on a POSIX GUI child we stay unelevated and fall through to the
        # "run this elevated yourself" help (a native macOS GUI-auth path via
        # osascript is a separate future addition).
        gui_can_elevate = gui and sys.platform == "win32"
        if args.non_interactive:
            if force:
                # GUI runs are non-interactive-with-a-human: on Windows, allow the
                # UAC-elevated retry (the UAC dialog IS the consent) so an ELEVATED
                # stuck writer can be killed instead of failing "insufficient
                # privilege". A truly headless run (or a POSIX GUI child, no
                # console for sudo) stays unelevated.
                if not _kill_stuck_writers(result.stuck, allow_sudo=gui_can_elevate):
                    # A kill failed — almost always an ELEVATED writer an
                    # unprivileged installer can't stop. Do NOT report success or
                    # migrate under it; abort with the actionable fix.
                    _surface_elevated_kill_help(halt, result.stuck)
                    halt.clear_halt()
                    return False
                result = halt.wait_for_quiesce(timeout=5.0)
                continue
            _err("  refusing to migrate with live DB-writers. Re-run with "
                 "--force-quiesce (kills stuck writers) or stop them first.")
            halt.clear_halt()  # abort: no exclusive step to protect
            return False
        # Interactive: the human has context we don't (a task finishing).
        choice = _ask_choice(
            "  A writer hasn't paused — it may have a task finishing.",
            ["kill", "wait", "abort"], default="wait")
        if choice == "kill":
            # Interactive → allow a sudo retry (sudo prompts inline) for an
            # elevated writer, auto-elevating the cleanup during the install.
            if not _kill_stuck_writers(result.stuck, allow_sudo=True):
                _surface_elevated_kill_help(halt, result.stuck)
                halt.clear_halt()
                return False
            result = halt.wait_for_quiesce(timeout=5.0)
        elif choice == "abort":
            _warn("  install aborted by user (DB-writers still active)")
            halt.clear_halt()  # abort: no exclusive step to protect
            return False
        else:  # wait
            result = halt.wait_for_quiesce(timeout=timeout)
    _ok("  all m3 DB-writers quiesced (HALT_m3 held through install)")
    return True


def _kill_process_posix(pid: int) -> bool:
    """SIGTERM a PID on POSIX (Linux + macOS). Returns True on success, False on
    PermissionError (an elevated target an unprivileged installer can't signal)
    or if the process is already gone."""
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True  # already gone — the goal (not holding the DB) is met
    except PermissionError:
        return False  # elevated target — caller surfaces "re-run elevated"


def _surface_elevated_kill_help(halt, stuck) -> None:
    """Tell the user exactly how to clear a stale/elevated m3 writer the installer
    couldn't stop, then retry — the ready-to-run elevated command(s) for THIS OS
    (Windows / Linux / macOS), targeting the actual stuck PIDs."""
    _err("  could not stop an elevated m3 writer. Run the command(s) below from "
         "an ELEVATED shell (Windows: 'Run as administrator'; Linux/macOS: sudo), "
         "then re-run the installer:")
    try:
        cmds = halt.elevated_kill_commands([p.pid for p in stuck])
    except Exception:  # noqa: BLE001 — never let help-text generation abort
        cmds = []
    if not cmds:  # fallback if the helper is unavailable (older m3_halt)
        pids = " ".join(str(p.pid) for p in stuck)
        cmds = ([f"taskkill /F /T {pids}"] if sys.platform == "win32"
                else [f"sudo kill {pids}"])
    for c in cmds:
        print(f"      {c}")


def _sudo_kill_posix(pid: int) -> bool:
    """Try `sudo kill <pid>` on POSIX (Linux + macOS). Only for INTERACTIVE runs —
    sudo prompts for the password on the console, so it must never be attempted
    headless (it would hang or fail). Returns True if the process is stopped (or
    already gone). A missing sudo, a declined password, or a still-refused kill →
    False."""
    if not shutil.which("sudo"):
        return False
    try:
        out = subprocess.run(["sudo", "kill", str(pid)],
                             timeout=60, check=False)
        if out.returncode == 0:
            return True
        # Escalate to SIGKILL once if TERM didn't take.
        out = subprocess.run(["sudo", "kill", "-9", str(pid)],
                             timeout=60, check=False)
        return out.returncode == 0
    except Exception:  # noqa: BLE001 — sudo unavailable / user aborted / timeout
        return False


def _runas_kill_windows(pid: int) -> bool:
    """Elevate a taskkill via UAC on INTERACTIVE Windows using PowerShell
    `Start-Process -Verb RunAs`. Windows has no inline sudo — RunAs pops the UAC
    consent dialog and runs the kill in a short-lived elevated process, which we
    -Wait on and read the exit code of.

    Universal (built-in PowerShell, every Windows; no native-sudo/gsudo
    dependency). Returns True only if the elevated taskkill actually exited 0.
    A cancelled UAC prompt raises in Start-Process → caught → False. Must only be
    called interactively (UAC is a GUI prompt; pointless headless)."""
    # -PassThru + -Wait lets us read the elevated child's ExitCode. taskkill /F /T
    # /PID <pid> is the same command the unprivileged path used, just elevated.
    ps = (
        "$p = Start-Process -FilePath taskkill "
        f"-ArgumentList '/F','/T','/PID','{int(pid)}' "
        "-Verb RunAs -PassThru -Wait -WindowStyle Hidden; "
        "exit $p.ExitCode"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=120, check=False,
        )
        # taskkill exit 0 = killed; 128 = not found (already gone → goal met).
        return out.returncode in (0, 128)
    except Exception:  # noqa: BLE001 — UAC cancelled / powershell missing / timeout
        return False


def _runas_schedule_repair_windows(script: str) -> bool:
    """UAC-elevate `install_schedules.py --repair` on interactive Windows.

    Registering a boot-start (ONSTART) scheduled task — the cognitive loop, embed
    server, secret rotator — requires admin, so an unelevated `m3 setup` (whether
    a first install OR an upgrade) fails those with 'Access is denied'. Rather than
    only printing a re-run-elevated hint, offer to do it inline: PowerShell
    `Start-Process -Verb RunAs` pops the UAC consent dialog and runs --repair
    elevated, which is idempotent (adds only the missing boot tasks). Returns True
    iff the elevated repair exited 0; a cancelled UAC → False (caller falls back to
    the printed banner). Interactive-only (UAC is a GUI prompt)."""
    ps = (
        "$p = Start-Process -FilePath "
        f"'{sys.executable}' "
        f"-ArgumentList '\"{script}\"','--repair' "
        "-Verb RunAs -PassThru -Wait; "
        "exit $p.ExitCode"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=300, check=False,
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001 — UAC cancelled / powershell missing / timeout
        return False


def _runas_delete_tasks_windows(task_names: list[str]) -> bool:
    """UAC-elevate `schtasks /Delete` for governor-migrated tasks on interactive
    Windows. Deleting a scheduled task needs admin, so an unelevated `m3 setup`
    can't remove the legacy governor-eligible tasks and falls back to printing
    the commands. This offers to do it inline via `Start-Process -Verb RunAs`.
    Returns True iff every delete exited 0. Interactive-only (UAC is a GUI
    prompt)."""
    # Build one elevated command that deletes each task; /F force, ignore a
    # not-found (already gone) as success.
    deletes = "; ".join(
        f"schtasks /Delete /TN '{n}' /F" for n in task_names
    )
    ps = (
        "$p = Start-Process -FilePath 'powershell' "
        f"-ArgumentList '-NoProfile','-Command',\"{deletes}\" "
        "-Verb RunAs -PassThru -Wait; exit $p.ExitCode"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            timeout=120, check=False,
        )
        return out.returncode == 0
    except Exception:  # noqa: BLE001 — UAC cancelled / powershell missing / timeout
        return False


def _offer_elevated_task_delete(task_names: list[str], *, non_interactive: bool,
                                gui: bool = False) -> bool:
    """On Windows with a HUMAN PRESENT, elevate deleting the governor-eligible
    scheduled tasks that an unelevated remove was denied — the UAC dialog itself
    is the user's consent. Returns True if the elevated delete succeeded.

    Human-present = a terminal interactive run (ask yes/no first) OR a GUI run
    (`gui=True`: prompts are pre-answered, but someone is watching the GUI, so we
    still fire the UAC prompt rather than skip it). A plain headless
    --non-interactive run (no GUI) skips — nobody could consent to the dialog."""
    if sys.platform != "win32" or not task_names:
        return False
    if non_interactive and not gui:
        return False  # truly headless — no one to approve UAC
    if not gui:
        # Terminal interactive: ask before popping UAC.
        if not _ask_yes_no(
            "  Remove them now? (opens a Windows admin prompt)", default=True,
        ):
            return False
    _say("  Requesting administrator access (approve the Windows UAC dialog)...")
    if _runas_delete_tasks_windows(task_names):
        _ok("  legacy scheduled task(s) removed (elevated).")
        return True
    _warn("  elevated removal was cancelled or failed — see the commands above "
          "to run them yourself from an admin shell.")
    return False


def _offer_elevated_schedule_repair(script: str, *, non_interactive: bool) -> bool:
    """On interactive Windows, OFFER to UAC-elevate the boot-task registration a
    prior unelevated attempt was denied. Applies to first install AND upgrade —
    every `m3 setup` re-runs schedule registration. Returns True if the elevated
    repair succeeded (boot tasks now registered), False otherwise (caller keeps
    the printed banner as the fallback). No-op off interactive Windows."""
    if sys.platform != "win32" or non_interactive:
        return False
    if not _ask_yes_no(
        "  Register the boot-start services now? (opens a Windows admin prompt)",
        default=True,
    ):
        return False
    _say("  Requesting administrator access (approve the Windows UAC dialog)...")
    if _runas_schedule_repair_windows(script):
        _ok("  boot-start services registered (elevated).")
        return True
    _warn("  elevated registration was cancelled or failed — see the command above "
          "to run it yourself from an admin shell.")
    return False


def _kill_stuck_writers(stuck, *, allow_sudo: bool = False) -> bool:
    """Kill every stuck writer; return True only if ALL were stopped (or already
    gone). A False means at least one kill was refused — cross-platform, almost
    always an ELEVATED process an unprivileged installer can't stop — so the
    caller must NOT report success or migrate under it. Works on Windows / Linux /
    macOS (the POSIX path covers both via os.kill).

    ``allow_sudo`` (INTERACTIVE runs only): if an unprivileged kill is refused,
    retry once with elevation, auto-clearing the stale writer during the install
    instead of making the user copy a command and re-run. POSIX uses ``sudo kill``
    (inline password prompt); Windows uses PowerShell ``Start-Process -Verb RunAs``
    (UAC consent dialog) — Windows has no inline sudo. Never set headless (both
    prompt the user; sudo would hang, UAC is pointless with no one to consent)."""
    is_win = sys.platform == "win32"
    killer = _kill_process_windows if is_win else _kill_process_posix
    elevate = _runas_kill_windows if is_win else _sudo_kill_posix
    how = "UAC" if is_win else "sudo"
    all_ok = True
    for p in stuck:
        if killer(p.pid):
            _ok(f"    stopped {p.role} (pid {p.pid})")
            continue
        # Unprivileged kill refused (typically an elevated target). On an
        # interactive run, retry with elevation before giving up.
        if allow_sudo and elevate(p.pid):
            _ok(f"    stopped {p.role} (pid {p.pid}) via {how}")
            continue
        all_ok = False
        _warn(f"    could NOT stop {p.role} (pid {p.pid}) — likely elevated / "
              "owned by another user")
    return all_ok


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

    # ── Probe 2.5: cooperatively quiesce autonomous DB-writers (all OSes) ──
    # The mcp-memory.exe probe above guards the Windows *file-lock*. This guards
    # the cross-platform *DB-open* hazard: the cognitive loop / embed / MCP hold
    # WAL-mode DBs open, and migrating under them risks a torn WAL. We raise
    # HALT_m3 and wait for them to pause+release (docs/design/HALT_PROTOCOL.md).
    # A False return is fatal (mirrors the mcp-memory lock gate).
    if not _quiesce_db_writers(args):
        ok = False
        return ok  # abort now; HALT already cleared by the helper on abort

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
        _say(f"  discovered BGE-M3 GGUF: {discovered}")
        _say("  wiring it into the SHARED embedder config so the single :8082 "
             "server loads it (one CUDA context, ~10-85x faster than HTTP tiers)")
        if args.non_interactive or _ask_yes_no(
            "  Use this GGUF for the shared embedder?", default=True
        ):
            # Seed the shared config (NOT an env var). An env var would force
            # every MCP-server process to open its OWN CUDA context — a hang risk
            # (§3 headless knob -> config file). The shared :8082 server owns the
            # one context; clients defer to it. Idempotent.
            try:
                from m3_memory.embedder_admin import seed_shared_config
                _cfg_path, _wrote = seed_shared_config(gguf_path=discovered)
                plan.embed_gguf = discovered
                _ok(f"  shared embedder config {'seeded' if _wrote else 'already set'}: {_cfg_path}")
                _say("  (start/keep the :8082 server via `m3 embedder install`; "
                     "clients defer to it automatically)")
            except Exception as _e:  # noqa: BLE001 — best-effort, don't abort setup
                _warn(f"  could not seed shared embedder config: {_e}")
    else:
        _say("  no BGE-M3 GGUF auto-discovered; tier-2 (:8082) will serve all embeds")
        _say("  (drop a bge-m3 GGUF in the LM Studio models dir + run "
             "`m3 doctor --fix` later to wire tier-1; see EMBEDDER_ARCHITECTURE.md)")

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
    """Force-kill a process by PID via taskkill. Returns True only if it actually
    terminated (or was already gone) — False when taskkill is REFUSED, which for
    an elevated process run from an unprivileged shell prints 'Access is denied'
    and exits non-zero. The old version ignored the exit code and always returned
    True, silently reporting success on a failed elevated kill."""
    try:
        out = subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if out.returncode == 0:
            return True
        # 128 = process not found (already gone → goal met). Any other non-zero
        # (5/'Access is denied' on an elevated target) is a real failure.
        combined = (out.stdout + out.stderr).lower()
        if "not found" in combined or "no running instance" in combined:
            return True
        return False
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


# Shell-rc / MCP-env persistence backends (never monkeypatched; confirmed via
# grep) live in wizard/persist.py, re-imported here so setup_wizard.<name>
# keeps resolving for tests/importers. The top-level _persist_embed_gguf /
# _persist_env_var wrappers below (which delegate to these) DO get patched by
# tests indirectly via _ask_yes_no/_run — they stay in this module along with
# every other patched-fn caller.
from .wizard.persist import (  # noqa: F401,E402 — re-exported for setup_wizard.<name> access
    _persist_embed_gguf_mcp,
    _persist_embed_gguf_shell,
    _persist_env_var_mcp,
    _persist_env_var_shell,
    _pick_unix_shell_rc,
)


def _persist_env_var(name: str, value: str, *, non_interactive: bool) -> None:
    """Generic env-var persistence — shell rc + memory MCP env block, mirroring
    _persist_embed_gguf for arbitrary name/value (e.g. failover opt-in vars).
    Best-effort across surfaces; warnings don't abort."""
    _persist_env_var_shell(name, value, non_interactive=non_interactive)
    _persist_env_var_mcp(name, value)


def _discover_bge_m3_gguf() -> str | None:
    """Discover a bge-m3 GGUF in the canonical model dirs (B5).

    Delegates to memory.embed.discover_bge_m3_gguf so the wizard's
    auto-discovery and the runtime tier-1 auto-detection share ONE bounded
    implementation (single source of truth — they must not drift). `bin/` is
    already on sys.path here (added at wizard entry); fall back to the inline
    cascade only if that import is somehow unavailable."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
        from memory.embed import discover_bge_m3_gguf as _discover
        return _discover()
    except Exception:  # noqa: BLE001 — keep setup resilient if bin/ isn't importable
        home = Path.home()
        candidate_dirs = [
            home / ".lmstudio" / "models",
            home / "Library" / "Application Support" / "LM Studio" / "models",
            home / ".cache" / "lm-studio" / "models",
            home / ".cache" / "m3" / "models",
            home / ".m3-memory" / "_assets" / "embedder",
            home / "models",
        ]
        for d in candidate_dirs:
            if not d.is_dir():
                continue
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
    from m3_memory.installer import find_bridge

    _say("Step 1/5: fetching m3-memory system payload (install-m3)")

    # If find_bridge() already resolves (packaged payload or dev checkout),
    # skip the fetch. The payload is already present.
    if find_bridge() is not None:
        _say("  payload already present (packaged or via sibling); skipping fetch")
        _ok("payload available")
        return True

    # Subprocess-time package-shadow guard. The wizard's own preflight checks
    # `import m3_memory` in THIS process, but the install-m3 CHILD can resolve
    # a different (stale) m3_memory if the repo payload is ahead of the pipx
    # venv on the child's sys.path (a PYTHONPATH / cwd-derived entry). Letting
    # the stale copy run install_m3() --force can rmtree the repo with old
    # logic (2026-06-08 incident). Two defenses:
    #   1. Strip PYTHONPATH for the child so the repo payload can't shadow the
    #      installed package.
    #   2. Probe the child's resolved m3_memory.__file__ and abort on mismatch.
    child_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        import m3_memory as _wiz_pkg
        wiz_path = Path(_wiz_pkg.__file__).resolve()
        probe = subprocess.run(
            [sys.executable, "-c", "import m3_memory,sys; print(m3_memory.__file__)"],
            capture_output=True, text=True, env=child_env, timeout=30,
        )
        child_path = Path(probe.stdout.strip()).resolve() if probe.stdout.strip() else None
        if child_path is not None and child_path != wiz_path:
            _err(
                "subprocess would load a STALE m3_memory package:\n"
                f"    wizard uses : {wiz_path}\n"
                f"    child uses  : {child_path}\n"
                "  Refusing to run install-m3 with the wrong copy (it could wipe "
                "the repo with stale logic). Clear PYTHONPATH / stale __pycache__ "
                "and re-run `m3 setup`."
            )
            return False
    except Exception as e:  # noqa: BLE001 — probe is best-effort; don't block install on it
        _warn(f"could not verify subprocess package resolution: {e} (continuing)")

    cmd = [sys.executable, "-m", "m3_memory.cli", "install-m3",
           "--non-interactive", "--force", "--capture-mode", plan.capture_mode]
    if plan.endpoint:
        cmd += ["--endpoint", plan.endpoint]
    if plan.cognitive_loop:
        cmd.append("--cognitive-loop")
    try:
        _run(cmd, env=child_env)
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
        # NOT a failure — this OPTIONAL always-on CPU embedder was skipped (its
        # GGUF/binary or admin rights aren't present). m3 embeds fine WITHOUT it:
        # the cascade uses the in-process Tier-1 (GPU/CPU) or an HTTP tier. Frame
        # it as "skipped, here's how to add it if you want it", not an error, so
        # the user doesn't think setup broke. (Any scary "Error:" line above came
        # from the sub-step and is superseded by this.)
        _say("Optional CPU embedder (:8082) — SKIPPED (not installed). This is fine:")
        _say("  m3 already embeds via the in-process Tier-1 / HTTP fallback tier;")
        _say("  setup did NOT fail. Add the always-on shared server later only if")
        _say(f"  you want it. (reason: {e})")
        print()
        print("  To add the always-on CPU embedder later, follow the steps for your OS:")
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


def _step_gpu_embedder(plan: "SetupPlan") -> bool:
    """Install the Project Oxidation native in-process embedder.

    Installs the matching prebuilt m3-core-rs wheel (PyPI, then GitHub
    Release). With NO GPU this is the CPU build — still the in-process
    EmbeddedEmbedder, NOT the HTTP fallback. A from-source build is attempted
    only when the user opted in (plan.allow_native_source_build); otherwise we
    pass --no-source-fallback so a no-matching-wheel host degrades gracefully
    to pure-Python instead of triggering a surprise multi-minute compile.
    Always non-fatal — m3 works either way.
    """
    _say("Step 3/5: installing Project Oxidation native in-process embedder")
    cmd = [sys.executable, "-m", "m3_memory.cli", "embedder", "install-gpu"]
    if not plan.allow_native_source_build:
        cmd.append("--no-source-fallback")
    try:
        _run(cmd)
        _ok("Project Oxidation native embedder installed (in-process hot path active)")
        return True
    except subprocess.CalledProcessError as e:
        # rc != 0 here means no prebuilt wheel matched AND source build was
        # disabled (or failed). Reassure: m3 is fully functional in pure-Python.
        _warn(f"native wheel not installed (exit {e.returncode}) — that's OK:")
        try:
            from m3_memory.rust_core_install import oxidation_fallback_note
            print(oxidation_fallback_note(indent="    "))
            print("    To build your own wheel, see docs/BUILD_WHEELS.md or run "
                  "`m3 embedder install-gpu` with a Rust toolchain installed.")
        except Exception:  # noqa: BLE001 — reassurance is best-effort
            print("    m3 stays fully functional via its pure-Python embed path.")
        return True  # non-fatal


def _step_shared_embedder(plan: "SetupPlan", *, non_interactive: bool = False) -> bool:
    """Enable shared-embedder mode (the shipped default): write .embed_config.json
    so every m3 process defers to ONE shared embedder server (GPU-accelerated
    where available, CPU-only otherwise), AND register the self-healing
    embed-server task so that server is always up. Non-fatal at each step."""
    print()
    print("[~] Enabling shared embedder (one shared server for all m3 processes)")
    try:
        from m3_memory.embedder_admin import seed_shared_config
        # seed_shared_config writes .embed_config.json idempotently — the single
        # source of truth shared with the installer/doctor, so all stay in lockstep.
        _path, _wrote = seed_shared_config(port=8082)
        print(f"    [OK] shared-mode config {'written' if _wrote else 'already set'}: {_path}")
    except Exception as e:  # noqa: BLE001 — non-fatal; user can run `m3 embedder shared` later
        print(f"    [!] could not write shared-mode config ({e}); run `m3 embedder shared` later.")
        return True

    # Register the self-healing embed-server task so the shared server is always
    # running (config alone points clients at a server nobody started — the exact
    # silent fleet-wide outage this mode must avoid). install_schedules is CLI-
    # only and prints its own elevation hint on Windows "Access is denied", so we
    # shell out and surface, but never fail setup, if it needs an admin shell.
    _register_embed_server_task(non_interactive=non_interactive)
    return True


def _register_embed_server_task(*, non_interactive: bool = False) -> None:
    """Ensure the shared :8082 server has a keep-alive, PREFERRING the Rust
    m3-embed-server OS service over the Python scheduled-task fallback.

    _step_cpu_sovereign_embedder (runs earlier) installs the Rust m3-embed-server
    as a systemd/launchd/Windows Service — cross-platform, OS-native restart.
    THAT is the preferred keep-alive. Only when the Rust binary is absent (a host
    without the oxidation wheel) do we register the Python-server scheduled task
    as a fallback. The two are mutually exclusive by design — both bind :8082, so
    we must NEVER wire both.

    Non-fatal at every step: on Windows the ONSTART task registration needs an
    elevated shell; install_schedules prints the exact re-run-elevated command,
    which we pass through."""
    try:
        from m3_memory import embedder_admin
        if embedder_admin._server_binary() is not None:
            print("    Keep-alive: the Rust m3-embed-server OS service (installed "
                  "above) keeps :8082 up — no scheduled task needed.")
            return
    except Exception:  # noqa: BLE001 — detection failure: fall through to the fallback
        pass

    # Rust binary absent. The scheduled-task fallback (bin/install_schedules.py,
    # schtasks) is WINDOWS-ONLY — there is no crontab/systemd/launchd unit for the
    # Python embed_server_inproc.py. So on Unix the only cross-boot keep-alive is
    # the Rust OS service; be honest and point there rather than shell out to a
    # Windows-only path that would silently do nothing (§1 3-OS, §3 never-silent).
    if sys.platform != "win32":
        print("    Rust m3-embed-server not present, and the Python embed-server has")
        print("    no launchd/systemd unit yet — so shared mode has no cross-boot")
        print("    keep-alive on this OS. To get one, install the sovereign embedder:")
        print("        m3 embedder install-gpu   # fetches the m3-embed-server binary")
        print("        m3 embedder install       # registers it as a systemd/launchd service")
        print("    Until then, shared mode works only while a server is started manually:")
        print("        python bin/embed_server_inproc.py --port 8082")
        return

    print("    Rust m3-embed-server not present — registering the Python embed-"
          "server keep-alive task as a fallback (Windows).")
    # Locate the payload's bin/install_schedules.py. In the dev checkout bin/ is
    # a sibling of the m3_memory package (parent.parent/bin, the same idiom the
    # GGUF-discovery step uses); a pipx install fetches the payload to the same
    # relative spot. Guard on existence and degrade to a manual hint otherwise.
    script = str(Path(__file__).resolve().parent.parent / "bin" / "install_schedules.py")
    if not os.path.exists(script):
        print("    [!] embed-server task not registered (install_schedules.py not found);")
        print("        run `python bin/install_schedules.py --add embed-server` from the payload.")
        return
    print("    Registering the self-healing embed-server task (keeps :8082 up)...")
    try:
        # Capture output so we can (a) show it AND (b) detect the Windows
        # 'Access is denied' boot-task case to offer inline UAC elevation.
        proc = subprocess.run(
            [sys.executable, script, "--add", "embed-server"],
            check=False, capture_output=True, text=True,
        )
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="")
        if proc.returncode != 0:
            denied = "access is denied" in (proc.stdout + proc.stderr).lower()
            # Windows-only: a denied ONSTART registration can be UAC-elevated inline
            # (Linux/macOS use USER-level systemd --user / LaunchAgents — no privilege
            # needed, so they never reach this denied branch). Offer it; on success we
            # skip the manual hint. Applies to install AND upgrade (both re-run this).
            if denied and _offer_elevated_schedule_repair(script, non_interactive=non_interactive):
                return  # registered elevated — done
            print("    [!] embed-server task not fully registered (see above). Shared mode")
            print("        still works once the server runs; re-run elevated to auto-start it:")
            print(f'            "{sys.executable}" "{script}" --repair   # from an admin shell')
    except Exception as e:  # noqa: BLE001 — never fail setup on the task step
        print(f"    [!] could not register the embed-server task ({e}); do it later with")
        print(f'        "{sys.executable}" "{script}" --add embed-server')


def _step_install_wolfssl(plan: "SetupPlan") -> bool:
    """Build + install the open-source wolfSSL so FIPS mode actually works.

    Returns True if a usable wolfSSL is present afterward (so the caller can
    safely enable M3_FIPS_MODE). Builds from official source via
    bin/install_wolfssl.py — license-clean, no binary shipped. Non-fatal: a
    build failure just means we DON'T enable FIPS (and say so), rather than
    leaving the user with a fail-closed crash.
    """
    _say("FIPS: building + installing open-source wolfSSL (from official source)")
    cmd = [sys.executable, "-m", "m3_memory.cli", "fips", "install-wolfssl"]
    try:
        _run(cmd)
        _ok("wolfSSL installed to ~/.m3/lib — FIPS mode can use it")
        return True
    except subprocess.CalledProcessError as e:
        _warn(f"wolfSSL build failed (exit {e.returncode}).")
        print("  Install a C toolchain (see docs/FIPS_MODULE_BOUNDARY.md §5), then:")
        print("    m3 fips install-wolfssl")
        print("    export M3_FIPS_MODE=1   # (and M3_FIPS_STRICT=1 for the validated module)")
        return False
    except FileNotFoundError:
        _warn("could not invoke the wolfSSL installer; run `m3 fips install-wolfssl` manually.")
        return False


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


def _opencode_entry_is_stale(entry: object) -> bool:
    """True if an existing OpenCode ``memory`` entry points at a dead path.

    OpenCode's schema is ``{"command": [interp, script, ...], ...}`` — a LIST,
    unlike the mcpServers schema — so any list element that looks like an absolute
    path to a missing file (a moved-install split-brain) means repoint. A bare
    console-script command like ``["m3"]`` has no path elements and is never stale.
    """
    if not isinstance(entry, dict):
        return True
    cmd = entry.get("command")
    parts = cmd if isinstance(cmd, list) else [cmd]
    for p in parts:
        if isinstance(p, str) and (("/" in p) or ("\\" in p) or p.endswith(".py")):
            if not Path(os.path.expandvars(os.path.expanduser(p))).exists():
                return True
    return False


def _opencode_config_paths() -> list[Path]:
    """Candidate opencode.json locations. On Windows OpenCode may live under
    %APPDATA% OR ~/.config (the XDG-style path some installs use), so heal both."""
    paths = [Path.home() / ".config" / "opencode" / "opencode.json"]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.insert(0, Path(appdata) / "opencode" / "opencode.json")
    return paths


def _wire_opencode() -> bool:
    """Register/repoint the `memory` MCP entry in opencode.json.

    Self-healing (mirrors _register_gemini_mcp): a present-but-STALE entry (dead
    interpreter/bridge path from a moved install) is REPOINTED to the canonical
    ``command: ["m3"]`` — the bare CLI form is relocation-proof, so a repointed
    entry never goes stale again. The old skip-if-present behavior silently left
    a dead entry in place across upgrades."""
    canonical = {"type": "local", "command": ["m3"], "enabled": True}
    healed_any = False
    for cfg_path in _opencode_config_paths():
        if not cfg_path.is_file():
            continue
        try:
            existing = json.loads(cfg_path.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            _warn(f"{cfg_path} is unreadable; skipping OpenCode wiring")
            continue
        mcp = existing.setdefault("mcp", {})
        cur = mcp.get("memory")
        if cur == canonical:
            _say(f"  · OpenCode already wired ({cfg_path})")
            healed_any = True
            continue
        if cur is not None and not _opencode_entry_is_stale(cur):
            _say(f"  · OpenCode already wired ({cfg_path})")
            healed_any = True
            continue
        # Missing or stale -> (re)write the canonical entry, backing up a rewrite.
        if cur is not None:
            bak = cfg_path.with_suffix(cfg_path.suffix + ".m3bak")
            try:
                bak.write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        mcp["memory"] = canonical
        existing.setdefault("$schema", "https://opencode.ai/config.json")
        cfg_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
        verb = "repointed" if cur is not None else "wrote"
        _say(f"  · {verb} OpenCode memory MCP in {cfg_path}")
        healed_any = True

    if not healed_any:
        # No existing config anywhere -> create the canonical one at the primary path.
        cfg_path = _opencode_config_paths()[0]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(
            json.dumps({"$schema": "https://opencode.ai/config.json",
                        "mcp": {"memory": canonical}}, indent=2) + "\n",
            encoding="utf-8",
        )
        _say(f"  · wrote OpenCode config to {cfg_path}")
    return True


def _wire_antigravity() -> bool:
    """Write the m3 MCP entry into ~/.gemini/antigravity-cli/settings.json."""
    from m3_memory.installer import _register_antigravity_mcp
    msg = _register_antigravity_mcp()
    if msg:
        _say(f"  · {msg.lstrip('[+=!]').strip()}")
    return True


def _wire_cursor() -> bool:
    """Write the m3 MCP entry into Cursor's ~/.cursor/mcp.json."""
    from m3_memory.installer import _register_cursor_mcp
    msg = _register_cursor_mcp()
    if msg:
        _say(f"  · {msg.lstrip('[+=!]').strip()}")
    return True


def _wire_cline() -> bool:
    """Write the m3 MCP entry into Cline's cline_mcp_settings.json (VS Code)."""
    from m3_memory.installer import _register_cline_mcp
    msg = _register_cline_mcp()
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


def _hermes_plugin_is_current(src: Path, dst: Path) -> bool:
    """True if the installed Hermes plugin already matches the vendored source.

    "Current" means every shipped plugin file exists in ``dst`` with byte-identical
    content. A missing or differing file (a partial copy, or an older plugin from a
    prior m3 version) is stale → the caller re-installs. Mirrors OpenCode's
    _opencode_entry_is_stale check: don't touch an up-to-date install, heal a stale
    one, rather than prompt.
    """
    for fname in _HERMES_PLUGIN_FILES:
        s, d = src / fname, dst / fname
        if not d.is_file():
            return False
        try:
            if s.read_bytes() != d.read_bytes():
                return False
        except OSError:
            return False
    return True


def _wire_hermes(*, non_interactive: bool = False) -> bool:
    """Copy the m3 memory-provider plugin into the user's hermes-agent checkout.

    Hermes Agent loads memory providers from `plugins/memory/<name>/`. We locate
    the vendored source (m3_memory/integrations/hermes/) and the user's hermes
    plugins dir, then copy the plugin files into `plugins/memory/m3/`.

    Self-healing (mirrors _wire_opencode): an up-to-date plugin is left untouched;
    a present-but-stale one (older m3 version, partial copy) is backed up to
    ``m3.m3bak`` and rewritten — so an UPGRADE actually refreshes the plugin files
    instead of skipping them. Runs unattended: no interactive overwrite prompt
    (which would raise EOFError under --non-interactive and abort setup).
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
    existed = dst.exists()
    if existed:
        # Already up to date -> leave it, exactly like OpenCode's "already wired".
        if _hermes_plugin_is_current(src, dst):
            _say(f"  · Hermes: m3 plugin already current ({dst})")
            return True
        # Present but stale -> back up the old plugin before overwriting so a bad
        # upgrade is recoverable (mirrors OpenCode's .m3bak on rewrite).
        bak = dst.with_name("m3.m3bak")
        try:
            if bak.exists():
                _shutil.rmtree(bak)
            _shutil.move(str(dst), str(bak))
        except OSError as e:
            _warn(f"  · Hermes: could not back up existing plugin ({e}) — skipping")
            return False
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for fname in _HERMES_PLUGIN_FILES:
            _shutil.copy2(src / fname, dst / fname)
    except OSError as e:
        _warn(f"  · Hermes: copy failed ({e}) — skipping")
        return False

    verb = "updated" if existed else "installed"
    _say(f"  · Hermes: m3 SOTA provider {verb} at {dst}"
         + (" (previous backed up to m3.m3bak)" if existed else ""))
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


def _step_install_dashboard(plan: "SetupPlan") -> bool:
    """Install the [dashboard] pip extra (fastapi + uvicorn) so `m3 dashboard` runs.

    Non-fatal by design: a failed dep install must never abort setup — the core
    MCP server needs neither dep, so we print how to finish it by hand and move
    on. Idempotent: pip is a no-op if the deps are already satisfied. Backend-
    agnostic — the dashboard itself works on any registered store backend.
    """
    if not plan.install_dashboard:
        return True
    # Already present? Then this is a no-op — skip the pip round-trip.
    have = True
    for mod in ("fastapi", "uvicorn"):
        try:
            __import__(mod)
        except ModuleNotFoundError:
            have = False
            break
    if have:
        print("  Web dashboard deps already present — `m3 dashboard` is ready.")
        # Register the boot task here too: the second `if have:` block below was
        # unreachable dead code, so on a fresh install where fastapi/uvicorn are
        # already present the dashboard auto-start task was never registered.
        # _register_dashboard_task is best-effort and never aborts setup.
        _register_dashboard_task(plan.dashboard_port)
        return True

    print("  Installing web dashboard deps (fastapi + uvicorn)...")
    # Install the extra against THIS interpreter (the one running setup), so the
    # deps land where `m3 dashboard` will import them. Match the payload's own
    # distribution name so the extra resolves regardless of how m3 was installed.
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "m3-memory[dashboard]"],
            check=False, capture_output=True, text=True,
        )
        if proc.returncode == 0:
            print("    [OK] web dashboard installed.")
            _register_dashboard_task(plan.dashboard_port)
            return True
        # Degrade to a clear manual hint; never fail setup on an optional extra.
        if proc.stderr:
            tail = proc.stderr.strip().splitlines()[-3:]
            for line in tail:
                print(f"      {line}")
        print("    [!] could not install the dashboard deps automatically. Finish with:")
        print('            pip install "m3-memory[dashboard]"')
    except Exception as e:  # noqa: BLE001 — optional step, never abort setup
        print(f"    [!] dashboard install skipped ({e}); add it later with:")
        print('            pip install "m3-memory[dashboard]"')
    return True


def _register_dashboard_task(port: int = 8088) -> None:
    """Register the boot-start dashboard task on ``port`` (windowless; 3-OS:
    schtasks on Windows, launchd/systemd on macOS/Linux via install_schedules).

    Best-effort: a task-registration failure never aborts setup — the dashboard
    still runs on demand via `m3 dashboard`. On Windows an ONSTART task may need
    an admin shell; we surface the manual command rather than fail.
    """
    script = str(Path(__file__).resolve().parent.parent / "bin" / "install_schedules.py")
    if not os.path.exists(script):
        print("    [!] boot task not registered (install_schedules.py not found);")
        print(f'        add it later with `python bin/install_schedules.py --add dashboard --port {port}`.')
        return
    print(f"    Registering the dashboard to auto-start on boot (windowless, :{port})...")
    try:
        proc = subprocess.run(
            [sys.executable, script, "--add", "dashboard", "--port", str(port)],
            check=False, capture_output=True, text=True,
        )
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.returncode == 0:
            print(f"    [OK] dashboard will start on boot → http://127.0.0.1:{port}")
            print("         (running now: `m3 dashboard`; stop: `m3 dashboard --stop`)")
        else:
            print("    [!] boot task not registered (see above). The dashboard still")
            print(f'        runs on demand: `m3 dashboard`. Retry: `python bin/install_schedules.py --add dashboard --port {port}`')
    except Exception as e:  # noqa: BLE001 — never fail setup on the task step
        print(f"    [!] could not register the boot task ({e}); the dashboard still")
        print("        runs on demand via `m3 dashboard`.")


def _step_wire_agents(plan: SetupPlan, *, non_interactive: bool = False) -> bool:
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
    if plan.targets.cursor:
        _wire_cursor()
    if plan.targets.cline:
        _wire_cline()
    if plan.targets.openclaw:
        _wire_openclaw_note()
    if plan.targets.hermes:
        _wire_hermes(non_interactive=non_interactive)
    return True


def _step_governor_migration(plan: SetupPlan, *, non_interactive: bool = False,
                             gui: bool = False) -> dict:
    """Replace governor-eligible scheduled tasks with the governor.

    Returns a dict the summary uses to surface results / privileged commands:
        {"removed": [...], "failed": [...], "privileged_cmds": [...],
         "not_migratable": [...]}
    All keys are always present (possibly empty). Never raises — a scheduler
    tool that isn't present just yields empty results.
    """
    result: dict = {"removed": [], "failed": [], "privileged_cmds": [], "not_migratable": []}
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
            _warn(f"  could not remove {name} (insufficient privilege?)")
        # On interactive Windows, OFFER inline UAC to delete them now — consistent
        # with how setup already elevates process-kills and boot-task registration,
        # instead of only printing commands the user must run in an admin shell.
        if _offer_elevated_task_delete(failed, non_interactive=non_interactive, gui=gui):
            # verify each is actually gone before claiming success
            still = [n for n in failed if n in gm.detect_scheduled_tasks().get("eligible", [])]
            result["removed"] = removed + [n for n in failed if n not in still]
            result["failed"] = still
            if still:
                result["privileged_cmds"] = gm.privileged_removal_commands(still)
        else:
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
# Pure rendering, not monkeypatched, calls no patched fn — lives in
# wizard/summary.py; re-imported here for setup_wizard.<name> access.
from .wizard.summary import _os_name_for_summary, _summary  # noqa: F401,E402


def _should_use_gui(args: argparse.Namespace) -> bool:
    """Decide whether to launch the graphical setup. Rules (in order):
      - never in --non-interactive (that IS the child the GUI spawns — prevents
        an infinite GUI->child->GUI loop) or when --terminal is forced;
      - --gui forces it (but gracefully falls back if no display);
      - otherwise OFFER it only on an interactive TTY with a usable display;
        default answer is the terminal wizard, so muscle memory / SSH / headless
        are never surprised by a window.
    """
    if getattr(args, "non_interactive", False) or getattr(args, "terminal", False):
        return False
    try:
        from m3_memory.setup_gui import gui_available
    except Exception:
        return False
    ok, reason = gui_available()
    if getattr(args, "gui", False):
        if not ok:
            _warn(f"--gui requested but unavailable ({reason}); using the terminal wizard.")
        return ok
    if not ok:
        return False  # no display: silently use terminal (don't nag)
    if not sys.stdin.isatty():
        return False
    return _ask_yes_no("  Configure with the graphical setup window?", default=False)


def run_setup(args: argparse.Namespace) -> int:
    """Top-level entry point invoked by `m3 setup`."""
    if _should_use_gui(args):
        from m3_memory.setup_gui import run_gui
        return run_gui()

    detected = _detect_agents()
    plan = _gather_plan(detected, args)

    print()
    _say("Plan:")
    targets = [n for n, v in {
        "Claude Code": plan.targets.claude,
        "Gemini CLI": plan.targets.gemini,
        "Antigravity CLI/Desktop": plan.targets.antigravity,
        "OpenCode": plan.targets.opencode,
        "Cursor": plan.targets.cursor,
        "Cline": plan.targets.cline,
        "OpenClaw": plan.targets.openclaw,
        "Hermes Agent": plan.targets.hermes,
    }.items() if v]
    print(f"  agents       : {', '.join(targets) if targets else '(none)'}")
    print(f"  capture mode : {plan.capture_mode}")
    print("  Embedder     : sovereign CPU (BGE-M3 on :8082) — always installed")
    if plan.install_gpu_embedder:
        src = "prebuilt; source-build if no match" if plan.allow_native_source_build \
            else "prebuilt only (pure-Python fallback if no match)"
        print(f"  Oxidation    : native wheel ({src})")
    else:
        print("  Oxidation    : skipped (pure-Python embed path)")
    if plan.endpoint:
        print(f"  LLM endpoint : {plan.endpoint}")
    if plan.cognitive_loop:
        print("  cognitive loop: enabled")
    print(f"  decouple roots: {'yes' if plan.decouple_roots else 'no'}")
    if plan.decouple_roots:
        print(f"    config root: {plan.config_root}")
        print(f"    engine root: {plan.engine_root}")
    if plan.fips_strict:
        print("  FIPS         : strict (M3_FIPS_STRICT — requires validated wolfCrypt)")
    elif plan.fips_mode:
        print("  FIPS         : mode (M3_FIPS_MODE — hardened wolfCrypt)")
    else:
        print("  FIPS         : off")
    if plan.fips_mode and plan.install_wolfssl:
        print("    wolfSSL    : build + install open-source build during setup")
    print()

    if not args.non_interactive and not _ask_yes_no("Proceed?", default=True):
        _warn("aborted by user — no changes made")
        return 1

    # Set decoupled paths in environment so child commands inherit them.
    if plan.decouple_roots:
        os.environ["M3_CONFIG_ROOT"] = plan.config_root
        os.environ["M3_ENGINE_ROOT"] = plan.engine_root
        os.makedirs(plan.config_root, exist_ok=True)
        os.makedirs(plan.engine_root, exist_ok=True)

    # FIPS: build wolfSSL FIRST (if requested), THEN set the FIPS env vars.
    # Order matters — setting M3_FIPS_MODE before wolfSSL exists would make every
    # subsequent step (which imports crypto_provider) fail closed and crash.
    fips_ready = True
    if plan.fips_mode and plan.install_wolfssl:
        fips_ready = _step_install_wolfssl(plan)
    if plan.fips_mode:
        if fips_ready:
            os.environ["M3_FIPS_MODE"] = "1"
            if plan.fips_strict:
                os.environ["M3_FIPS_STRICT"] = "1"
        else:
            _warn("Not setting M3_FIPS_MODE for this run — wolfSSL is not present,")
            _warn("so enabling it would fail closed. Install wolfSSL "
                  "(`m3 fips install-wolfssl`), then export the FIPS vars.")
            plan.fips_mode = plan.fips_strict = False

    # Execute. Step 0 (preflight) and Step 1 (install-m3) can hard-abort.
    # Preflight may raise HALT_m3 to quiesce DB-writers; that halt stays up
    # through the exclusive install/migrate steps and is cleared here in a
    # finally so paused writers ALWAYS resume — success, failure, or exception.
    if not _step_preflight(plan, args):
        _err("setup aborted by preflight")
        return 2  # preflight cleared its own HALT on any abort path
    try:
        if not _step_install_m3(plan):
            _err("setup aborted")
            return 2
        _step_cpu_sovereign_embedder()
        if plan.install_gpu_embedder:
            _step_gpu_embedder(plan)
        if plan.use_shared_embedder:
            _step_shared_embedder(plan, non_interactive=args.non_interactive)
        _step_wire_agents(plan, non_interactive=args.non_interactive)
        _step_install_dashboard(plan)
        governor_result = _step_governor_migration(
            plan, non_interactive=args.non_interactive, gui=getattr(args, "gui_child", False))
        _step_doctor()
        _summary(plan, governor_result)
        return 0
    finally:
        # Lower HALT_m3 (idempotent — a no-op if preflight never raised it) so
        # the cognitive loop / embed / MCP resume. Best-effort; never mask the
        # real return/exception with a cleanup error.
        try:
            _halt = _import_m3_halt()
            if _halt is not None:
                _halt.clear_halt()
        except Exception:  # noqa: BLE001
            pass


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add `m3 setup` flags to an argparse subparser."""
    parser.add_argument(
        "--non-interactive", action="store_true",
        help="Run unattended with flag-driven choices (used by install.sh/install.ps1).",
    )
    # Configuration-UX selector. Default: ask (TTY + display) else terminal.
    # The GUI is a thin front-end that re-invokes `m3 setup --non-interactive`,
    # so it never duplicates the engine. --terminal/--gui skip the prompt and
    # are scriptable; --gui is a no-op (falls back to terminal) when no display.
    parser.add_argument(
        "--gui", action="store_true",
        help="Use the graphical setup window (falls back to the terminal wizard "
             "if no display / tkinter is unavailable).",
    )
    parser.add_argument(
        "--terminal", action="store_true",
        help="Force the terminal wizard, skipping the graphical-setup prompt.",
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
        "--force-quiesce", action="store_true",
        help="In non-interactive mode, force-kill any autonomous m3 DB-writer "
             "(cognitive loop / embed / MCP) that hasn't paused within "
             "--quiesce-timeout, instead of aborting. Cross-platform superset of "
             "--force-kill-mcp for the HALT_m3 quiesce step.",
    )
    parser.add_argument(
        "--quiesce-timeout", type=float, default=30.0,
        help="Seconds to wait for autonomous m3 DB-writers to pause before "
             "install (HALT_m3 protocol). Default: 30.",
    )
    parser.add_argument(
        "--gui-child", action="store_true",
        help="Internal: setup was launched by the graphical front-end "
             "(setup_gui.py) as its non-interactive worker. Prompts are "
             "pre-answered via flags, but a HUMAN IS WATCHING the GUI — so Windows "
             "elevation steps (UAC) for killing a stuck process or deleting a "
             "scheduled task are still OFFERED/attempted rather than skipped. "
             "Without this, the GUI's non-interactive child would silently skip "
             "every UAC prompt (nothing to consent to on a headless run), leaving "
             "those steps 'insufficient privilege'. NOT the same as --gui (which "
             "LAUNCHES the graphical window).",
    )

    parser.add_argument(
        "--install-gpu-embedder", action="store_true",
        help="Force-install the Project Oxidation native in-process embedder "
             "(CPU/GPU autodetected). Now ON by default — this flag is kept for "
             "back-compat and is redundant unless paired with a config that "
             "disabled it.",
    )
    parser.add_argument(
        "--no-native-wheel", action="store_true",
        help="Do NOT attempt the Project Oxidation native wheel; run pure-Python "
             "only. m3 stays fully functional but embeds run on the slower HTTP "
             "fallback path (see docs/BUILD_WHEELS.md).",
    )
    parser.add_argument(
        "--allow-native-source-build", action="store_true",
        help="If no prebuilt native wheel matches this platform/Python, build it "
             "from source (needs Rust + cmake + a C++ compiler; multi-minute "
             "compile). Default: skip the source build and fall back to "
             "pure-Python.",
    )
    parser.add_argument(
        "--shared-embedder", action="store_true",
        help="Enable shared-embedder mode: all m3 processes defer to ONE shared "
             "GPU embedder server (one CUDA context, ~9-10 GB reclaimed). Writes "
             ".embed_config.json. Toggle later with `m3 embedder shared/unshared`.",
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
        help="Enable M3_FIPS_MODE=1 — route crypto through wolfCrypt (hardened, "
             "fail-closed; accepts the open-source wolfSSL build).",
    )
    parser.add_argument(
        "--fips-strict", action="store_true",
        help="Enable M3_FIPS_STRICT=1 (implies --fips-mode) — additionally require "
             "the CMVP-validated wolfCrypt FIPS module (commercial wolfSSL).",
    )
    parser.add_argument(
        "--install-wolfssl", action="store_true",
        help="When FIPS is enabled, build + install the open-source wolfSSL from "
             "source during setup so FIPS mode works (avoids a fail-closed crash).",
    )
    parser.add_argument(
        "--no-governor-migration", action="store_true",
        help="Do NOT replace governor-eligible cron/schtasks entries with the "
             "Adaptive Background Workload Governor. By default the wizard offers "
             "to remove legacy scheduled tasks the governor can take over.",
    )
    parser.add_argument(
        "--no-shared-embedder", action="store_true",
        help="Debug escape hatch: do NOT enable shared-embedder mode. Shared mode "
             "(one shared embedder server for all m3 processes, GPU or CPU, kept up "
             "by a self-healing task) is the shipped default; disabling it means "
             "each process loads its own embedder. Not recommended.",
    )
    parser.add_argument(
        "--dashboard", action="store_true",
        help="Install the local web dashboard's deps ([dashboard] extra) "
             "unattended. Interactive setup offers this by default; use this flag "
             "for headless runs to force it on.",
    )
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="Do NOT install the web dashboard deps. By default the wizard offers "
             "the dashboard (fastapi + uvicorn) with a yes default.",
    )
    parser.add_argument(
        "--dashboard-port", type=int, default=None, metavar="PORT",
        help="Port for the web dashboard + its boot service (default 8088).",
    )
    parser.set_defaults(func=run_setup)
