"""
Optional secret-scrubbing for chat log entries.

Scans content with pre-compiled regex patterns for common secret formats
and replaces matches with [REDACTED:<group>]. Disabled by default.
"""

import hashlib
import re
import threading
from typing import Optional

# Module-scope compiled patterns cache
_COMPILED: dict[str, list[re.Pattern]] = {}
_LAST_CONFIG_HASH: Optional[str] = None
_COMPILE_ERRORS: list[str] = []
_COMPILE_LOCK = threading.Lock()


def get_compile_errors() -> list[str]:
    """Return any regex compilation errors from the last compile_patterns() call.
    Empty list if all patterns compiled cleanly."""
    with _COMPILE_LOCK:
        return list(_COMPILE_ERRORS)


def compile_patterns(config: dict) -> None:
    """Explicit warm-up hook; called by chatlog_init and when config changes.
    Idempotent. Populates module-scope compiled regex cache."""
    global _COMPILED, _LAST_CONFIG_HASH, _COMPILE_ERRORS

    enabled = config.get("enabled", False)
    patterns = config.get("patterns", [])
    custom_regex = config.get("custom_regex", [])
    redact_pii = config.get("redact_pii", False)

    # Compute config hash to detect changes
    config_tuple = (enabled, tuple(patterns), tuple(custom_regex), redact_pii)
    config_hash = hashlib.blake2b(
        str(config_tuple).encode(), digest_size=16
    ).hexdigest()

    with _COMPILE_LOCK:
        if _LAST_CONFIG_HASH == config_hash:
            return  # No change, skip recompile

        _LAST_CONFIG_HASH = config_hash
        _COMPILE_ERRORS.clear()
        _COMPILED.clear()

        if not enabled:
            return

        # Define all pattern groups
        pattern_groups = {
            "api_keys": [
                (
                    "anthropic",
                    re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
                ),
                (
                    "openai_project",
                    re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
                ),
                (
                    "openai_generic",
                    re.compile(r"sk-[A-Za-z0-9]{20,}"),
                ),
                (
                    "xai",
                    re.compile(r"xai-[A-Za-z0-9_-]{20,}"),
                ),
                (
                    "google",
                    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
                ),
            ],
            "bearer_tokens": [
                (
                    "auth_header",
                    re.compile(
                        r"(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._~+/=-]{20,}"
                    ),
                ),
                (
                    "bearer_generic",
                    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
                ),
            ],
            "jwt": [
                (
                    "jwt",
                    re.compile(
                        r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
                    ),
                ),
            ],
            "aws_keys": [
                (
                    "access_key_id",
                    re.compile(r"AKIA[0-9A-Z]{16}"),
                ),
                (
                    "secret_access_key",
                    re.compile(
                        r"(?i)aws[_-]?secret[_-]?access[_-]?key[\"'\s:=]+[A-Za-z0-9/+=]{40}"
                    ),
                ),
            ],
            "github_tokens": [
                ("ghp", re.compile(r"ghp_[A-Za-z0-9]{36}")),
                ("gho", re.compile(r"gho_[A-Za-z0-9]{36}")),
                ("ghu", re.compile(r"ghu_[A-Za-z0-9]{36}")),
                ("ghs", re.compile(r"ghs_[A-Za-z0-9]{36}")),
                ("ghr", re.compile(r"ghr_[A-Za-z0-9]{36}")),
            ],
            "pii": [
                (
                    "email",
                    re.compile(
                        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
                    ),
                ),
                (
                    "us_phone",
                    re.compile(
                        r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
                    ),
                ),
                (
                    "ssn",
                    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
                ),
            ],
        }

        # Compile built-in groups that are enabled
        for group_name in patterns:
            if group_name in pattern_groups:
                _COMPILED[group_name] = pattern_groups[group_name]

        # PII only enabled if explicitly in patterns AND redact_pii is True
        if "pii" in patterns and redact_pii:
            _COMPILED["pii"] = pattern_groups["pii"]
        elif "pii" in _COMPILED:
            del _COMPILED["pii"]

        # Compile custom regex patterns
        if "custom_regex" in patterns and custom_regex:
            custom_compiled = []
            for pattern_str in custom_regex:
                try:
                    custom_compiled.append((pattern_str, re.compile(pattern_str)))
                except re.error as e:
                    _COMPILE_ERRORS.append(
                        f"custom_regex compilation error: {pattern_str}: {e}"
                    )
            if custom_compiled:
                _COMPILED["custom_regex"] = custom_compiled


def scrub(content: str, config: dict) -> tuple[str, int, list[str]]:
    """
    Scrub secrets from content.

    Returns (scrubbed_content, match_count, groups_fired).

    `config` is the `redaction` sub-dict of the chat log config:
        {
            "enabled": bool,
            "patterns": ["api_keys", "bearer_tokens", "jwt", "aws_keys", "github_tokens"],
            "custom_regex": ["pattern1", "pattern2", ...],   # user-supplied
            "redact_pii": bool,
            "store_original_hash": bool,   # NOT used here — caller handles hashing
        }

    If config["enabled"] is False, return (content, 0, []) immediately
    without any regex work (hot path).
    """
    if not config.get("enabled", False):
        return (content, 0, [])

    # Ensure patterns are compiled for this config
    compile_patterns(config)

    scrubbed = content
    total_matches = 0
    groups_fired = []

    with _COMPILE_LOCK:
        compiled = dict(_COMPILED)

    # Evaluation order: api_keys → bearer_tokens → jwt → aws_keys → github_tokens → custom_regex → pii
    evaluation_order = [
        "api_keys",
        "bearer_tokens",
        "jwt",
        "aws_keys",
        "github_tokens",
        "custom_regex",
        "pii",
    ]

    for group_name in evaluation_order:
        if group_name not in compiled:
            continue

        group_patterns = compiled[group_name]
        match_count_for_group = [0]

        def make_replacement(group: str):
            def replacement_fn(m):
                match_count_for_group[0] += 1
                return f"[REDACTED:{group}]"
            return replacement_fn

        # Apply each pattern in the group
        for pattern_name, pattern in group_patterns:
            scrubbed = pattern.sub(
                make_replacement(group_name), scrubbed
            )

        if match_count_for_group[0] > 0:
            total_matches += match_count_for_group[0]
            groups_fired.append(group_name)

    return (scrubbed, total_matches, groups_fired)


if __name__ == "__main__":
    # Self-tests
    cfg_off = {
        "enabled": False,
        "patterns": [],
        "custom_regex": [],
        "redact_pii": False,
    }
    result = scrub("sk-ant-foobar12345678901234567890", cfg_off)
    assert result == ("sk-ant-foobar12345678901234567890", 0, [])

    cfg_keys = {
        "enabled": True,
        "patterns": ["api_keys"],
        "custom_regex": [],
        "redact_pii": False,
    }
    scrubbed, n, groups = scrub(
        "here is sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890 keep it safe",
        cfg_keys,
    )
    assert n == 1 and "api_keys" in groups and "[REDACTED:api_keys]" in scrubbed

    cfg_gh = {
        "enabled": True,
        "patterns": ["github_tokens"],
        "custom_regex": [],
        "redact_pii": False,
    }
    _, n, _ = scrub("token: ghp_" + "a" * 36, cfg_gh)
    assert n == 1

    cfg_custom = {
        "enabled": True,
        "patterns": ["custom_regex"],
        "custom_regex": [r"MY_SECRET_\d+"],
        "redact_pii": False,
    }
    _, n, _ = scrub("MY_SECRET_123 and MY_SECRET_456", cfg_custom)
    assert n == 2

    # Bad custom regex doesn't crash
    cfg_bad = {
        "enabled": True,
        "patterns": ["custom_regex"],
        "custom_regex": ["[unclosed"],
        "redact_pii": False,
    }
    scrub("irrelevant", cfg_bad)
    assert get_compile_errors(), "expected a compile error to be recorded"

    print("chatlog_redaction.py self-tests passed")
