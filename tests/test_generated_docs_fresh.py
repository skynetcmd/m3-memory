"""Drift gate for catalog-derived docs: CAPABILITY_MATRIX.md and features.json.

Both are generated from docs/tools/MCP_CATALOG.json (by gen_capability_matrix.py /
gen_features_json.py). If the catalog changes and someone forgets to regenerate,
these docs silently rot — teaching humans, search engines, and AI agents a stale
tool surface. This test makes "forgot to regenerate" a red build.

On failure: run
    python bin/gen_capability_matrix.py
    python bin/gen_features_json.py
and commit the regenerated files.
"""
from __future__ import annotations

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BIN = os.path.join(_ROOT, "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_capability_matrix_is_fresh(tmp_path, monkeypatch):
    committed = os.path.join(_ROOT, "docs", "CAPABILITY_MATRIX.md")
    assert os.path.exists(committed), "CAPABILITY_MATRIX.md missing — run gen_capability_matrix.py"

    import gen_capability_matrix
    # Redirect the generator's output to a temp file, then compare content.
    fresh = tmp_path / "CAPABILITY_MATRIX.md"
    monkeypatch.setattr(gen_capability_matrix, "OUTPUT", str(fresh))
    gen_capability_matrix.main()

    assert _read(committed) == _read(str(fresh)), (
        "CAPABILITY_MATRIX.md is stale — run `python bin/gen_capability_matrix.py` and commit."
    )


def test_features_json_is_fresh(tmp_path, monkeypatch):
    committed = os.path.join(_ROOT, "docs", "features.json")
    assert os.path.exists(committed), "features.json missing — run gen_features_json.py"

    import gen_features_json
    fresh = tmp_path / "features.json"
    monkeypatch.setattr(gen_features_json, "OUTPUT", str(fresh))
    gen_features_json.main()

    # Compare parsed objects so insignificant whitespace never false-fails.
    assert json.loads(_read(committed)) == json.loads(_read(str(fresh))), (
        "features.json is stale — run `python bin/gen_features_json.py` and commit."
    )


def test_features_json_tool_count_matches_catalog():
    """features.json's tool count must equal the catalog's — a cheap independent
    check that doesn't depend on the generator running."""
    catalog = json.loads(_read(os.path.join(_ROOT, "docs", "tools", "MCP_CATALOG.json")))
    features = json.loads(_read(os.path.join(_ROOT, "docs", "features.json")))
    assert features["tools"]["total"] == len(catalog["tools"]), (
        "features.json tool count != catalog tool count — regenerate features.json."
    )
