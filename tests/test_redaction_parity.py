"""Byte-exact parity gate: Rust m3_core_rs.scrub vs pure-Python _scrub_python.

If Rust and Python diverge on ANY input, that's a finding to document, not hide.
"""

import importlib
import os
import sys

import pytest

pytest.importorskip("m3_core_rs")
import m3_core_rs  # noqa: E402

_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

# Import the module fresh with the kill-switch forced ON so we get a handle to
# the pure-Python implementation regardless of wheel presence. We then call its
# _scrub_python directly (the Python reference) and compare to m3_core_rs.scrub.
_prev = os.environ.get("M3_CORE_RS_DISABLE")
os.environ["M3_CORE_RS_DISABLE"] = "1"
try:
    if "chatlog_redaction" in sys.modules:
        del sys.modules["chatlog_redaction"]
    import chatlog_redaction as _clr_py  # noqa: E402

    importlib.reload(_clr_py)
finally:
    if _prev is None:
        del os.environ["M3_CORE_RS_DISABLE"]
    else:
        os.environ["M3_CORE_RS_DISABLE"] = _prev

assert _clr_py.m3_core_rs is None, "kill-switch did not force the Python path"


def py_scrub(content, config):
    return _clr_py._scrub_python(content, config)


def rs_scrub(content, config):
    return m3_core_rs.scrub(content, config)


def assert_parity(content, config):
    rs = rs_scrub(content, config)
    py = py_scrub(content, config)
    assert rs == py, f"divergence:\n  input={content!r}\n  config={config}\n  rust={rs!r}\n  py  ={py!r}"
    return rs


# --- fixtures --------------------------------------------------------------

ALL_GROUPS = ["api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens"]

GH = "ghp_" + "a" * 36
ANTH = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"
OPENAI_PROJ = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
XAI = "xai-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
GOOGLE = "AIza" + "B" * 35
AWS_ID = "AKIAIOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF2QT4"
BEARER = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"


def cfg(enabled=True, patterns=None, custom=None, pii=False):
    return {
        "enabled": enabled,
        "patterns": patterns if patterns is not None else [],
        "custom_regex": custom if custom is not None else [],
        "redact_pii": pii,
    }


# --- per-group -------------------------------------------------------------


@pytest.mark.parametrize(
    "group,sample",
    [
        ("api_keys", f"key {ANTH} and {OPENAI_PROJ} and {XAI} and {GOOGLE}"),
        ("bearer_tokens", f"hdr {BEARER}"),
        ("jwt", f"token {JWT} end"),
        ("aws_keys", f'key {AWS_ID} aws_secret_access_key="{"A" * 40}"'),
        ("github_tokens", f"t {GH}"),
    ],
)
def test_each_group(group, sample):
    assert_parity(sample, cfg(patterns=[group]))


def test_all_groups_together():
    content = f"{ANTH} {BEARER} {JWT} {AWS_ID} {GH} a@b.com"
    assert_parity(content, cfg(patterns=ALL_GROUPS))
    assert_parity(content, cfg(patterns=ALL_GROUPS + ["pii"], pii=True))


# --- eval order ------------------------------------------------------------


def test_eval_order_sensitive():
    # sk-ant key: anthropic pattern runs before openai_generic within api_keys.
    assert_parity("sk-ant-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", cfg(patterns=["api_keys"]))
    # a bearer token also contains a base64-ish blob; auth_header before generic.
    assert_parity(BEARER, cfg(patterns=["bearer_tokens"]))
    # custom_regex before pii: a custom pattern that eats an email-shaped token.
    assert_parity(
        "contact admin@corp.com now",
        cfg(patterns=["custom_regex", "pii"], custom=[r"admin@\S+"], pii=True),
    )


def test_marker_not_rematched():
    # the [REDACTED:...] marker must not itself match later patterns.
    content = f"{ANTH} {ANTH} {ANTH}"
    assert_parity(content, cfg(patterns=ALL_GROUPS + ["pii"], pii=True))


# --- PII gating ------------------------------------------------------------


def test_pii_off_by_default():
    assert_parity("mail me a@b.com call 555-123-4567 ssn 123-45-6789", cfg(patterns=["pii"], pii=False))


def test_pii_on():
    assert_parity("mail me a@b.com call 555-123-4567 ssn 123-45-6789", cfg(patterns=["pii"], pii=True))


def test_pii_needs_pattern_entry():
    # redact_pii True but "pii" not in patterns -> inactive.
    assert_parity("a@b.com", cfg(patterns=["api_keys"], pii=True))


# --- custom regex ----------------------------------------------------------


def test_custom_regex():
    assert_parity("MY_SECRET_123 and MY_SECRET_456", cfg(patterns=["custom_regex"], custom=[r"MY_SECRET_\d+"]))


def test_custom_regex_multiple():
    assert_parity(
        "FOO_1 BAR_2 FOO_3",
        cfg(patterns=["custom_regex"], custom=[r"FOO_\d", r"BAR_\d"]),
    )


def test_custom_regex_not_in_patterns():
    # custom_regex supplied but not listed in patterns -> inactive.
    assert_parity("MY_SECRET_123", cfg(patterns=["api_keys"], custom=[r"MY_SECRET_\d+"]))


def test_bad_custom_regex_does_not_crash():
    c = cfg(patterns=["custom_regex"], custom=["[unclosed"])
    assert_parity("irrelevant text", c)
    # both sides should record a compile error
    rs_scrub("irrelevant", c)
    assert m3_core_rs.redaction_compile_errors(), "Rust recorded no compile error"
    py_scrub("irrelevant", c)
    assert _clr_py.get_compile_errors(), "Python recorded no compile error"


def test_mixed_good_bad_custom_regex():
    assert_parity(
        "GOOD_42 here",
        cfg(patterns=["custom_regex"], custom=["[bad", r"GOOD_\d+", "(also(bad"]),
    )


# --- edge cases ------------------------------------------------------------


def test_disabled_config():
    assert_parity(ANTH, cfg(enabled=False))
    assert_parity(ANTH, cfg(enabled=False, patterns=ALL_GROUPS))


def test_empty_string():
    assert_parity("", cfg(patterns=ALL_GROUPS + ["pii"], pii=True))


def test_no_secrets():
    assert_parity("the quick brown fox jumps over the lazy dog", cfg(patterns=ALL_GROUPS + ["pii"], pii=True))


def test_adjacent_secrets():
    assert_parity(f"{ANTH}{GH}", cfg(patterns=ALL_GROUPS))
    assert_parity(f"{ANTH} {GH}", cfg(patterns=ALL_GROUPS))
    assert_parity(f"{AWS_ID}{AWS_ID}", cfg(patterns=["aws_keys"]))


def test_overlapping_shaped_secrets():
    # sk- prefix shared by anthropic / openai_project / openai_generic.
    assert_parity(
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        cfg(patterns=["api_keys"]),
    )


def test_repeated_same_secret():
    assert_parity(" ".join([GH] * 5), cfg(patterns=["github_tokens"]))


def test_missing_config_keys():
    # Python .get() defaults vs Rust dict-read defaults.
    assert_parity(ANTH, {"enabled": True, "patterns": ["api_keys"]})
    assert_parity(ANTH, {"enabled": True})
    assert_parity(ANTH, {})
