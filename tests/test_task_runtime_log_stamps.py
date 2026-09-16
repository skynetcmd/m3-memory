"""Every line a scheduled task writes must carry a UTC ISO-8601 stamp.

A task log is read long after the run, usually to work out WHICH run a
traceback belongs to. Two gaps made that impossible before 2026-09-16:

* ``logging`` stamped its records but a bare ``print()`` wrote straight to the
  redirected stdout and landed undated — and these entrypoints print far more
  than they log (``m3_enrich.py``: 45 prints, zero logging calls). The drain log
  had to be ordered by LINE NUMBER to attribute a traceback.
* ``%(asctime)s`` renders LOCAL time with no offset, so timestamps could not be
  correlated with the Rust embed server (ISO-8601 Z) or the PostgreSQL
  warehouse on another host — and during the autumn DST fold an hour of lines
  repeats with no way to order it.
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))

# 2026-09-16T15:09:00.334Z — the same shape the Rust embed server emits.
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3,6}Z ")


def _capture(tmp_path, emit) -> list[str]:
    """Run `emit` with task-runtime redirection active, return the log lines.

    Closes the redirected handle before restoring stdout: leaving it to the
    garbage collector raises an unraisable exception that pytest turns into a
    failure, which masks the assertions this helper exists to make.
    """
    import _task_runtime as tr

    log = tmp_path / "probe.log"
    real_out, real_err = sys.stdout, sys.stderr
    try:
        # _redirect_output is called directly rather than via
        # setup_task_runtime(), whose _INITIALIZED guard makes a second call in
        # the same process a no-op — every test after the first would then
        # assert against an empty file.
        tr._redirect_output(log, "probe")
        emit()
        sys.stdout.flush()
    finally:
        redirected = sys.stdout
        sys.stdout, sys.stderr = real_out, real_err
        # Drop the logging handler that holds the same file object, then close.
        logging.basicConfig(handlers=[logging.NullHandler()], force=True)
        try:
            redirected.close()
        except Exception:  # noqa: BLE001 — cleanup must not mask an assertion
            pass
    return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_print_output_is_timestamped(tmp_path):
    lines = _capture(tmp_path, lambda: print("a bare print line"))
    assert lines, "nothing was captured"
    assert STAMP.match(lines[0]), f"print output is not stamped: {lines[0]!r}"
    assert "a bare print line" in lines[0]


def test_logging_output_is_timestamped(tmp_path):
    lines = _capture(tmp_path, lambda: logging.getLogger("probe").info("a logging line"))
    assert lines, "nothing was captured"
    assert STAMP.match(lines[0]), f"logging output is not stamped: {lines[0]!r}"
    assert "a logging line" in lines[0]


def test_multiline_print_stamps_every_line(tmp_path):
    """A traceback arrives as one multi-line write; each line must be datable."""
    lines = _capture(tmp_path, lambda: print("first\nsecond\nthird"))
    assert len(lines) == 3, lines
    for ln in lines:
        assert STAMP.match(ln), f"unstamped continuation line: {ln!r}"


def test_stamps_are_utc_not_local(tmp_path):
    """The stamp must be UTC. A local stamp shifts twice a year (DST)."""
    import datetime

    before = datetime.datetime.now(datetime.timezone.utc)
    lines = _capture(tmp_path, lambda: print("x"))
    after = datetime.datetime.now(datetime.timezone.utc)

    stamped = lines[0].split(" ", 1)[0].rstrip("Z")
    parsed = datetime.datetime.fromisoformat(stamped).replace(
        tzinfo=datetime.timezone.utc
    )
    # A local-time stamp on a machine off UTC lands outside this window.
    assert before <= parsed <= after, (
        f"stamp {parsed} outside the UTC window {before}..{after} — "
        "looks like local time"
    )


def test_print_and_logging_share_one_format(tmp_path):
    """Mixed lines must sort and parse identically, or the log is unsortable."""
    def emit():
        print("print line")
        logging.getLogger("probe").info("logging line")

    lines = _capture(tmp_path, emit)
    assert len(lines) == 2, lines
    for ln in lines:
        assert STAMP.match(ln), f"inconsistent stamp: {ln!r}"
