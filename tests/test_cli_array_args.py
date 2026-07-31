"""`m3 <domain> <tool> --arrayflag` must produce a list, not a string.

Array-of-scalar tool arguments fell through to argparse's default string type,
so the tool layer iterated the value CHARACTER BY CHARACTER:

    m3 chat chatlog_set_redaction --patterns "api_keys,jwt"
    -> patterns = ['a', 'p', 'i', '_', 'k', 'e', 'y', 's', ',', 'j', ...]

and it SAVED that, reporting success. Repeating the flag overwrote instead of
appending, so there was no working way to pass several values at all. Observed
2026-07-31 while enabling chatlog redaction: the config persisted 49 one-letter
"pattern groups", none of which match anything — redaction was configured and
silently inert.
"""
from __future__ import annotations

import argparse

import pytest

from m3_memory.cli import _CommaSplitAppend

GROUPS = ["api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens"]


def _parse(argv: list[str]) -> list[str] | None:
    p = argparse.ArgumentParser()
    p.add_argument("--patterns", dest="patterns", nargs="+",
                   action=_CommaSplitAppend, default=None)
    return p.parse_args(argv).patterns


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--patterns", "api_keys,jwt"], ["api_keys", "jwt"]),
        (["--patterns", "api_keys", "jwt"], ["api_keys", "jwt"]),
        (["--patterns", "api_keys", "--patterns", "jwt"], ["api_keys", "jwt"]),
        (["--patterns", "api_keys,jwt", "--patterns", "aws_keys"],
         ["api_keys", "jwt", "aws_keys"]),
        (["--patterns", ",".join(GROUPS)], GROUPS),
        (["--patterns", *GROUPS], GROUPS),
    ],
)
def test_forms_all_yield_the_same_list(argv, expected):
    assert _parse(argv) == expected


def test_never_character_splits():
    """The actual bug: every element must be a real group name."""
    got = _parse(["--patterns", ",".join(GROUPS)])
    assert all(len(item) > 1 for item in got), f"character-split: {got}"
    assert set(got) == set(GROUPS)


def test_repeats_accumulate_rather_than_overwrite():
    """Second half of the bug — last-flag-wins silently dropped earlier values."""
    got = _parse(["--patterns", "api_keys", "--patterns", "jwt",
                  "--patterns", "aws_keys"])
    assert got == ["api_keys", "jwt", "aws_keys"]


@pytest.mark.parametrize("raw", ["api_keys,,jwt", "api_keys,jwt,", ",api_keys,jwt"])
def test_empty_segments_dropped(raw):
    assert _parse(["--patterns", raw]) == ["api_keys", "jwt"]


def test_absent_flag_stays_none():
    """None must survive so the tool layer can tell 'unset' from 'empty'."""
    assert _parse([]) is None


def test_single_value_is_still_a_list():
    assert _parse(["--patterns", "api_keys"]) == ["api_keys"]


def test_array_tools_are_registered_with_the_split_action():
    """Guard the WIRING: a future refactor must not drop the action and
    silently reintroduce character-splitting."""
    pytest.importorskip("mcp_tool_catalog")
    import mcp_tool_catalog as cat

    from m3_memory.cli import _spec_is_complex

    spec = next((t for t in cat.TOOLS if t.name == "chatlog_set_redaction"), None)
    if spec is None:
        pytest.skip("chatlog_set_redaction not in this catalog build")
    assert not _spec_is_complex(spec), \
        "flat-flag tool became complex; array handling moved to --json"
    assert spec.parameters["properties"]["patterns"]["type"] == "array"
