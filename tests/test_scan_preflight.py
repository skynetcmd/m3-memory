"""The scan orchestrator must refuse to upload a partial run.

THE FOOTGUN: run_scanner() catches a missing binary, prints `[name] ERROR`, and
RETURNS. main() then uploads whatever report files exist. On a host with only a
few scanners installed -- a dev workstation rather than the scan box -- that
publishes a partial run to the central DefectDojo engagement, where it renders
as a normal scan. The dashboard becomes the evidence: a reader sees few findings
and concludes the repo is clean, when most scanners never ran.

A silent partial upload is worse than no scan, because no scan is visibly absent
while a partial one looks complete (section 3: fail loud, never silent; a false
green trains people to trust the dashboard).

Measured on this host 2026-09-14: 15 of 19 scanners absent.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scan_repo_v7.py"

pytestmark = pytest.mark.skipif(not _SCRIPT.is_file(), reason="scan_repo_v7.py absent")


def _load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("scan_repo_v7", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_imports_without_defectdojo_credentials():
    """The credential resolver must NOT run at import. The host most likely to
    be missing scanners is also the one with no DefectDojo credentials, so
    resolving eagerly would exit(2) before the check could ever report."""
    mod = _load()  # must not raise SystemExit
    assert mod.DD_TOKEN is None, "token must resolve lazily, not at import"


def test_missing_scanners_is_detectable():
    mod = _load()
    missing = mod.missing_scanners()
    assert isinstance(missing, list)
    assert len(missing) <= len(mod.SCANNERS)


def test_preflight_refuses_when_scanners_are_missing(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "missing_scanners", lambda: ["gitleaks", "trivy"])
    with pytest.raises(SystemExit) as e:
        mod._preflight(force_partial=False)
    assert e.value.code == 3, "a deficient host must exit 3, not 0"


def test_preflight_names_every_missing_scanner(monkeypatch, capsys):
    mod = _load()
    monkeypatch.setattr(mod, "missing_scanners", lambda: ["gitleaks", "trivy"])
    with pytest.raises(SystemExit):
        mod._preflight(force_partial=False)
    err = capsys.readouterr().err
    assert "gitleaks" in err and "trivy" in err


def test_force_flag_proceeds_but_still_reports(monkeypatch, capsys):
    """The escape hatch must not be silent: an operator who forces it should
    still see what they suppressed, so it cannot be forced by accident and
    forgotten."""
    mod = _load()
    monkeypatch.setattr(mod, "missing_scanners", lambda: ["gitleaks"])
    mod._preflight(force_partial=True)  # must NOT raise
    err = capsys.readouterr().err
    assert "gitleaks" in err
    assert "NOT reflect" in err


def test_complete_host_passes_preflight(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "missing_scanners", lambda: [])
    mod._preflight(force_partial=False)  # must not raise


def test_bash_wrapped_scanners_resolve_to_the_real_binary():
    """Several entries are ['bash','-c','gitleaks ...']. Reporting 'bash' as
    present would mark every wrapped scanner installed on any POSIX host."""
    mod = _load()
    got = mod._scanner_binary(["bash", "-c", "gitleaks detect --source {repo}"])
    assert got == "gitleaks", f"resolved to {got!r}, not the wrapped tool"


def test_check_only_exits_nonzero_on_a_deficient_host():
    """End-to-end through the CLI, so the exit code a script would see is the
    one under test -- main() returning a code is useless if __main__ drops it."""
    r = subprocess.run([sys.executable, str(_SCRIPT), str(_ROOT), "--check-only"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode in (0, 3), f"unexpected exit {r.returncode}: {r.stderr[:200]}"
    if r.returncode == 3:
        assert "missing" in r.stderr.lower()


def test_main_actually_calls_the_preflight():
    """The guard is worthless if main() does not invoke it.

    Found by counter-test: deleting the `_preflight(...)` call from main()
    reintroduced the footgun -- a plain run scanned and uploaded blindly -- and
    every other test in this file still passed, because they exercise the
    function in isolation. A test that cannot fail when the defect returns is
    itself a section 3 violation.
    """
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "_preflight(args.force_partial_upload)" in src, (
        "main() no longer calls _preflight -- a partial run will upload silently"
    )


def test_preflight_runs_before_any_scanning(monkeypatch):
    """Order matters: refusing AFTER the scan wastes 20 minutes and, worse,
    leaves report files on disk that a later run could upload."""
    src = _SCRIPT.read_text(encoding="utf-8")
    body = src[src.index("def main("):]
    pre = body.index("_preflight(")
    scan = body.index("run_scanner(")
    assert pre < scan, "_preflight must run before the first run_scanner call"


def test_plain_run_on_a_deficient_host_refuses_end_to_end():
    """The whole point, through the CLI: no scan, no upload, exit 3."""
    mod = _load()
    if not mod.missing_scanners():
        pytest.skip("this host has every scanner; nothing to refuse")
    r = subprocess.run([sys.executable, str(_SCRIPT), str(_ROOT)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 3, f"expected refusal (3), got {r.returncode}"
    assert "Refusing to upload a partial run" in r.stderr
    assert "== scanning" not in r.stdout, "it started scanning despite refusing"
