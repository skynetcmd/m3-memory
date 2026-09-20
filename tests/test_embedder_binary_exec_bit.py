"""The embed-server binary must be executable before we exec it.

⚠ ONE EACCES LOOKED LIKE FOUR FAILURES. The `m3-core-rs-macos-metal` wheel
shipped `m3_core_rs/m3-embed-server` as mode 0644, so `m3 setup` step 2/5 died
with `PermissionError: [Errno 13]` out of `_service_cmd`. That single cause
surfaced as:

    embedding-cascade: broken
    embed-server: error
    shared-embedder: 2 issue(s)
    nothing LISTENing on :8082
    VERIFICATION FAILED

which reads as an embedder or config problem and sends the diagnosis in the
wrong direction entirely (macOS arm64, py3.14, m3-memory 2026.9.20.1).

Packaging the wheel 0755 is the real fix. This guards the defensive half, which
is worth keeping anyway: it costs one stat on a path we are about to exec, and
it survives a future wheel that regresses.

⚠ WINDOWS CANNOT RUN THE MEANINGFUL ASSERTION. `os.access(path, X_OK)` is True
for every file there and `os.chmod` ignores exec bits entirely — a probe on
Windows reports "added the exec bit" while the mode stays 0644. Measured, and
it is exactly the sort of green-for-the-wrong-reason this file exists to
prevent, so the mode assertions are POSIX-only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from m3_memory.embedder_admin import _ensure_executable  # noqa: E402

_posix_only = pytest.mark.skipif(
    os.name == "nt",
    reason="Windows has no exec bit: os.access(X_OK) is always True and "
           "os.chmod ignores 0o111, so the assertion cannot fail here",
)


@_posix_only
def test_a_non_executable_binary_gains_the_exec_bit(tmp_path):
    """0644 — exactly what the metal wheel shipped — must become executable."""
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(binary, 0o644)
    assert not os.access(binary, os.X_OK), "fixture did not start non-executable"

    _ensure_executable(binary)

    assert os.access(binary, os.X_OK), (
        "the binary is still not executable — `m3 setup` will die with "
        "PermissionError and report four unrelated-looking failures"
    )
    assert os.stat(binary).st_mode & 0o111, "no exec bits were set"


@_posix_only
def test_existing_permissions_are_preserved(tmp_path):
    """Add the exec bits, never replace the mode wholesale.

    A blunt chmod(0o755) would widen or narrow read/write for group and other;
    the fix ORs 0o111 onto whatever the file already had.
    """
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\n")
    os.chmod(binary, 0o600)          # private, non-executable

    _ensure_executable(binary)

    mode = os.stat(binary).st_mode & 0o777
    assert mode & 0o100, "owner exec bit missing"
    assert mode & 0o600 == 0o600, f"owner read/write was altered: {mode:o}"
    assert not mode & 0o044, f"read access was widened to group/other: {mode:o}"


def test_an_already_executable_binary_is_untouched(tmp_path):
    """The common case must not churn the mode on every call."""
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\n")
    if os.name != "nt":
        os.chmod(binary, 0o755)
    before = os.stat(binary).st_mode

    _ensure_executable(binary)

    assert os.stat(binary).st_mode == before, "mode changed on an executable file"


def test_a_missing_binary_does_not_raise(tmp_path):
    """Best-effort: the exec below reports the real failure.

    Raising here would replace a clear "no such file" from the exec with a
    stat traceback from the permission helper.
    """
    _ensure_executable(tmp_path / "does-not-exist")


def test_service_cmd_ensures_the_bit_before_exec():
    """The call site matters, not just the helper.

    A helper nothing invokes is the shape of defect this fix was written
    against, so pin that _service_cmd actually calls it.
    """
    import inspect

    from m3_memory import embedder_admin

    src = inspect.getsource(embedder_admin._service_cmd)
    assert "_ensure_executable(" in src, (
        "_service_cmd no longer ensures the exec bit before running the "
        "binary; a non-executable wheel will fail setup again"
    )


# ── doctor reports it, --fix repairs it ──────────────────────────────────────

from m3_memory.embedder_admin import exec_bit_status, repair_exec_bit  # noqa: E402


@_posix_only
def test_status_detects_a_non_executable_binary(tmp_path):
    """Detection is separate from repair on purpose.

    `_ensure_executable` fixes this at exec time, which keeps setup working —
    but a silent repair hides a wheel that ships 0644, and a read-only install
    cannot be repaired at all. doctor has to be able to SAY it.
    """
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\n")
    os.chmod(binary, 0o644)

    st = exec_bit_status(binary)
    assert st["state"] == "not-executable", st
    assert st["mode"] == "644"
    assert "chmod +x" in st["detail"], "the report must name the fix"


@_posix_only
def test_status_is_ok_for_an_executable_binary(tmp_path):
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\n")
    os.chmod(binary, 0o755)
    assert exec_bit_status(binary)["state"] == "ok"


def test_status_is_ok_on_windows_without_pretending(tmp_path):
    """Windows has no exec bit; a check there would be vacuously green.

    Asserting the DETAIL, not just the state: the point is that it says why,
    rather than implying something was verified.
    """
    if os.name != "nt":
        pytest.skip("Windows-specific")
    st = exec_bit_status(tmp_path / "anything")
    assert st["state"] == "ok"
    assert "no exec bit" in st["detail"].lower()


@_posix_only
def test_repair_fixes_a_user_owned_binary(tmp_path):
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\n")
    os.chmod(binary, 0o644)

    res = repair_exec_bit(binary)
    assert res["status"] == "fixed", res
    assert exec_bit_status(binary)["state"] == "ok"

    # Idempotent: a second --fix must not report a repair it did not make.
    assert repair_exec_bit(binary)["status"] == "ok"


@_posix_only
def test_repair_dry_run_changes_nothing(tmp_path):
    binary = tmp_path / "m3-embed-server"
    binary.write_text("#!/bin/sh\n")
    os.chmod(binary, 0o644)

    res = repair_exec_bit(binary, dry_run=True)
    assert res["status"] == "skipped"
    assert "would chmod" in res["detail"]
    assert exec_bit_status(binary)["state"] == "not-executable", (
        "dry_run repaired the file anyway"
    )


def test_repair_never_prompts_for_a_password():
    """⚠ `sudo -n`, NOT `sudo`. doctor --fix runs from scheduled tasks and
    scripts; a hidden password prompt would hang them indefinitely. The
    non-interactive flag makes an unprivileged failure immediate and reports
    the manual command instead."""
    import inspect

    from m3_memory import embedder_admin

    src = inspect.getsource(embedder_admin.repair_exec_bit)
    assert '"sudo", "-n"' in src, (
        "the escalation lost its non-interactive flag — doctor --fix can now "
        "block on a password prompt"
    )
