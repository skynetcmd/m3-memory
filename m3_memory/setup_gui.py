"""setup_gui.py — optional graphical front-end for `m3 setup`.

This is a THIN front-end, not a second installer. It collects the same answers
the terminal wizard's interactive prompts collect (see setup_wizard._gather_plan),
then runs the EXACT same engine by shelling out to:

    m3 setup --non-interactive <flags>

…and streams that subprocess's output into a log pane. All install logic — agent
wiring, FIPS ordering, root decoupling, embedder + governor steps, doctor verify —
lives in setup_wizard.run_setup and is reused verbatim. The GUI only builds the
flag set, so it can never drift from the engine (there is exactly one engine).

Design constraints (DESIGN_PHILOSOPHIES):
  - §1 local-first / cross-platform: stdlib tkinter only — no new dependency, works
    offline. Never required: the terminal wizard remains the default and the GUI is
    skipped cleanly when tkinter or a display is unavailable.
  - §3 never silent: if the GUI cannot start, say why and fall back to the terminal
    wizard rather than crash.
  - subprocess isolation: a GUI crash cannot corrupt a half-run install; the child
    `m3 setup --non-interactive` owns the actual work and writes its own output.
"""
from __future__ import annotations

import platform
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass


def gui_available() -> tuple[bool, str]:
    """Return (ok, reason). ok=True only if tkinter imports AND a display/root can
    be created. The reason is for a `never silent` fallback message."""
    try:
        import tkinter as tk  # noqa: F401
    except Exception as e:  # ImportError, or a broken Tk build
        return False, f"tkinter is not available ({type(e).__name__})"
    try:
        # Creating + destroying a root is the only reliable probe for a usable
        # display (headless Linux, no $DISPLAY, broken Tcl all fail here).
        root = tk.Tk()
        root.withdraw()
        root.destroy()
    except Exception as e:
        return False, f"no usable display for a GUI ({type(e).__name__})"
    return True, ""


@dataclass
class _Field:
    """One form control bound to a setup flag."""
    key: str          # internal id
    label: str        # shown to the user
    kind: str         # 'check' | 'entry' | 'choice'
    default: object   # default value
    flag: str         # the `m3 setup` flag this maps to
    flag_kind: str    # 'bool' (presence) | 'value' (flag + value) | 'choice'
    choices: tuple = ()  # for kind == 'choice'
    help: str = ""    # tooltip / inline help


def _m3_command() -> list[str]:
    """The argv prefix that re-invokes this CLI's `setup` in a child process.

    Prefer the installed `m3` console script if it's on PATH; otherwise fall back
    to `python -m m3_memory.cli` so the GUI works from a source checkout too."""
    import shutil

    exe = shutil.which("m3")
    if exe:
        return [exe, "setup"]
    return [sys.executable, "-m", "m3_memory.cli", "setup"]


def _build_flags(values: dict) -> list[str]:
    """Translate collected widget values into `m3 setup --non-interactive` flags.

    Mirrors the flag surface in setup_wizard.add_arguments; only emits a flag when
    it differs from the wizard's own default so the command stays minimal and the
    non-interactive plan-construction path (args.non_interactive branch in
    _gather_plan) interprets it identically to a hand-run install.ps1."""
    flags: list[str] = ["--non-interactive"]

    # Agents: comma-separated; omit to let the child wire all detected agents.
    agents = [a for a in ("claude", "gemini", "antigravity", "opencode", "openclaw")
              if values.get(f"agent_{a}")]
    if agents:
        flags += ["--agents", ",".join(agents)]

    capture = values.get("capture_mode")
    if capture and capture != "both":  # both is the wizard default
        flags += ["--capture-mode", capture]

    if values.get("no_native_wheel"):
        flags.append("--no-native-wheel")
    if values.get("allow_native_source_build"):
        flags.append("--allow-native-source-build")

    endpoint = (values.get("endpoint") or "").strip()
    if endpoint:
        flags += ["--endpoint", endpoint]

    if values.get("cognitive_loop"):
        flags.append("--cognitive-loop")

    if values.get("decouple_roots"):
        flags.append("--decouple-roots")
        cr = (values.get("config_root") or "").strip()
        er = (values.get("engine_root") or "").strip()
        if cr:
            flags += ["--config-root", cr]
        if er:
            flags += ["--engine-root", er]

    # FIPS: strict implies mode (the child enforces it); install-wolfssl only
    # meaningful when FIPS is on.
    if values.get("fips_strict"):
        flags.append("--fips-strict")
    elif values.get("fips_mode"):
        flags.append("--fips-mode")
    if (values.get("fips_mode") or values.get("fips_strict")) and values.get("install_wolfssl"):
        flags.append("--install-wolfssl")

    if values.get("no_governor_migration"):
        flags.append("--no-governor-migration")

    if values.get("clean_cache"):
        flags.append("--clean-cache")
    if values.get("force_kill_mcp"):
        flags.append("--force-kill-mcp")

    return flags


# Stable text markers `m3 setup` prints around the wolfSSL build step (see
# setup_wizard._step_install_wolfssl). The GUI watches the combined stream for
# these to (a) route a status line to the main window and (b) open/close the
# separate build window. Matching is substring-based to survive minor wording.
_WOLF_START = "building + installing open-source wolfSSL"
_WOLF_OK = "wolfSSL installed to ~/.m3/lib"
_WOLF_FAIL = "wolfSSL build failed"


# Help text for the consequential options, shown via a hover-able ⓘ icon.
# Keyed by the state id. Module-level so the copy is easy to review + testable.
_TOOLTIPS = {
    "capture_mode": "How much Claude-Code chat is captured to memory. 'both' "
                    "(Stop + PreCompact) is the most complete and recommended; "
                    "'none' disables chat capture.",
    "no_native_wheel": "Skip the fast in-process Rust embedder and use the "
                       "pure-Python path. m3 still works but embeds are slower. "
                       "Leave unchecked unless the native wheel won't install.",
    "allow_native_source_build": "If no prebuilt native wheel matches this "
                                 "platform/Python, compile it from source. Needs "
                                 "Rust + a C++ toolchain and takes several minutes.",
    "cognitive_loop": "Run a background worker that periodically consolidates and "
                      "enriches memory. Useful, but uses some CPU/LLM time when idle.",
    "decouple_roots": "Store config and databases in separate roots "
                      "(~/.m3/config, ~/.m3/engine) instead of one combined dir. "
                      "Recommended: lets you secure/back-up the engine independently.",
    "fips_mode": "Route crypto through hardened wolfCrypt (fail-closed, KAT-checked). "
                 "Requires the wolfSSL library present — tick 'build wolfSSL' below, "
                 "or FIPS is left disabled to avoid a fail-closed crash.",
    "fips_strict": "Additionally require the CMVP-validated wolfCrypt FIPS module "
                   "(commercial wolfSSL). Implies FIPS mode. Only enable if you "
                   "have the validated module.",
    "install_wolfssl": "Build + install the open-source wolfSSL from official "
                       "source during setup (license-clean, no binary shipped). "
                       "Auto-selected when FIPS is enabled, since FIPS needs it.",
    "force_kill_mcp": "Terminate a running mcp-memory.exe before installing. Needed "
                      "on Windows to overwrite a locked binary during an upgrade — "
                      "it will stop a live m3 MCP server when setup runs.",
    "no_governor_migration": "Keep legacy scheduled tasks as-is instead of letting "
                             "the Adaptive Background Workload Governor take them "
                             "over. Leave unchecked to use the governor (recommended).",
    "clean_cache": "Delete __pycache__ directories before installing. Harmless "
                   "housekeeping that avoids stale-bytecode surprises after upgrade.",
}


def _apply_platform_tooltips() -> None:
    """Rewrite the embedder tooltips to name THIS platform's real acceleration
    back-end and source-build prerequisites. The native embedder is a compiled
    wheel whose accelerator differs per OS (Metal on Apple Silicon, CUDA/Vulkan
    on Windows/Linux); saying the true thing for the running machine beats a
    vague generic (DESIGN_PHILOSOPHIES §3 never-silent, §5 effectiveness).
    Runs at import; a no-op on platforms without a specific note."""
    if sys.platform == "darwin":
        apple_silicon = platform.machine() == "arm64"
        accel = ("Metal on Apple Silicon (~10–50× faster embeds than the "
                 "pure-Python path)" if apple_silicon
                 else "CPU only — Intel Macs have no Metal wheel")
        _TOOLTIPS["no_native_wheel"] = (
            "Skip the fast in-process Rust embedder and use the pure-Python "
            f"path. On this Mac the native wheel runs on {accel}. Leave "
            "unchecked unless the native wheel won't install.")
        _TOOLTIPS["allow_native_source_build"] = (
            "If no prebuilt native wheel matches this macOS + Python, compile "
            "it from source. Needs the Xcode Command Line Tools "
            "(xcode-select --install) plus cmake; takes several minutes.")


_apply_platform_tooltips()

# Leading status tokens the doctor output uses, mapped to a color-tag name. The
# emoji forms come from --brief; the bracket forms from installer.doctor / the
# full output. Checked longest-first so "❌" isn't shadowed, etc.
_DOCTOR_STATUS_TOKENS = (
    ("✅", "ok"), ("[OK]", "ok"),
    ("⚠️", "warn"), ("⚠", "warn"), ("[WARN]", "warn"), ("NAG", "warn"),
    ("❌", "fail"), ("[FAIL]", "fail"), ("[ERROR]", "fail"), ("[X]", "fail"),
)


def _doctor_line_status(line: str) -> "tuple[str, str] | None":
    """If `line` begins with a status token, return (tag, cleaned_line) where
    tag is 'ok'/'warn'/'fail' and cleaned_line has the leading emoji/bracket
    token stripped (so the GUI can prepend its own colored ● bullet). Returns
    None for non-status lines. Pure/text — unit-testable without Tk."""
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    for token, tag in _DOCTOR_STATUS_TOKENS:
        if stripped.startswith(token):
            rest = stripped[len(token):].lstrip()
            return tag, indent + rest
    return None


def _render_doctor(text_widget, content: str) -> None:
    """Insert doctor `content` into a Tk text widget, prepending a colored ●
    bullet (via pre-configured 'ok'/'warn'/'fail' tags) to each status line.
    Non-status lines are inserted plain. The bullet is a plain char colored by
    a tag, because Tk renders emoji as flat black in a text widget."""
    for i, line in enumerate(content.splitlines()):
        if i:
            text_widget.insert("end", "\n")
        status = _doctor_line_status(line)
        if status is not None:
            tag, cleaned = status
            text_widget.insert("end", "● ", tag)
            text_widget.insert("end", cleaned)
        else:
            text_widget.insert("end", line)


def run_gui() -> int:
    """Show the setup window. Returns the child `m3 setup` exit code, or 1 if the
    user closed the window without running. Assumes gui_available() was checked."""
    import os
    import tkinter as tk
    from tkinter import scrolledtext, ttk

    # ── tooltip helpers: a borderless popup shown while hovering a ⓘ icon ───────
    def _attach_tip(widget, text: str) -> None:
        tip: dict[str, "tk.Toplevel | None"] = {"win": None}

        def _show(_e=None) -> None:
            if tip["win"] is not None or not text:
                return
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)  # no border/title
            tw.wm_geometry(f"+{widget.winfo_rootx() + 16}+{widget.winfo_rooty() + 20}")
            tk.Label(tw, text=text, justify="left", wraplength=360,
                     background="#ffffe0", relief="solid", borderwidth=1,
                     padx=6, pady=4).pack()
            tip["win"] = tw

        def _hide(_e=None) -> None:
            if tip["win"] is not None:
                tip["win"].destroy()
                tip["win"] = None

        widget.bind("<Enter>", _show)
        widget.bind("<Leave>", _hide)

    def _info_icon(parent, key: str) -> None:
        """Append a hover-able ⓘ icon carrying _TOOLTIPS[key] (no-op if no tip)."""
        text = _TOOLTIPS.get(key)
        if not text:
            return
        lbl = ttk.Label(parent, text=" ⓘ", foreground="#1a6fb5",
                        cursor="question_arrow")
        lbl.pack(side="left")
        _attach_tip(lbl, text)

    def _check_with_info(parent, label: str, key: str, variable) -> None:
        """A checkbutton + trailing ⓘ info icon, for one boolean option."""
        rowf = ttk.Frame(parent)
        rowf.pack(anchor="w", fill="x")
        ttk.Checkbutton(rowf, text=label, variable=variable).pack(side="left")
        _info_icon(rowf, key)

    # Detect agents so their checkboxes can be pre-ticked, exactly like the
    # terminal wizard pre-ticks detected agents.
    try:
        from m3_memory.setup_wizard import _detect_agents
        detected = _detect_agents()
    except Exception:
        detected = None

    def _det(name: str) -> bool:
        return bool(getattr(detected, name, False)) if detected else False

    root = tk.Tk()
    root.title("m3-memory — graphical setup")
    root.geometry("900x680")
    root.minsize(820, 560)

    # State holds every widget's tk variable, keyed by the same ids _build_flags reads.
    state: dict[str, tk.Variable] = {}
    exit_code = {"rc": 1}  # mutated by the worker; read after mainloop
    running = {"v": False}  # True while `m3 setup` is in flight (locks closes)

    # Lock the CONFIG window's [x] while setup runs: closing it mid-install would
    # destroy root and end the mainloop, killing the running child. Before the run
    # it closes normally (cancel, no install). The window is also withdrawn during
    # the run; this handler is the belt to that suspenders.
    def _root_close() -> None:
        if running["v"]:
            return  # no-op during the run
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", _root_close)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)

    ttk.Label(
        main,
        text="Configure m3-memory — the same questions the terminal wizard asks, "
             "with recommended defaults already selected. Adjust anything you like, "
             "then click “Accept and run setup” to apply (runs "
             "`m3 setup --non-interactive`).",
        wraplength=860, justify="left",
    ).pack(anchor="w", pady=(0, 8))

    # ── Action bar, pinned to the bottom of the CONFIG window ──────────────────
    # Packed before the form so it can never be pushed off-screen. The config
    # window holds ONLY the form + these buttons; live output goes to a separate
    # log window created when setup starts (so the config window is hidden and
    # un-closable during the run).
    btns = ttk.Frame(main)
    btns.pack(side="bottom", fill="x", pady=(8, 0))
    # default="active": on macOS Aqua this renders the pulsing blue default
    # button and makes Return trigger it (HIG). Harmless on other platforms.
    run_btn = ttk.Button(btns, text="Accept and run setup ▶", default="active")
    run_btn.pack(side="right")
    close_btn = ttk.Button(btns, text="Cancel", command=root.destroy)
    close_btn.pack(side="right", padx=(0, 8))

    # ── Two-column form ───────────────────────────────────────────────────────
    form = ttk.Frame(main)
    form.pack(side="top", fill="both", expand=True)
    left = ttk.Frame(form)
    right = ttk.Frame(form)
    left.pack(side="left", fill="both", expand=True, padx=(0, 6))
    right.pack(side="left", fill="both", expand=True, padx=(6, 0))

    # ── Agents (left) ─────────────────────────────────────────────────────────
    agents_box = ttk.LabelFrame(left, text="Agents to wire (detected are pre-checked)", padding=8)
    agents_box.pack(fill="x", pady=4)
    for name, label in (("claude", "Claude Code"), ("gemini", "Gemini CLI"),
                        ("antigravity", "Antigravity"), ("opencode", "OpenCode"),
                        ("openclaw", "OpenClaw (local proxy)")):
        var = tk.BooleanVar(value=_det(name))
        state[f"agent_{name}"] = var
        ttk.Checkbutton(agents_box, text=label, variable=var).pack(anchor="w")

    # ── Capture mode (left) ───────────────────────────────────────────────────
    cap_box = ttk.LabelFrame(left, text="Chatlog capture mode (Claude Code)", padding=8)
    cap_box.pack(fill="x", pady=4)
    cap_var = tk.StringVar(value="both")  # default: recommended, pre-selected
    state["capture_mode"] = cap_var
    for val, desc in (("both", "both (Stop + PreCompact) — recommended"),
                      ("stop", "stop only"), ("precompact", "precompact only"),
                      ("none", "none")):
        ttk.Radiobutton(cap_box, text=desc, value=val, variable=cap_var).pack(anchor="w")

    # ── Embedder (left) ───────────────────────────────────────────────────────
    emb_title = ("Embedder (native Metal wheel ON by default)"
                 if sys.platform == "darwin" and platform.machine() == "arm64"
                 else "Embedder (native wheel ON by default)")
    emb_box = ttk.LabelFrame(left, text=emb_title, padding=8)
    emb_box.pack(fill="x", pady=4)
    state["no_native_wheel"] = tk.BooleanVar(value=False)  # default: keep native wheel
    _check_with_info(emb_box, "Skip native wheel (pure-Python embed path; slower)",
                     "no_native_wheel", state["no_native_wheel"])
    state["allow_native_source_build"] = tk.BooleanVar(value=False)
    _check_with_info(emb_box, "Allow native source build if no prebuilt wheel matches",
                     "allow_native_source_build", state["allow_native_source_build"])

    # ── Misc (left) ── recommended defaults pre-selected ───────────────────────
    misc_box = ttk.LabelFrame(left, text="Other (recommended defaults pre-selected)", padding=8)
    misc_box.pack(fill="x", pady=4)
    ep_row = ttk.Frame(misc_box); ep_row.pack(fill="x", pady=2)
    ttk.Label(ep_row, text="LLM endpoint:", width=12).pack(side="left")
    state["endpoint"] = tk.StringVar(value="")
    ttk.Entry(ep_row, textvariable=state["endpoint"]).pack(side="left", fill="x", expand=True)
    state["cognitive_loop"] = tk.BooleanVar(value=True)  # default ON
    _check_with_info(misc_box, "Enable background cognitive loop",
                     "cognitive_loop", state["cognitive_loop"])
    state["no_governor_migration"] = tk.BooleanVar(value=False)  # default: DO migrate
    _check_with_info(misc_box, "Do NOT migrate legacy scheduled tasks to the governor",
                     "no_governor_migration", state["no_governor_migration"])
    if sys.platform == "win32":
        state["force_kill_mcp"] = tk.BooleanVar(value=True)  # default ON
        _check_with_info(misc_box, "Force-kill a running mcp-memory.exe before install",
                         "force_kill_mcp", state["force_kill_mcp"])
    state["clean_cache"] = tk.BooleanVar(value=True)  # default ON
    _check_with_info(misc_box, "Wipe __pycache__ before install",
                     "clean_cache", state["clean_cache"])

    # ── Decoupled roots (right) — default ON ──────────────────────────────────
    roots_box = ttk.LabelFrame(right, text="Decoupled roots (recommended)", padding=8)
    roots_box.pack(fill="x", pady=4)
    state["decouple_roots"] = tk.BooleanVar(value=True)  # default ON
    _check_with_info(roots_box, "Use decoupled config + engine roots (~/.m3/config, ~/.m3/engine)",
                     "decouple_roots", state["decouple_roots"])
    # Pre-fill the default roots so the user sees where things land (and can edit).
    _home = os.path.expanduser("~")
    cr_row = ttk.Frame(roots_box); cr_row.pack(fill="x", pady=2)
    ttk.Label(cr_row, text="config root:", width=12).pack(side="left")
    state["config_root"] = tk.StringVar(value=os.path.join(_home, ".m3", "config"))
    ttk.Entry(cr_row, textvariable=state["config_root"]).pack(side="left", fill="x", expand=True)
    er_row = ttk.Frame(roots_box); er_row.pack(fill="x", pady=2)
    ttk.Label(er_row, text="engine root:", width=12).pack(side="left")
    state["engine_root"] = tk.StringVar(value=os.path.join(_home, ".m3", "engine"))
    ttk.Entry(er_row, textvariable=state["engine_root"]).pack(side="left", fill="x", expand=True)

    # ── FIPS (right) ──────────────────────────────────────────────────────────
    fips_box = ttk.LabelFrame(right, text="FIPS / crypto (off by default)", padding=8)
    fips_box.pack(fill="x", pady=4)
    state["fips_mode"] = tk.BooleanVar(value=False)
    _check_with_info(fips_box, "Enable FIPS mode (hardened wolfCrypt, fail-closed)",
                     "fips_mode", state["fips_mode"])
    state["fips_strict"] = tk.BooleanVar(value=False)
    _check_with_info(fips_box, "Strict FIPS (requires CMVP-validated commercial wolfSSL)",
                     "fips_strict", state["fips_strict"])
    state["install_wolfssl"] = tk.BooleanVar(value=False)
    _check_with_info(fips_box, "Build + install open-source wolfSSL during setup "
                     "(needed for FIPS)", "install_wolfssl", state["install_wolfssl"])

    # FIPS can't work without wolfSSL present, so auto-tick the build whenever
    # FIPS (mode or strict) is enabled. The user can still untick it deliberately;
    # we only AUTO-ENABLE on a FIPS turn-on, never force it off.
    def _auto_select_wolfssl(*_a) -> None:
        if state["fips_mode"].get() or state["fips_strict"].get():
            state["install_wolfssl"].set(True)
    state["fips_mode"].trace_add("write", _auto_select_wolfssl)
    state["fips_strict"].trace_add("write", _auto_select_wolfssl)

    # Every item is (kind, payload): kind in {line, wolf_start, wolf_ok,
    # wolf_fail, __DONE__}; payload is a str except __DONE__ (int rc).
    out_q: "queue.Queue[tuple[str, object]]" = queue.Queue()

    # ── Phase-2 LOG window (created when setup starts; not the config window) ───
    # The config window holds only the form; once the user accepts, it's hidden
    # and this dedicated, NON-CLOSABLE-during-run window shows live output.
    log_win: "tk.Toplevel | None" = None
    log_text: "scrolledtext.ScrolledText | None" = None
    status_var = tk.StringVar(value="Ready.")

    def _append(text: str) -> None:
        if log_text is None:
            return
        log_text.configure(state="normal")
        log_text.insert("end", text)
        log_text.see("end")
        log_text.configure(state="disabled")

    def _collect_values() -> dict:
        return {k: v.get() for k, v in state.items()}

    # Observed facts for the end-of-run, GUI-built summary.
    # The one observed fact the GUI-built summary needs that isn't in `values`:
    # whether the wolfSSL build succeeded/failed (from the stream markers).
    wolfssl_result: "str | None" = None
    # True between the wolfSSL start and ok/fail markers: while set, the
    # installer's own stdout is routed to the build window, not the main log.
    wolf_active: bool = False

    # ── wolfSSL build sub-window (created lazily when the build starts) ─────────
    # Typed locals (not an object-dict) so the Tk widgets keep their static types.
    wolf_win: "tk.Toplevel | None" = None
    wolf_text: "scrolledtext.ScrolledText | None" = None
    wolf_wait_var = tk.StringVar(value="")  # persistent "waiting…" line (not scrolled)

    def _open_wolf_window() -> None:
        nonlocal wolf_win, wolf_text
        if wolf_win is not None:
            return
        w = tk.Toplevel(root)
        w.title("m3-memory — building wolfSSL")
        w.geometry("820x480")
        ttk.Label(w, text="Building the open-source wolfSSL crypto library from "
                  "source. This shows the install's progress; it closes itself "
                  "when the build ends. (Full compiler output, incl. warnings, "
                  "is saved to ~/.m3/logs/wolfssl-build.log.)",
                  wraplength=780, justify="left", padding=8).pack(anchor="w")
        txt = scrolledtext.ScrolledText(w, height=20, wrap="word", state="disabled")
        txt.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        # Persistent status line BELOW the scroll area — it never scrolls away,
        # so the "output appears in 10–15s" hint stays visible until real build
        # output arrives (then _wolf_append clears it). This was the fix for the
        # hint getting buried at the top of the scroll buffer.
        wait_lbl = ttk.Label(w, textvariable=wolf_wait_var, foreground="#1a6fb5",
                             font=("TkDefaultFont", 12, "bold"), padding=(10, 2, 10, 10))
        wait_lbl.pack(anchor="w")
        wolf_wait_var.set("⏳ Building wolfSSL — first output appears in ~10–15s, "
                          "full compile takes 1–2 min. This window closes itself "
                          "when done.")
        # Can't be closed during the build (it auto-closes via _close_wolf_window
        # when the build ends); prevents abandoning a build mid-flight.
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        wolf_win, wolf_text = w, txt

        # Raise the build window above the main log window so it's the one in
        # front (the bug: it could sit behind the main log). topmost briefly,
        # then release so it doesn't permanently float over everything.
        try:
            w.lift()
            w.attributes("-topmost", True)
            w.after(800, lambda: w.attributes("-topmost", False))
        except tk.TclError:
            pass
        # No file tail: the installer's own stdout (routed here via wolf_active
        # in _pump) is the clean narrative. The raw 8000-warning build log stays
        # on disk — re-streaming it would re-create the very flood we moved away.

    def _wolf_append(text: str) -> None:
        if wolf_text is None:
            return
        # Keep the persistent hint visible for the WHOLE build (it's a reassurance
        # line pinned below the scroll area, so it never scrolls away). It's only
        # cleared in _close_wolf_window when the build ends — clearing it on the
        # first status line would remove it during the very cloning/compile gap
        # where the user needs it most.
        wolf_text.configure(state="normal")
        wolf_text.insert("end", text)
        wolf_text.see("end")
        wolf_text.configure(state="disabled")

    def _close_wolf_window(banner: str) -> None:
        wolf_wait_var.set("")  # build finished — retire the "building…" hint
        _wolf_append("\n" + banner + "\n")
        if wolf_win is not None:
            # Leave the banner visible briefly, then auto-close.
            wolf_win.after(2500, wolf_win.destroy)

    def _worker(cmd: list[str]) -> None:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            stream = proc.stdout
            if stream is not None:
                for line in stream:
                    low = line.lower()
                    if _WOLF_START.lower() in low:
                        out_q.put(("wolf_start", line))
                    elif _WOLF_OK.lower() in low:
                        out_q.put(("wolf_ok", line))
                    elif _WOLF_FAIL.lower() in low:
                        out_q.put(("wolf_fail", line))
                    else:
                        out_q.put(("line", line))
            proc.wait()
            out_q.put(("__DONE__", proc.returncode))
        except Exception as e:  # never let the worker die silently (§3)
            out_q.put(("line", f"\n[gui] failed to run setup: {type(e).__name__}: {e}\n"))
            out_q.put(("__DONE__", 1))

    def _set_status(msg: str) -> None:
        status_var.set(msg)

    def _build_summary(rc: int, values: dict) -> str:
        agents = [lbl for key, lbl in (
            ("agent_claude", "Claude Code"), ("agent_gemini", "Gemini CLI"),
            ("agent_antigravity", "Antigravity"), ("agent_opencode", "OpenCode"),
            ("agent_openclaw", "OpenClaw")) if values.get(key)]
        lines = ["", "──────── Setup summary ────────"]
        # Agents can be a long list; print one per continuation line with a
        # hanging indent aligned under the value column so it doesn't wrap raggedly.
        label = "Agents wired : "
        indent = " " * len(label)
        if agents:
            lines.append(label + agents[0])
            for a in agents[1:]:
                lines.append(indent + a)
        else:
            lines.append(label + "(all detected)")
        lines.append("Capture mode : " + str(values.get("capture_mode", "both")))
        lines.append("Native wheel : " + ("skipped" if values.get("no_native_wheel") else "yes"))
        if values.get("decouple_roots"):
            lines.append("Roots        : decoupled (config/engine)")
        if values.get("fips_strict") or values.get("fips_mode"):
            wolf_state = wolfssl_result or "not built"
            mode = "strict" if values.get("fips_strict") else "mode"
            lines.append(f"FIPS         : {mode} — wolfSSL {wolf_state}")
        elif wolfssl_result:
            lines.append("wolfSSL      : " + wolfssl_result)
        lines.append("Result       : " + ("✅ success" if rc == 0 else f"❌ exit {rc}"))
        lines.append("───────────────────────────────")
        return "\n".join(lines) + "\n"

    def _run_doctor(trigger_btn) -> None:
        """Run `m3 doctor --brief` and show its stdout verbatim in a window.

        The engine's --brief distills the verdicts to stdout; the noisy
        llama.cpp model-load logs go to stderr. We show ONLY stdout (the clean
        summary), always archive full stderr to a temp file, and surface that
        file's path only if doctor exits non-zero. No GUI-side stderr parsing.
        A toggle re-runs plain `m3 doctor` for the full stdout detail.

        Runs on a worker thread; results return via a queue polled on the main
        thread (Tk is not thread-safe)."""
        trigger_btn.configure(state="disabled", text="Running m3 doctor…")
        dq: "queue.Queue[str]" = queue.Queue()
        m3 = _m3_command()[:-1]  # drop the trailing 'setup'

        def _work(full: bool) -> None:
            import tempfile
            try:
                # Brief is `m3 doctor`'s default now; --verbose gets full detail.
                argv = [*m3, "doctor"] + (["--verbose"] if full else [])
                # Verdicts are on stdout. stderr is ~800 lines of llama.cpp/ggml
                # model-load internals (create_tensor:, sched_reserve:, …) — NOT
                # errors. Scanning it for "real errors" is whack-a-mole and was
                # leaking that firehose into the window. The authoritative
                # pass/fail signal is the EXIT CODE, so:
                #   • ALWAYS save full stderr to a temp file (nothing lost),
                #   • rc == 0  -> prune the file, show only the clean stdout,
                #   • rc != 0  -> keep the file, note the failure + its path
                #                 (the user opens the file to see the detail).
                # No stderr content is ever dumped into the pretty window.
                proc = subprocess.run(
                    argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace", timeout=180,
                )
                out = proc.stdout or "(no output)"
                stderr = proc.stderr or ""
                tf = tempfile.NamedTemporaryFile(
                    prefix="m3-doctor-stderr-", suffix=".log", delete=False,
                    mode="w", encoding="utf-8", errors="replace")
                tf.write(stderr)
                tf.close()
                if proc.returncode == 0:
                    try:
                        os.unlink(tf.name)  # healthy — nothing to keep
                    except OSError:
                        pass
                else:
                    out += (f"\n\n❌ m3 doctor exited {proc.returncode}. "
                            f"Full diagnostic output (incl. model-load logs) "
                            f"saved to:\n{tf.name}")
                dq.put(out)
            except Exception as e:
                dq.put(f"[gui] could not run m3 doctor: {type(e).__name__}: {e}")

        def _poll() -> None:
            try:
                out = dq.get_nowait()
            except queue.Empty:
                root.after(150, _poll)
                return
            trigger_btn.configure(state="normal", text="Verify with m3 doctor")
            dw = tk.Toplevel(root)
            dw.title("m3 doctor — system health")
            dw.geometry("720x480")
            ttk.Label(dw, padding=10, text="m3 doctor — key results "
                      "(toggle below for full detail):").pack(anchor="w")
            t = scrolledtext.ScrolledText(dw, wrap="word", state="normal")
            t.pack(fill="both", expand=True, padx=10, pady=(0, 8))
            # Colored-dot tags. Tk renders EMOJI (✅⚠️❌) as flat black in a text
            # widget, so we don't rely on them for color — instead we prepend a
            # plain "●" and color JUST that char via a foreground tag, which Tk
            # honors regardless of the OS emoji font.
            t.tag_configure("ok", foreground="#1a9e46")     # green
            t.tag_configure("warn", foreground="#d98a00")   # amber
            t.tag_configure("fail", foreground="#cc2b2b")   # red
            _render_doctor(t, out)
            t.configure(state="disabled")

            showing_full = {"v": False}

            def _toggle() -> None:
                showing_full["v"] = not showing_full["v"]
                toggle_btn.configure(state="disabled", text="Running…")
                t.configure(state="normal"); t.delete("1.0", "end")
                t.insert("end", "Running m3 doctor…"); t.configure(state="disabled")

                def _re_poll() -> None:
                    try:
                        newout = dq.get_nowait()
                    except queue.Empty:
                        root.after(150, _re_poll); return
                    t.configure(state="normal"); t.delete("1.0", "end")
                    _render_doctor(t, newout); t.configure(state="disabled")
                    toggle_btn.configure(
                        state="normal",
                        text="Show key results" if showing_full["v"] else "Show full output")
                threading.Thread(target=_work, args=(showing_full["v"],), daemon=True).start()
                root.after(150, _re_poll)

            bar2 = ttk.Frame(dw, padding=(10, 0, 10, 10)); bar2.pack(fill="x")
            ttk.Button(bar2, text="Close", command=dw.destroy).pack(side="right")
            toggle_btn = ttk.Button(bar2, text="Show full output", command=_toggle)
            toggle_btn.pack(side="left")
            dw.lift()

        threading.Thread(target=_work, args=(False,), daemon=True).start()
        root.after(150, _poll)

    def _finish(rc: int) -> None:
        exit_code["rc"] = rc
        running["v"] = False  # install done — closes are allowed again
        summary = _build_summary(rc, _collect_values())
        _append(summary)  # keep it in the log window too, for "View log"
        # The install is over, so the log window may now be closed; hide it and
        # present the summary window. The config window (root) stays withdrawn
        # the whole time — the user never returns to it. We WITHDRAW the log
        # window (not destroy) so "View log" can reveal the full output again.
        if log_win is not None:
            log_win.protocol("WM_DELETE_WINDOW", log_win.withdraw)  # closable now
            log_win.withdraw()

        win = tk.Toplevel(root)
        win.title("m3-memory — setup complete")
        win.geometry("560x360")
        ok = (rc == 0)
        ttk.Label(
            win, padding=12,
            text=("✅ Setup completed successfully." if ok
                  else f"❌ Setup finished with errors (exit {rc})."),
        ).pack(anchor="w")
        # wrap="none" so the aligned summary columns don't re-wrap raggedly.
        body = scrolledtext.ScrolledText(win, height=12, wrap="none", state="normal")
        body.insert("end", summary.strip())
        body.configure(state="disabled")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))

        bar = ttk.Frame(win, padding=(12, 0, 12, 12))
        bar.pack(fill="x")

        def _done() -> None:
            win.destroy()
            root.destroy()  # ends mainloop -> run_gui returns exit_code

        def _view_log() -> None:
            # Reveal the log window (which holds the full raw output).
            if log_win is not None:
                log_win.deiconify()
                log_win.lift()

        ttk.Button(bar, text="Done", command=_done).pack(side="right")
        ttk.Button(bar, text="View log", command=_view_log).pack(side="right", padx=(0, 8))
        doctor_btn = ttk.Button(bar, text="Verify with m3 doctor")
        doctor_btn.pack(side="left")
        doctor_btn.configure(command=lambda: _run_doctor(doctor_btn))

        # [x] on the summary behaves like Done (don't strand a dangling mainloop).
        win.protocol("WM_DELETE_WINDOW", _done)
        win.lift()

    def _pump() -> None:
        nonlocal wolfssl_result, wolf_active
        try:
            while True:
                item = out_q.get_nowait()
                kind, payload = item  # every queued item is (kind, payload)
                if kind == "__DONE__":
                    _finish(int(payload) if isinstance(payload, int) else 1)
                    return  # worker done; stop pumping
                if kind == "wolf_start":
                    wolf_active = True
                    _set_status("wolfSSL DLL: building… (see build window)")
                    _append("wolfSSL DLL: building… (output in the build window)\n")
                    _open_wolf_window()
                    _wolf_append(str(payload))  # show the start line in the window too
                elif kind == "wolf_ok":
                    wolf_active = False
                    wolfssl_result = "success"
                    _set_status("wolfSSL DLL: ✅ success")
                    _append("wolfSSL DLL: ✅ build success\n")
                    _close_wolf_window("===== BUILD SUCCESS =====")
                elif kind == "wolf_fail":
                    wolf_active = False
                    wolfssl_result = "FAILED"
                    _set_status("wolfSSL DLL: ❌ failed")
                    _append("wolfSSL DLL: ❌ build FAILED (FIPS left disabled)\n")
                    _close_wolf_window("===== BUILD FAILED =====")
                else:
                    # While the wolfSSL phase is active, the installer's own
                    # stdout (cloning, configuring, git "Updating files…",
                    # "[install-wolfssl] building…") belongs in the build window —
                    # not the main log. This is the fix for that chatter leaking
                    # to the main window.
                    if wolf_active:
                        _wolf_append(str(payload))
                    else:
                        _append(str(payload))
        except queue.Empty:
            pass
        root.after(120, _pump)

    def _open_log_window() -> None:
        """Create the phase-2 log window and HIDE the config window. The log
        window cannot be closed while setup runs (WM_DELETE_WINDOW is a no-op),
        so the user can't abandon a running install; _finish re-enables closing."""
        nonlocal log_win, log_text
        root.withdraw()  # hide the config window for the whole run
        w = tk.Toplevel(root)
        w.title("m3-memory — running setup")
        w.geometry("820x520")
        ttk.Label(w, padding=(10, 10, 10, 4),
                  text="Installing… please wait. This window can't be closed until "
                       "setup finishes; the wolfSSL build (if any) opens its own window.",
                  wraplength=780, justify="left").pack(anchor="w")
        ttk.Label(w, textvariable=status_var, padding=(10, 0)).pack(anchor="w")
        txt = scrolledtext.ScrolledText(w, height=22, wrap="word", state="disabled")
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        # Block close during the run (no-op handler). _finish swaps this for a
        # real close once the install is done.
        w.protocol("WM_DELETE_WINDOW", lambda: None)
        log_win, log_text = w, txt

    def _on_run() -> None:
        values = _collect_values()
        cmd = _m3_command() + _build_flags(values)
        running["v"] = True  # lock window closes until _finish
        _open_log_window()
        _append("[gui] running: " + " ".join(cmd) + "\n\n")
        _set_status("Running setup…")
        threading.Thread(target=_worker, args=(cmd,), daemon=True).start()
        root.after(120, _pump)

    run_btn.configure(command=_on_run)

    # macOS/HIG keyboard affordances: Return/Enter triggers the default action,
    # Escape cancels. Guarded on running["v"] so a stray Return can't relaunch
    # setup mid-run (the config window is withdrawn then anyway — belt + braces).
    def _on_return(_e: "object" = None) -> None:
        if not running["v"]:
            _on_run()

    def _on_escape(_e: "object" = None) -> None:
        _root_close()

    root.bind("<Return>", _on_return)
    root.bind("<KP_Enter>", _on_return)
    root.bind("<Escape>", _on_escape)

    root.mainloop()
    return exit_code["rc"]
