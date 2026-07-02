"""DEPRECATED_ENV_RENAMES must stay in sync with the getenv_compat call sites.

The static map in m3_core.paths is the source of truth for the on-disk
env-migration helper (m3 doctor scans config for the OLD names, --fix rewrites
them). If a new getenv_compat(new, old) call is added without a map entry, the
helper would silently miss that var. This test greps every call site and asserts
the map covers exactly them — so the two can't drift.
"""
from __future__ import annotations

import re
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin"
_CALL = re.compile(r'getenv_compat\(\s*"(M3_[A-Z0-9_]+)"\s*,\s*"([A-Z0-9_]+)"')


def _call_site_pairs() -> dict[str, str]:
    """{old_name: new_name} from every getenv_compat(new, old) call under bin/."""
    pairs: dict[str, str] = {}
    for py in _BIN.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        for new_name, old_name in _CALL.findall(py.read_text(encoding="utf-8", errors="ignore")):
            pairs[old_name] = new_name
    return pairs


def test_rename_map_matches_call_sites():
    import sys
    sys.path.insert(0, str(_BIN))
    from m3_core.paths import DEPRECATED_ENV_RENAMES

    call_sites = _call_site_pairs()
    assert call_sites, "no getenv_compat call sites found — regex or layout changed"

    missing = {o: n for o, n in call_sites.items() if o not in DEPRECATED_ENV_RENAMES}
    assert not missing, f"getenv_compat call sites not in DEPRECATED_ENV_RENAMES: {missing}"

    extra = {o: n for o, n in DEPRECATED_ENV_RENAMES.items() if o not in call_sites}
    assert not extra, f"DEPRECATED_ENV_RENAMES has entries with no call site: {extra}"

    # And the new-name mapping must agree where both know it.
    for old, new in call_sites.items():
        assert DEPRECATED_ENV_RENAMES[old] == new, (
            f"{old}: call site says -> {new}, map says -> {DEPRECATED_ENV_RENAMES[old]}"
        )


def test_rename_map_is_pure_namespacing():
    import sys
    sys.path.insert(0, str(_BIN))
    from m3_core.paths import DEPRECATED_ENV_RENAMES

    # Every new name is the old name with an M3_ prefix (the migration's rule).
    for old, new in DEPRECATED_ENV_RENAMES.items():
        assert new == f"M3_{old}", f"{old} -> {new} is not pure M3_ namespacing"
