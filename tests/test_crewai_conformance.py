"""CrewAI StorageBackend protocol-conformance gate (needs crewai installed).

Mirrors the storage-backend conformance discipline from the m3 modularity refactor
(tests/test_backend_conformance.py): assert m3's adapter satisfies CrewAI's
``@runtime_checkable StorageBackend`` Protocol, so a future CrewAI protocol change
is caught by OUR CI — not by a user's crashed crew (§3 parity: catch drift in our
own tests). Skips cleanly when crewai is absent (hermetic in default CI).

Scope: STRUCTURAL conformance (the backend IS-A StorageBackend; every method CrewAI
actually calls in 1.15.3 is present and callable-shaped) + a no-DB tenancy check.
The live behavioral round-trip (dual-embed, cross-agent search) is
tests/test_crewai_live.py.
"""

from __future__ import annotations

import os
import sys

import pytest

# Put bin/ on path (some m3 modules import lazily inside the backend). Harmless if
# already there via conftest.
_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_REPO, os.path.join(_REPO, "bin")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

crewai = pytest.importorskip("crewai", reason="requires `pip install crewai`")


def _backend_cls():
    # Import the class directly (bypasses the package __getattr__ version guard,
    # which is separately tested); the class body has no crewai import.
    from m3_memory.integrations.crewai.backend import M3StorageBackend

    return M3StorageBackend


def test_adapter_satisfies_storagebackend_protocol():
    from crewai.memory.storage.backend import StorageBackend

    backend = _backend_cls()(user_id="conformance-tenant")
    # @runtime_checkable: isinstance verifies the method surface is present.
    assert isinstance(backend, StorageBackend), (
        "M3StorageBackend does not satisfy CrewAI's StorageBackend Protocol — a "
        "method is missing or misnamed. If CrewAI changed the protocol, update the "
        "adapter (this test caught the drift, as intended)."
    )


def test_every_called_method_is_present_and_callable():
    # The methods CrewAI 1.15.3 actually invokes (see research memory). count +
    # the async trio are optional/unused but should still be present.
    required = [
        "save", "search", "delete", "update", "get_record", "list_records",
        "get_scope_info", "list_scopes", "list_categories", "reset",
        "touch_records",  # non-protocol, called via getattr after every recall
        "count", "asave", "asearch", "adelete",
    ]
    backend = _backend_cls()(user_id="conformance-tenant")
    for name in required:
        assert hasattr(backend, name), f"missing StorageBackend method: {name}"
        assert callable(getattr(backend, name)), f"{name} is not callable"


def test_touch_records_is_the_getattr_contract_name():
    # CrewAI probes `getattr(storage, "touch_records", None)` — the exact spelling
    # matters (a rename silently disables recency refresh). Pin it.
    backend = _backend_cls()(user_id="conformance-tenant")
    assert getattr(backend, "touch_records", None) is not None


def test_construction_requires_tenant():
    cls = _backend_cls()
    with pytest.raises(ValueError):
        cls(user_id="")


def test_memoryrecord_fields_the_adapter_relies_on_exist():
    # The adapter reads these MemoryRecord fields; if CrewAI renamed one, the
    # mapping breaks. Assert the contract we build against.
    from crewai.memory.types import MemoryRecord

    rec = MemoryRecord(content="x")
    for field in ("id", "content", "scope", "categories", "metadata",
                  "importance", "created_at", "last_accessed", "embedding",
                  "source", "private"):
        assert hasattr(rec, field), f"MemoryRecord lost field the adapter uses: {field}"


def test_scopeinfo_fields_exist():
    from crewai.memory.types import ScopeInfo

    si = ScopeInfo(path="/x", record_count=0, categories=[],
                   oldest_record=None, newest_record=None, child_scopes=[])
    for field in ("path", "record_count", "categories", "oldest_record",
                  "newest_record", "child_scopes"):
        assert hasattr(si, field), f"ScopeInfo lost field: {field}"
