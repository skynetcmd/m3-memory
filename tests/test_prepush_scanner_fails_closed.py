"""The leakage scanner must never report a clean scan it did not perform.

A false green here is a disclosure, so each test below is one way the scan can
decline to run — unreadable policy, empty policy, a pattern that will not
compile, a broken allow-list — and every one of them must BLOCK rather than
pass. Both directions are asserted: a gate tested only for quiet is one nobody
has shown still catches anything.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCANNER = Path(__file__).resolve().parent.parent / "bin" / "prepush" / "scan_diff.py"

# A key-shaped pattern, assembled so this file does not itself carry a literal
# that the real scanner would (correctly) flag on every push.
_KEY_PREFIX = "sk-" + "ant-"
_PATTERN = _KEY_PREFIX + r"[a-z0-9]{20,}"
_HIT = _KEY_PREFIX + "abcdefghij0123456789xyz"


def _run(diff: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCANNER), *args],
        input=diff, capture_output=True, text=True,
    )


def test_the_scanner_exists_where_the_hook_expects_it():
    """The hook blocks if this file is missing; keep the path honest."""
    assert SCANNER.is_file(), f"hook expects the scanner at {SCANNER}"


def test_a_real_hit_blocks(tmp_path):
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\n")
    r = _run(f'+++ b/x.py\n+KEY = "{_HIT}"\n', "--patterns", str(pats))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BLOCKED" in r.stderr


def test_a_clean_diff_passes(tmp_path):
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\n")
    r = _run("+++ b/x.py\n+nothing to see\n", "--patterns", str(pats))
    assert r.returncode == 0, r.stdout + r.stderr


def test_an_uncompilable_pattern_blocks_instead_of_passing(tmp_path):
    """The exact shape of the 2026-10-02 failure."""
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\nfoo(\n")          # second pattern cannot compile
    r = _run("+++ b/x.py\n+nothing to see\n", "--patterns", str(pats))
    assert r.returncode == 1, "an unevaluable policy read as a clean scan"
    assert "BLOCKED" in r.stderr
    assert "p.txt:2" in r.stderr, "must name the offending line"


def test_the_block_message_withholds_the_pattern_body(tmp_path):
    """The patterns are themselves a disclosure; the error must not print them."""
    pats = tmp_path / "p.txt"
    secret_pattern = "internal-codename-xyzzy[0-9]{2,}("   # uncompilable AND sensitive
    pats.write_text(secret_pattern + "\n")
    r = _run("+nothing\n", "--patterns", str(pats))
    assert r.returncode == 1
    assert "xyzzy" not in r.stderr + r.stdout, "leaked the pattern body in a diagnostic"
    assert "withheld" in r.stderr


def test_one_bad_pattern_does_not_disable_the_others(tmp_path):
    """Why patterns are compiled independently rather than joined with `|`."""
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\n")
    good = _run(f'+KEY = "{_HIT}"\n', "--patterns", str(pats))
    assert good.returncode == 1

    pats.write_text(_PATTERN + "\nfoo(\n")
    both = _run(f'+KEY = "{_HIT}"\n', "--patterns", str(pats))
    # Still blocks -- and for the stricter reason (policy unevaluable), never
    # silently losing the pattern that would have caught the hit.
    assert both.returncode == 1


def test_a_missing_pattern_file_blocks(tmp_path):
    r = _run("+nothing\n", "--patterns", str(tmp_path / "absent.txt"))
    assert r.returncode == 1
    assert "cannot read" in r.stderr


def test_an_empty_policy_blocks(tmp_path):
    pats = tmp_path / "p.txt"
    pats.write_text("# only a comment\n\n")
    r = _run("+nothing\n", "--patterns", str(pats))
    assert r.returncode == 1
    assert "no usable patterns" in r.stderr


def test_no_patterns_at_all_blocks():
    r = _run("+nothing\n")
    assert r.returncode == 1
    assert "no patterns given" in r.stderr


def test_only_added_lines_are_scanned(tmp_path):
    """Deletions cannot reach the remote; `+++` headers are file paths."""
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\n")
    r = _run(f'--- a/x.py\n+++ b/{_HIT}.py\n-KEY = "{_HIT}"\n', "--patterns", str(pats))
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_placeholder_allow_list_is_applied(tmp_path):
    pats = tmp_path / "p.txt"
    pats.write_text(r"/home/[a-z]+/" + "\n")
    allow = r"(/home/(bob|alice))([\\/]|$)"
    assert _run("+p = /home/bob/x\n", "--patterns", str(pats),
                "--exclude-identities", allow).returncode == 0
    # Assembled at runtime: the literal would trip the live leak policy on push.
    assert _run("+p = /home/" + "realperson/x\n", "--patterns", str(pats),
                "--exclude-identities", allow).returncode == 1


def test_an_uncompilable_allow_list_blocks(tmp_path):
    """A broken allow-list would turn every placeholder into a reported leak."""
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\n")
    r = _run("+nothing\n", "--patterns", str(pats), "--exclude-identities", "foo(")
    assert r.returncode == 1
    assert "allow-list" in r.stderr


def test_selftest_passes_on_a_good_policy(tmp_path):
    pats = tmp_path / "p.txt"
    pats.write_text(_PATTERN + "\n")
    r = _run("", "--selftest", "--patterns", str(pats))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASS" in r.stdout


def test_selftest_fails_on_a_policy_that_will_not_compile(tmp_path):
    """The whole point: a gate that cannot run must not report that it ran."""
    pats = tmp_path / "p.txt"
    pats.write_text("foo(\n")
    r = _run("", "--selftest", "--patterns", str(pats))
    assert r.returncode == 1
    assert "does not load" in r.stdout or "BLOCKED" in r.stdout + r.stderr
