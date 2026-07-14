"""Hermetic logic tests for the LangChain integration — NO live m3, NO live
LangChain (mirrors the hermes ``test_provider_logic.py`` pattern).

Exercises the pure mapping + shaping logic that has no I/O: the mem0 field-name
rename, the metadata_json split/merge round-trip, the string-return parsers
(``memory_get`` / write-id / ``chatlog_search``), and the tenancy/normalization
helpers. The live round-trip, isolation, and gate behaviors are covered by the
live integration test (needs a real DB) — see ``tests/test_langchain_live.py``.

Run: ``python m3_memory/integrations/langchain/test_provider_logic.py``
"""

from __future__ import annotations

import os
import sys

# Allow running as a bare script: put the repo root (four levels up:
# m3_memory/integrations/langchain/test_provider_logic.py) on sys.path so
# `import m3_memory...` resolves without an editable install.
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _c(cond: bool, msg: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        _c.failed = True  # type: ignore[attr-defined]


_c.failed = False  # type: ignore[attr-defined]


def test_mapping() -> None:
    from m3_memory.integrations.langchain import mapping

    print("mapping.to_mem0_result — field rename + temporal metadata")
    item = {"id": "x1", "content": "hi", "confidence": 0.8,
            "valid_from": "2026-01-01", "valid_to": "", "metadata_json": '{"k":"v"}'}
    r = mapping.to_mem0_result(0.9, item)
    _c(r["memory"] == "hi", "content mapped to 'memory'")
    _c("content" not in r, "'content' key NOT present (mem0 trap)")
    _c(r["id"] == "x1" and r["score"] == 0.9, "id + score carried")
    _c(r["metadata"].get("confidence") == 0.8, "confidence in metadata")
    _c(r["metadata"].get("valid_from") == "2026-01-01", "valid_from in metadata")
    _c("valid_to" not in r["metadata"], "empty valid_to omitted")
    _c(r["metadata"].get("k") == "v", "user metadata_json merged")

    print("mapping.split_value / merge_value — round-trip")
    val = {"content": "body", "tag": "a", "n": 3}
    content, md = mapping.split_value(val)
    _c(content == "body", "content extracted")
    _c(md == {"tag": "a", "n": 3}, "non-content keys -> metadata")
    merged = mapping.merge_value({"content": content,
                                  "metadata_json": '{"tag":"a","n":3}'})
    _c(merged["content"] == "body" and merged["tag"] == "a",
       "merge_value reconstructs value")

    print("mapping.parse_get — JSON string / not-found sentinel")
    _c(mapping.parse_get('{"id":"a","content":"c"}')["content"] == "c",
       "JSON string parsed")
    _c(mapping.parse_get("Error: not found") is None, "not-found -> None")
    _c(mapping.parse_get(None) is None, "None -> None")

    print("mapping.parse_written_id — 'Created:' / 'Superseded' / error / suffix")
    u1 = "3f3a44a1-06d9-462b-8c43-7c2a007063c8"
    u2 = "09ae9bf0-bdc7-4d0c-b00b-2c6bf49c475b"
    _c(mapping.parse_written_id("Created: " + u1) == u1,
       "Created: <uuid> extracted")
    # the fresh-write suffix must NOT contaminate the id (the real bug we hit)
    _c(mapping.parse_written_id(
        f"Created: {u1} (embedding deferred — searchable now via FTS)") == u1,
       "trailing suffix stripped")
    _c(mapping.parse_written_id(
        f"Superseded {u2} -> Created: {u1}") == u1,
       "Superseded tail extracted (not the old id)")
    _c(mapping.parse_written_id("Error: too large") is None, "error -> None")

    print("mapping.parse_chatlog_search — JSON string envelope")
    rows = mapping.parse_chatlog_search('{"results":[{"content":"t"}],"count":1}')
    _c(len(rows) == 1 and rows[0]["content"] == "t", "results extracted")
    _c(mapping.parse_chatlog_search("") == [], "empty -> []")


def test_message_normalization() -> None:
    from m3_memory.integrations.langchain.mem0_compat import _msg_text, _normalize_messages

    print("mem0 message normalization — str / dict / list")
    _c(_normalize_messages("hi") == [{"role": "user", "content": "hi"}],
       "bare string")
    _c(_normalize_messages({"role": "assistant", "content": "yo"})
       == [{"role": "assistant", "content": "yo"}], "single dict")
    lst = _normalize_messages(["a", {"role": "user", "content": "b"}])
    _c(len(lst) == 2 and lst[0]["content"] == "a", "mixed list")
    _c(_msg_text({"content": "x"}) == "x", "content field read")
    _c(_msg_text({"text": "y"}) == "y", "text fallback read")


def test_tenancy_helper() -> None:
    # _require_user is a pure method; test it without touching the DB by using a
    # bare instance (M3Client construction is deferred until first call).
    from m3_memory.integrations.langchain.mem0_compat import Memory

    print("tenancy — _require_user fail-loud")
    m = Memory.__new__(Memory)  # no __init__, no client/DB
    m._default_user_id = None
    try:
        m._require_user(None)
        _c(False, "missing user_id should raise")
    except ValueError:
        _c(True, "missing user_id raises ValueError")
    _c(m._require_user("alex") == "alex", "explicit user_id honored")
    m._default_user_id = "default_u"
    _c(m._require_user(None) == "default_u", "constructor default honored")


def main() -> int:
    for t in (test_mapping, test_message_normalization, test_tenancy_helper):
        t()
    if getattr(_c, "failed", False):
        print("\nFAILED")
        return 1
    print("\nALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
