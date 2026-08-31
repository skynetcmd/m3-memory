"""Guard: every long-lived entrypoint bounds its --log-file at startup.

WHY THIS EXISTS
A daemon that runs for weeks and appends to a log nothing ever rotates grows
without limit. Measured in the wild on 2026-08-29: bin/m3_cognitive_loop.py
produced a 720 MB cognitive_loop.log. _task_runtime._rotate_if_oversized had
already been written to prevent exactly this, but the cognitive loop and the
in-process embed server never called it — they manage their own daemonize and
handlers, so they bypassed setup_task_runtime() and the bound with it.

Truncating such a log out-of-band does NOT fix it: the running process holds an
open handle at its stored offset, so the next write zero-fills the gap and the
file springs back to full size (observed on Windows). The bound has to happen at
startup, BEFORE the handle is opened. That is what these tests pin.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN))

import _task_runtime  # noqa: E402
from _task_runtime import _rotate_if_oversized  # noqa: E402

CAP = 64 * 1024 * 1024


def _make_oversized(path: Path, size: int = CAP + 1024) -> None:
    """Create a sparse file just past the rotation cap (no real bytes written)."""
    with open(path, "wb") as fh:
        fh.seek(size)
        fh.write(b"x")


# --- the shared helper ------------------------------------------------------

def test_under_cap_is_left_alone(tmp_path):
    log = tmp_path / "d.log"
    log.write_text("small\n", encoding="utf-8")
    _rotate_if_oversized(log)
    assert log.exists()
    assert not (tmp_path / "d.log.1").exists()


def test_over_cap_rotates_to_single_generation(tmp_path):
    log = tmp_path / "d.log"
    _make_oversized(log)
    _rotate_if_oversized(log)
    assert not log.exists(), "oversized log must be moved aside"
    assert (tmp_path / "d.log.1").exists(), "previous generation must be kept"


def test_second_rotation_drops_the_older_generation(tmp_path):
    """Single generation only — .1/.2/.3 accumulating would defeat the point."""
    log = tmp_path / "d.log"
    for _ in range(2):
        _make_oversized(log)
        _rotate_if_oversized(log)
    generations = sorted(p.name for p in tmp_path.iterdir())
    assert generations == ["d.log.1"], generations


def test_cap_zero_disables_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("M3_LOG_MAX_BYTES", "0")
    _reset_log_cfg_cache()
    log = tmp_path / "d.log"
    _make_oversized(log)
    _rotate_if_oversized(log)
    assert log.exists(), "M3_LOG_MAX_BYTES=0 must disable rotation"


# --- §3: the cap must be reachable from a HEADLESS launcher -----------------
# A Windows scheduled task / launchd agent / systemd --user unit runs with a
# BARE environment, so an env-only knob is absent in exactly the process whose
# log grows. Precedence must be: config file > env var > default.

def _reset_log_cfg_cache():
    _task_runtime._log_cfg_cache.update({"ts": 0.0, "mtime": None, "max_bytes": None})


@pytest.fixture
def log_cfg(tmp_path, monkeypatch):
    """Point the resolver at a temp config file and clear its mtime cache."""
    cfg = tmp_path / ".log_config.json"
    monkeypatch.setattr(_task_runtime, "_log_config_path", lambda: str(cfg))
    _reset_log_cfg_cache()
    yield cfg
    _reset_log_cfg_cache()


def test_default_cap_when_nothing_configured(log_cfg, monkeypatch):
    monkeypatch.delenv("M3_LOG_MAX_BYTES", raising=False)
    _reset_log_cfg_cache()
    assert _task_runtime._log_max_bytes() == CAP


def test_env_overrides_default(log_cfg, monkeypatch):
    monkeypatch.setenv("M3_LOG_MAX_BYTES", "1234")
    _reset_log_cfg_cache()
    assert _task_runtime._log_max_bytes() == 1234


def test_config_file_beats_env(log_cfg, monkeypatch):
    """The §3 requirement: headless launchers can only see the file."""
    monkeypatch.setenv("M3_LOG_MAX_BYTES", "1234")
    log_cfg.write_text(json.dumps({"max_bytes": 999}), encoding="utf-8")
    _reset_log_cfg_cache()
    assert _task_runtime._log_max_bytes() == 999


def test_malformed_config_warns_loudly_and_falls_back(log_cfg, monkeypatch, capsys):
    """§3 never silent: dead tuning must not look like working tuning."""
    monkeypatch.setenv("M3_LOG_MAX_BYTES", "1234")
    log_cfg.write_text("{not json", encoding="utf-8")
    _reset_log_cfg_cache()
    monkeypatch.setattr(sys, "__stderr__", sys.stderr)  # route to capsys
    assert _task_runtime._log_max_bytes() == 1234, "must fall back to env"
    assert "WARNING" in capsys.readouterr().err, "malformed config must warn"


def test_rotation_failure_is_not_silent(tmp_path, monkeypatch, capsys):
    """Fail safe (don't block startup) but never silent (§3)."""
    log = tmp_path / "d.log"
    _make_oversized(log)

    def _boom(*a, **k):
        raise OSError("simulated: log locked by another process")

    monkeypatch.setattr(Path, "replace", _boom)
    monkeypatch.setattr(sys, "__stderr__", sys.stderr)
    _rotate_if_oversized(log)  # must NOT raise
    assert "WARNING" in capsys.readouterr().err, (
        "a rotation failure means the only disk-space bound is dead — say so"
    )


def test_malformed_cap_falls_back_to_default(tmp_path, monkeypatch):
    """A typo'd env var must not silently disable the only disk-space bound."""
    monkeypatch.setenv("M3_LOG_MAX_BYTES", "not-a-number")
    log = tmp_path / "d.log"
    _make_oversized(log)
    _rotate_if_oversized(log)
    assert not log.exists(), "malformed cap must fall back to the 64MB default"


# --- the entrypoints that must USE it ---------------------------------------

# Long-lived: runs until the machine goes down, so its log is never otherwise
# bounded. install_schedules.py is deliberately absent — it only PASSES
# --log-file through to the tasks it registers; it is not itself a daemon.
LONG_LIVED = ["m3_cognitive_loop.py", "embed_server_inproc.py"]


@pytest.mark.parametrize("name", LONG_LIVED)
def test_long_lived_entrypoint_bounds_its_log(name):
    src = (BIN / name).read_text(encoding="utf-8")
    assert "--log-file" in src, f"{name} no longer takes --log-file; update this guard"
    assert "_rotate_if_oversized" in src or "setup_task_runtime" in src, (
        f"{name} opens a --log-file but never bounds it at startup. A long-lived "
        f"daemon appending to an unrotated log grows without limit (720 MB was "
        f"measured on 2026-08-29). Call _rotate_task_log()/_rotate_if_oversized() "
        f"BEFORE opening the handle, or route through setup_task_runtime()."
    )


def test_cognitive_loop_rotates_before_opening_the_inherited_handle():
    """Order matters: the parent hands this handle to the child.

    Rotating after the open would leave the child writing to the old, oversized
    inode — the bug would persist while looking fixed.
    """
    src = (BIN / "m3_cognitive_loop.py").read_text(encoding="utf-8")
    rotate = src.index("_rotate_task_log(args.log_file)")
    child_open = src.index('child_out = open(args.log_file, "a"')
    assert rotate < child_open, "must rotate BEFORE opening the inherited handle"


def test_posix_launchers_reach_rotation_without_a_re_exec():
    """macOS/Linux coverage: main()'s FileHandler is the ONLY rotation point.

    The launchd plist and the systemd unit deliberately do NOT pass
    --background (they manage the process directly; a double-fork would lose
    the real worker). So daemonize_windows() never runs there and the parent
    redirect above is Windows-only. If main() ever stopped rotating, both POSIX
    platforms would silently go unbounded again while Windows looked fine.
    """
    import ast

    src = (BIN / "m3_cognitive_loop.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.parse(src).body
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    lines = src.splitlines()
    body = "\n".join(lines[fn.lineno - 1: (fn.end_lineno or len(lines))])

    rotate = body.find("_rotate_task_log(args.log_file)")
    handler = body.find("logging.FileHandler(args.log_file")
    assert rotate != -1, "main() must rotate — it is the only POSIX rotation point"
    assert handler != -1, "main() no longer opens a FileHandler; update this guard"
    assert rotate < handler, "must rotate BEFORE opening the handler"


@pytest.mark.parametrize("unit", ["com.m3memory.cognitiveloop.plist",
                                  "m3-cognitive-loop.service"])
def test_posix_units_still_pass_a_log_file(unit):
    """If a unit stops passing --log-file the rotation guard above is moot."""
    path = BIN / unit
    if not path.exists():
        pytest.skip(f"{unit} not present")
    assert "--log-file" in path.read_text(encoding="utf-8"), (
        f"{unit} no longer passes --log-file; the rotation path it exercises "
        f"is dead — update this guard deliberately, don't delete it."
    )


def test_rotation_has_no_database_coupling():
    """The rotation path must stay backend-agnostic (SQLite AND PostgreSQL).

    Rotation is pure filesystem work. If it ever grows a DB import it would
    become backend-sensitive and could fail on one of the two supported
    stores — a class of bug this repo takes seriously (§10a).
    """
    src = (BIN / "_task_runtime.py").read_text(encoding="utf-8")
    for banned in ("sqlite3", "psycopg", "asyncpg", "get_m3_engine_root"):
        assert banned not in src, (
            f"_task_runtime imports {banned!r}: log rotation must not depend on "
            f"a database backend"
        )


@pytest.mark.parametrize("name", LONG_LIVED)
def test_entrypoint_still_imports_cleanly(name):
    """The rotation import must resolve for real, not fail into a bare except.

    Both call sites swallow exceptions so a rotation failure cannot stop the
    daemon booting. That makes a typo (e.g. an undefined `pathlib`) invisible at
    runtime, so verify the module imports and compiles here instead.
    """
    proc = subprocess.run(
        [sys.executable, "-c", f"import py_compile,sys; py_compile.compile(r'{BIN / name}', doraise=True)"],
        capture_output=True, text=True, env={**os.environ, "M3_SKIP_AUTOSTART": "1"},
    )
    assert proc.returncode == 0, proc.stderr
