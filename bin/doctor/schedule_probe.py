"""Dangling scheduled-task probe — do the installed background jobs still point
at a real interpreter and script?

m3 installs its background work as OS-native scheduled jobs whose definitions
bake an ABSOLUTE interpreter path (``…/.venv/…/python``) and script path:

  * **Windows** — ``schtasks`` tasks ``AgentOS_*`` (XML ``<Command>`` + ``<Arguments>``).
  * **macOS**   — a launchd agent ``~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist``
                  (``ProgramArguments[0]`` = interpreter, ``[1]`` = script), plus
                  ``install_unix_crontab`` entries.
  * **Linux**   — a ``systemd --user`` unit ``~/.config/systemd/user/m3-cognitive-loop.service``
                  (``ExecStart=<interp> <script> …``), plus crontab entries.

If the code install is deleted or moved (e.g. ``rm -rf ~/.local/pipx`` wipes the
venv, or the repo clone is relocated), those definitions stay registered but
point at a path that no longer resolves. The job fires on schedule and fails
silently: the background governor never resurrects, because self-heal restarts a
crashed loop but not a missing interpreter.

Nothing else surfaces this: ``installer._path_is_stale`` only covers MCP-config
paths, and ``governor_probe`` only flags *legacy* (pre-governor) schedules, not
*dangling* ones. This probe checks — on ALL THREE OSes (DESIGN §1) — that every
installed job's interpreter and script still exist on disk, and nags with the
one-command fix when they don't.

Report-only (DESIGN §3): always returns 0 and never crashes the doctor — a
dangling job is a recoverable state (re-register via ``m3 setup``), not a doctor
failure. Detection is per-OS backends behind a common ``find_dangling`` shape so
the check degrades gracefully rather than going blind on 2 of 3 platforms.
"""
from __future__ import annotations

import logging
import os
import plistlib
import posixpath
import re
import subprocess
import sys

# defusedxml hardens the parser against XXE / billion-laughs / quadratic-blowup.
# task_xml comes from `schtasks /query /xml` (external process output), which the
# security scanner treats as untrusted — so parse it defensively. Drop-in for
# xml.etree.ElementTree (same fromstring/find API); falls back to stdlib only if
# defusedxml is somehow unavailable, still safe because Python 3.x's expat has
# entity-expansion limits, but defusedxml is the belt-and-suspenders default.
try:
    import defusedxml.ElementTree as ET  # type: ignore
except ImportError:  # pragma: no cover — defusedxml is a declared dependency
    import xml.etree.ElementTree as ET  # noqa: S405  # nosec B405

logger = logging.getLogger("memory.doctor.schedule_probe")

# ── Windows: Task Scheduler ────────────────────────────────────────────────
_TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
_SCHTASKS_NOT_FOUND_RC = 1  # schtasks /Query rc when the named task doesn't exist

# ── Unix: fixed install destinations (mirror install_schedules.py) ─────────
_LAUNCHD_PLIST = "~/Library/LaunchAgents/com.m3memory.cognitiveloop.plist"
_SYSTEMD_UNIT = "~/.config/systemd/user/m3-cognitive-loop.service"


# A dangling record is a dict: {job, interpreter, script, missing:[...]}. `job`
# is a human label (task name / plist label / unit name / crontab line-N).


def _flag_missing(job: str, interpreter: str | None, script: str | None) -> dict | None:
    """Return a dangling record iff the interpreter or a declared script is
    missing on disk; else None. Shared by every backend so the 'what counts as
    dangling' rule lives in exactly one place."""
    missing: list[str] = []
    if not interpreter or not os.path.exists(interpreter):
        missing.append("interpreter")
    if script and not os.path.exists(script):  # only flag a script the job declares
        missing.append("script")
    if not missing:
        return None
    return {"job": job, "interpreter": interpreter, "script": script, "missing": missing}


# ── Windows backend ────────────────────────────────────────────────────────

def _expected_task_names(m3_memory_root: str) -> list[str]:
    """AgentOS_* task names this install registers — sourced from
    install_schedules.get_schedule_specs so the names stay single-sourced."""
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)
    import install_schedules as sched  # noqa: E402

    return [t["name"] for t in sched.get_schedule_specs(m3_memory_root)]


def _query_task_xml(name: str) -> str | None:
    """Task Scheduler XML for ``name``, or None if not installed. Raises on an
    unexpected schtasks failure (caught by the backend)."""
    proc = subprocess.run(
        ["schtasks", "/Query", "/TN", name, "/XML"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").upper()
        if proc.returncode == _SCHTASKS_NOT_FOUND_RC or "ERROR:" in stderr:
            return None  # not-installed is benign, not dangling
        raise RuntimeError(f"schtasks /Query {name} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def _parse_exec_paths(task_xml: str) -> tuple[str | None, str | None]:
    """(interpreter, script) from a task's <Exec>. <Command> may be quoted;
    the first <Arguments> token is the script (quotes stripped)."""
    # ET is defusedxml at runtime (see import above); the value is schtasks
    # /query output (trusted local OS), not untrusted network XML.
    root = ET.fromstring(task_xml)  # nosec B314
    exec_el = root.find(f".//{_TASK_NS}Actions/{_TASK_NS}Exec")
    if exec_el is None:
        return None, None
    cmd_el = exec_el.find(f"{_TASK_NS}Command")
    interpreter = cmd_el.text.strip().strip('"') if cmd_el is not None and cmd_el.text else None
    args_el = exec_el.find(f"{_TASK_NS}Arguments")
    script = None
    if args_el is not None and args_el.text and args_el.text.strip():
        script = args_el.text.strip().split(" ")[0].strip('"') or None
    return interpreter, script


def _dangling_windows(m3_memory_root: str) -> list[dict]:
    dangling: list[dict] = []
    for name in _expected_task_names(m3_memory_root):
        try:
            task_xml = _query_task_xml(name)
        except Exception as e:  # noqa: BLE001 — one bad task must not sink the probe
            dangling.append({"job": name, "interpreter": None, "script": None,
                             "missing": ["query-error"], "error": str(e)})
            continue
        if task_xml is None:
            continue
        interpreter, script = _parse_exec_paths(task_xml)
        rec = _flag_missing(name, interpreter, script)
        if rec:
            dangling.append(rec)
    return dangling


# ── macOS backend (launchd plist + crontab) ────────────────────────────────

def _dangling_darwin(m3_memory_root: str) -> list[dict]:
    dangling: list[dict] = []
    plist_path = os.path.expanduser(_LAUNCHD_PLIST)
    if os.path.exists(plist_path):
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
            args = data.get("ProgramArguments", [])
            interpreter = args[0] if len(args) >= 1 else None
            script = args[1] if len(args) >= 2 else None
            label = data.get("Label", "com.m3memory.cognitiveloop")
            rec = _flag_missing(f"launchd:{label}", interpreter, script)
            if rec:
                dangling.append(rec)
        except Exception as e:  # noqa: BLE001
            dangling.append({"job": f"launchd:{plist_path}", "interpreter": None,
                             "script": None, "missing": ["parse-error"], "error": str(e)})
    dangling.extend(_dangling_crontab())
    return dangling


# ── Linux backend (systemd --user unit + crontab) ──────────────────────────

_EXECSTART_RE = re.compile(r"^\s*ExecStart\s*=\s*(.+?)\s*$", re.MULTILINE)


def _parse_execstart(unit_text: str) -> tuple[str | None, str | None]:
    """(interpreter, script) from a systemd ExecStart= line. systemd allows a
    leading '-'/'@'/'+' prefix on the command; strip it before splitting."""
    m = _EXECSTART_RE.search(unit_text)
    if not m:
        return None, None
    cmd = m.group(1).lstrip("-@+!:").strip()
    toks = cmd.split()
    interpreter = toks[0] if toks else None
    script = toks[1] if len(toks) >= 2 else None
    return interpreter, script


def _dangling_linux(m3_memory_root: str) -> list[dict]:
    dangling: list[dict] = []
    unit_path = os.path.expanduser(_SYSTEMD_UNIT)
    if os.path.exists(unit_path):
        try:
            with open(unit_path, encoding="utf-8") as f:
                interpreter, script = _parse_execstart(f.read())
            rec = _flag_missing("systemd:m3-cognitive-loop.service", interpreter, script)
            if rec:
                dangling.append(rec)
        except Exception as e:  # noqa: BLE001
            dangling.append({"job": f"systemd:{unit_path}", "interpreter": None,
                             "script": None, "missing": ["parse-error"], "error": str(e)})
    dangling.extend(_dangling_crontab())
    return dangling


# ── Shared Unix crontab backend ────────────────────────────────────────────

def _read_crontab() -> str | None:
    """Current user's crontab text, or None if there is none / crontab absent."""
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except FileNotFoundError:
        return None  # no crontab binary — nothing scheduled that way
    if proc.returncode != 0:
        return None  # "no crontab for user" also returns non-zero
    return proc.stdout


def _dangling_crontab() -> list[dict]:
    """Flag crontab lines that invoke a now-missing m3 interpreter/script.

    Only inspects lines that reference m3's bin/ scripts or venv python, so a
    user's unrelated cron jobs are never touched. A cron line is
    ``<m h dom mon dow> <command…>``; we scan the command tokens for the first
    absolute path that looks like m3's interpreter or a bin/ script.
    """
    text = _read_crontab()
    if not text:
        return []
    dangling: list[dict] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Consider only m3-OWNED lines, so a user's unrelated cron jobs are
        # never touched. m3's cron entries either invoke the m3 venv python
        # (…/.venv/bin/python …/bin/<script>.py) or the pg_sync.sh wrapper — a
        # generic "*.sh under some bin/" gate would wrongly catch /usr/bin/*.sh.
        is_venv_python = re.search(r"/\.venv/(bin|Scripts)/python", line)
        is_pg_sync = re.search(r"/bin/pg_sync\.sh\b", line)
        if not (is_venv_python or is_pg_sync):
            continue
        toks = line.split()
        interpreter = None
        script = None
        for tok in toks:
            path = tok.strip('"')
            # crontab paths are always POSIX — use posixpath.isabs so the parse
            # is correct even when `m3 doctor` runs on Windows (os.path.isabs
            # would reject every '/'-rooted token there).
            if not posixpath.isabs(path):
                continue
            if interpreter is None and re.search(r"/\.venv/(bin|Scripts)/python", path):
                interpreter = path
            elif script is None and re.search(r"/bin/[\w.-]+\.(py|sh)$", path):
                script = path
        # pg_sync.sh lines invoke the .sh directly (no python interpreter token);
        # treat the .sh itself as the interpreter that must exist.
        rec = _flag_missing(f"crontab:line-{lineno}", interpreter or script, script if interpreter else None)
        if rec:
            dangling.append(rec)
    return dangling


# ── Dispatcher ─────────────────────────────────────────────────────────────

def find_dangling(m3_memory_root: str) -> list[dict]:
    """Return one record per installed background job whose interpreter or
    script is missing on disk, using the backend for the current OS.

    Not-installed jobs are skipped (not dangling). Query/parse errors for a
    single job are recorded (missing=["query-error"|"parse-error"]) rather than
    aborting the whole probe.
    """
    if sys.platform == "win32":
        return _dangling_windows(m3_memory_root)
    if sys.platform == "darwin":
        return _dangling_darwin(m3_memory_root)
    return _dangling_linux(m3_memory_root)  # linux / other posix


def _platform_label() -> str:
    return {"win32": "Windows Task Scheduler", "darwin": "launchd + crontab"}.get(
        sys.platform, "systemd --user + crontab"
    )


def run(brief: bool = False) -> int:
    """Report installed background jobs that point at a missing interpreter or
    script. Always returns 0 (report-only). Never crashes the doctor."""
    m3_memory_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    try:
        dangling = find_dangling(m3_memory_root)
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        if brief:
            print("schedules: unknown (probe failed)")
        else:
            print()
            print("=== scheduled-job interpreters ===")
            print(f"  status   : could not run schedule probe: {type(e).__name__}: {e}")
        return 0

    if brief:
        if dangling:
            print(f"⚠️  schedules: {len(dangling)} dangling job(s); run `m3 setup`")
        else:
            print("✅ schedules: OK (installed jobs resolve to a real interpreter)")
        return 0

    print()
    print("=== scheduled-job interpreters ===")
    print(f"  backend  : {_platform_label()}")
    if not dangling:
        print("  status   : OK — every installed background job points at an")
        print("             interpreter and script that still exist on disk.")
        return 0

    print(f"  status   : NAG — {len(dangling)} installed job(s) point at a missing")
    print("             interpreter/script. They fire on schedule but cannot launch:")
    for d in dangling:
        err = d.get("missing", [])
        if "query-error" in err or "parse-error" in err:
            print(f"             - {d['job']} → could not read: {d.get('error', '')}")
            continue
        bits = []
        if "interpreter" in err:
            bits.append(f"interpreter {d['interpreter']!r} (missing)")
        if "script" in err:
            bits.append(f"script {d['script']!r} (missing)")
        print(f"             - {d['job']} → " + "; ".join(bits))
    print()
    print("  why      : the code install moved or was deleted (e.g. the venv/pipx")
    print("             dir was removed). The job stays registered but its baked-in")
    print("             absolute path no longer resolves, so the run fails silently —")
    print("             the background governor never comes back until re-registered.")
    print()
    print("  fix      : re-register the jobs against the current install:")
    print("               m3 setup")
    return 0
