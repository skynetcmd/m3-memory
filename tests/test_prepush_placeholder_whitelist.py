"""The pre-push leak scan must stay LOUD on real leaks while ignoring fixtures.

The scan matches the SHAPE of a home path, so anonymized placeholders in test
fixtures (`/Users/u/...`) tripped it and blocked legitimate pushes. A gate that
fires on correct work trains people to reach for --no-verify, which is precisely
when a real leak walks through -- so the false alarm is itself the hazard
(DESIGN_PHILOSOPHIES section 3; see m3 memory d8579b92, where the BASELINE was
fixed but the PATTERNS still could not tell a fixture from a leak).

The whitelist is deliberately narrow: only specific placeholder identities, and
only as whole path segments. It is NOT an exclusion of `tests/` wholesale -- a
real credential can land in a test file, and excluding the directory would create
a blind spot. `/Users/ursula` must still trip the scan even though it starts with
the same letter as the placeholder `u`.

Both directions are asserted here. A whitelist that is only tested for quiet is
one nobody has proven still catches anything.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_HOOK = pathlib.Path(__file__).resolve().parent.parent / ".githooks" / "pre-push"


def _patterns() -> tuple[str, str]:
    """(placeholder_whitelist, leak_pattern) as the hook actually defines them."""
    src = _HOOK.read_text(encoding="utf-8")
    m = re.search(r"PLACEHOLDER_IDENTITIES='([^']+)'", src)
    assert m, "PLACEHOLDER_IDENTITIES not found in .githooks/pre-push"
    return m.group(1), r"/Users/[a-z]+|C:[\\/]+Users|/home/[a-z]+/|@gmail|sk-ant-[a-z0-9]{20,}"


def _blocked(line: str) -> bool:
    """Mirror the hook: drop whitelisted lines, then apply the leak pattern."""
    whitelist, leak = _patterns()
    if re.search(whitelist, line, re.IGNORECASE):
        return False
    return bool(re.search(leak, line, re.IGNORECASE))


@pytest.mark.parametrize(
    "line",
    [
        '+    ("/Users/u/dev/m3-memory", False),',
        r'+    (r"C:\Users\u\pipx\venvs\m3-memory", True),',
        '+    ("/home/user/.local/lib/python3.12/site-packages", False),',
        '+    ("/Users/someone/Library/Python/3.12", True),',
    ],
)
def test_anonymized_fixtures_are_allowed(line):
    assert not _blocked(line), f"false alarm on an anonymized fixture: {line}"


@pytest.mark.parametrize(
    "line,why",
    [
        ('+  path = "/Users/realperson/.m3/engine/agent_memory.db"', "real macOS home"),
        (r'+  p = r"C:\Users\realperson\pipx\venvs"', "real Windows home"),
        ('+  home = "/Users/ursula/work"', "near-miss: starts with the placeholder letter"),
        # Assembled at runtime: a literal key-shaped string in this file would
        # (correctly) trip the very scanner under test on every push.
        ('+  KEY = "' + "sk-ant-" + 'abcdefghij0123456789xyz"', "api key"),
        ('+  contact = "realperson@' + 'gmail.com"', "email"),
        ('+  d = "/home/realperson/secrets/"', "real POSIX home"),
    ],
)
def test_real_leaks_are_still_blocked(line, why):
    assert _blocked(line), f"leak slipped through ({why}): {line}"


def test_whitelist_does_not_exclude_test_directories():
    """Excluding tests/ wholesale would be a blind spot: a real credential can
    land in a test file."""
    src = _HOOK.read_text(encoding="utf-8")
    whitelist, _ = _patterns()
    assert "tests/" not in whitelist, (
        "the placeholder whitelist must not exempt whole directories"
    )
    assert "bench" not in whitelist


def test_whitelist_anchors_on_path_segments():
    """Placeholders must match as whole segments, so `ursula` is not forgiven
    for beginning with `u`."""
    whitelist, _ = _patterns()
    assert r"([\\/]|$)" in whitelist, "whitelist must anchor at a path separator"


def test_scanner_selftest_exclusion_is_a_single_file():
    """This file is excluded from the scan by PATH, because a test proving the
    scanner catches home paths must contain home-path-shaped strings.

    That exclusion must stay pinned to this ONE file. Widening it to `tests/`
    would mean a real credential committed in any test file ships unnoticed --
    the blind spot the narrow whitelist was chosen to avoid.
    """
    src = _HOOK.read_text(encoding="utf-8")
    m = re.search(r"SCANNER_SELFTEST='([^']+)'", src)
    assert m, "SCANNER_SELFTEST not found in .githooks/pre-push"
    value = m.group(1)
    assert value == ":(exclude)tests/test_prepush_placeholder_whitelist.py", value
    assert not value.rstrip("/").endswith("tests"), "must not exclude the whole tests/ tree"
