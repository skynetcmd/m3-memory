"""Procedural memory — the `procedure` head type + procedure_kind facet.

Covers:
  - `procedure` is a valid, shipping memory type.
  - The `enrich.py` HARDCODED duplicate of the valid-type set stays in parity
    with spec.py's VALID_MEMORY_TYPES (minus the `auto` sentinel). This closes
    the class of drift the audit found, where `belief` had silently fallen out
    of the enrich copy. If this test fails, sync bin/memory/enrich.py's
    `valid_types` set with bin/catalog/spec.py's VALID_MEMORY_TYPES.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def test_procedure_is_a_valid_memory_type():
    import mcp_tool_catalog
    assert "procedure" in mcp_tool_catalog.VALID_MEMORY_TYPES


def test_enrich_valid_types_matches_spec():
    """The enrich._auto_classify duplicate type-set must equal spec.py's
    VALID_MEMORY_TYPES minus 'auto' (the classification-request sentinel)."""
    import inspect
    import re

    import mcp_tool_catalog
    from memory import enrich

    spec_types = set(mcp_tool_catalog.VALID_MEMORY_TYPES) - {"auto"}

    # Extract the literal set assigned to `valid_types` in _auto_classify.
    src = inspect.getsource(enrich._auto_classify)
    m = re.search(r"valid_types\s*=\s*\{(.*?)\}", src, re.DOTALL)
    assert m, "could not locate the valid_types literal in enrich._auto_classify"
    enrich_types = set(re.findall(r'"([^"]+)"', m.group(1)))

    missing = spec_types - enrich_types
    extra = enrich_types - spec_types
    assert not missing, f"enrich.py valid_types is MISSING types present in spec: {missing}"
    assert not extra, f"enrich.py valid_types has EXTRA types not in spec: {extra}"


def test_procedure_kind_constants():
    """The engine's soft-validated sub-kinds are the four shipping kinds."""
    import memory_maintenance
    assert memory_maintenance.VALID_PROCEDURE_KINDS == {
        "skill", "runbook", "how_to", "checklist"
    }
