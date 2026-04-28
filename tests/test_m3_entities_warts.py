"""Unit tests for the three entity-extraction warts patched in bin/m3_entities.py:

(a) Generic etype-prefix strip (`file_path_*`, `memory_id_*`, ...)
(b) Anti-extraction list drops 'Windows 11 Pro' typed as 'host'
(c) Bare module identifiers misfiled as 'file_path' are reclassified to 'module'

Imports the module-level helpers directly; no DB / HTTP needed.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_entities  # noqa: E402


def test_strip_etype_prefix_file_path():
    assert m3_entities._strip_etype_prefix(
        "file_path", "file_path_bin_memory_core.py"
    ) == "bin_memory_core.py"
    # Existing memory_id_ wart still handled by the same helper.
    assert m3_entities._strip_etype_prefix(
        "memory_id", "memory_id_abc12345"
    ) == "abc12345"
    # No-op when no prefix present.
    assert m3_entities._strip_etype_prefix("module", "memory_core") == "memory_core"


def test_anti_extraction_list_filters_windows_11_pro():
    assert "Windows 11 Pro" in m3_entities.ANTI_EXTRACTION_NAMES


def test_module_reclassification_from_file_path():
    # Bare identifier with no slash or extension → module.
    assert m3_entities._maybe_reclassify_module("file_path", "memory_core") == "module"
    # Dotted module (no slash, no file extension) → module.
    assert m3_entities._maybe_reclassify_module("file_path", "pkg.sub") == "module"
    # Real file path stays file_path.
    assert m3_entities._maybe_reclassify_module(
        "file_path", "bin/memory_core.py"
    ) == "file_path"
    # File with extension stays file_path.
    assert m3_entities._maybe_reclassify_module("file_path", "foo.py") == "file_path"
    # Non-file_path types are not touched.
    assert m3_entities._maybe_reclassify_module("tool", "memory_core") == "tool"
