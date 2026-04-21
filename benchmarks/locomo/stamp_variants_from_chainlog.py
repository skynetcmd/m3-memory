"""Retrofit `variant` field into summary.json files based on a chain.log.

The chain runner emits:
    === START <variant> HH:MM:SS ===
    (audit output, including summary_path at the end)
    === DONE  <variant> HH:MM:SS ===

For each bracket, find the audit run_dir whose run.log starts at a
timestamp between START and DONE, and stamp the variant into
summary.json (under key "variant") unless already present.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
RUNS_DIR = BASE / "runs"

START_RE = re.compile(r"^=== START (\S+) (\d\d:\d\d:\d\d) ===")
DONE_RE  = re.compile(r"^=== DONE (\S+) (\d\d:\d\d:\d\d) ===")


def parse_chain(log: Path) -> list[tuple[str, str, str]]:
    """Return list of (variant, start_hhmmss, end_hhmmss)."""
    open_var: dict[str, str] = {}
    out: list[tuple[str, str, str]] = []
    for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = START_RE.match(line)
        if m:
            open_var[m.group(1)] = m.group(2)
            continue
        m = DONE_RE.match(line)
        if m and m.group(1) in open_var:
            out.append((m.group(1), open_var.pop(m.group(1)), m.group(2)))
    return out


_LOCAL_TS_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\]")


def run_dir_time(run_dir: Path) -> str | None:
    """Return the first HH:MM:SS timestamp from run.log (local time, matches
    the local-time markers the chain runner prints). Falls back to parsing
    the audit_YYYYMMDD_HHMMSS dir name if run.log is missing — but note dir
    names are UTC, so the fallback won't line up against a chain log that
    was written in a different tz."""
    log = run_dir / "run.log"
    if log.exists():
        try:
            with log.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = _LOCAL_TS_RE.match(line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    stem = run_dir.name
    if not stem.startswith("audit_"):
        return None
    try:
        _, ymd, hms = stem.split("_", 2)
        if len(hms) == 6:
            return f"{hms[0:2]}:{hms[2:4]}:{hms[4:6]}"
    except ValueError:
        return None
    return None


def stamp(run_dir: Path, variant: str) -> bool:
    s = run_dir / "summary.json"
    if not s.exists():
        return False
    data = json.loads(s.read_text(encoding="utf-8"))
    if data.get("variant"):
        return False
    data["variant"] = variant
    s.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True


def in_range(t: str, start: str, end: str) -> bool:
    return start <= t <= end


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--chain-log", type=Path, required=True)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    brackets = parse_chain(args.chain_log)
    for variant, start, end in brackets:
        for rd in sorted(RUNS_DIR.glob("audit_*")):
            t = run_dir_time(rd)
            if t and in_range(t, start, end):
                if stamp(rd, variant):
                    print(f"stamped {rd.name} -> variant={variant}")
