"""Run several administrator actions under ONE Windows UAC prompt.

Hazard: setup used to raise a separate UAC dialog for each privileged step
(boot-task registration, legacy task removal, ...), so one run could ask for
consent several times, and a dialog lost behind the terminal silently dropped
its step. Queuing the steps and running them in a single elevated PowerShell
process asks once; the per-step exit codes come back through a results file, so
each step is still reported as done or not.

Windows only. Never raises; a cancelled prompt or a missing results file means
no step ran.
"""
from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed argv lists, no shell
import sys
import tempfile


def _ps_quote(s: str) -> str:
    """A PowerShell single-quoted literal (a ' inside is doubled)."""
    return "'" + str(s).replace("'", "''") + "'"


class ElevationBatch:
    """Queued (label, argv) actions, run together under one UAC prompt."""

    def __init__(self) -> None:
        self.actions: "list[tuple[str, list[str]]]" = []

    def add(self, label: str, argv: "list[str]") -> None:
        """Queue an action; the same command queued twice runs once."""
        if all(list(argv) != a for _, a in self.actions):
            self.actions.append((label, list(argv)))

    def __len__(self) -> int:
        return len(self.actions)

    def render_script(self, results_path: str, log_dir: str) -> str:
        """The PowerShell the elevated process runs: each action in order, its
        output to its own log and its exit code under its label, then the codes
        written as JSON."""
        lines = ["$ErrorActionPreference = 'Continue'", "$r = [ordered]@{}"]
        for i, (label, argv) in enumerate(self.actions):
            call = "& " + " ".join(_ps_quote(a) for a in argv)
            log = _ps_quote(os.path.join(log_dir, f"{i}.log"))
            lines.append(f"try {{ {call} *> {log}; $r[{_ps_quote(label)}] = $LASTEXITCODE }} "
                         f"catch {{ $_ | Out-File -LiteralPath {log} -Append; "
                         f"$r[{_ps_quote(label)}] = -1 }}")
        lines.append(f"$r | ConvertTo-Json | Set-Content -LiteralPath {_ps_quote(results_path)} "
                     f"-Encoding UTF8")
        return "\r\n".join(lines) + "\r\n"

    def run(self) -> "dict[str, dict] | None":
        """Run every queued action in one elevated process. Returns
        {label: {"rc": exit code, "output": last lines}}, or None when the
        prompt was declined or the elevated process wrote no results (then
        nothing can be assumed to have run).
        """
        if not self.actions:
            return {}
        if sys.platform != "win32":
            return None
        tmp = tempfile.mkdtemp(prefix="m3-elevate-")
        script = os.path.join(tmp, "actions.ps1")
        results = os.path.join(tmp, "results.json")
        with open(script, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(self.render_script(results, tmp))
        launcher = (
            "$p = Start-Process -FilePath 'powershell' -ArgumentList "
            f"'-NoProfile','-ExecutionPolicy','Bypass','-File',{_ps_quote(script)} "
            "-Verb RunAs -PassThru -Wait -WindowStyle Hidden; exit $p.ExitCode"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", launcher],
                           timeout=900, check=False, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            return None
        try:
            with open(results, encoding="utf-8-sig") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return None
            out: "dict[str, dict]" = {}
            for i, (label, _argv) in enumerate(self.actions):
                rc = data.get(label)
                out[label] = {"rc": -1 if rc is None else int(rc),
                              "output": _tail(os.path.join(tmp, f"{i}.log"))}
            return out
        except (OSError, ValueError):
            return None  # declined, or the elevated process never got that far
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _tail(path: str, lines: int = 8) -> str:
    """The last lines of an action's log (PowerShell writes UTF-16 by default)."""
    for enc in ("utf-16", "utf-8-sig"):
        try:
            with open(path, encoding=enc) as fh:
                return "\n".join(fh.read().strip().splitlines()[-lines:])
        except (OSError, UnicodeError):
            continue
    return ""
