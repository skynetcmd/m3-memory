"""The doctor's embed_backfill step must resolve a REAL script path.

Regression: doctor.py (in bin/memory/) built the path as
dirname(dirname(__file__))/"bin"/"embed_backfill.py" — a double "bin" that
pointed at a nonexistent bin/bin/embed_backfill.py, so `m3 doctor --fix` reported
[error] embed_backfill "can't open file". (The companion interpreter fix —
propagating PYTHONPATH so a macOS framework python finds m3's deps — is exercised
live; here we lock the path resolution.)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import doctor  # noqa: E402


def test_embed_backfill_script_resolves_to_a_real_file():
    path = doctor._embed_backfill_script()
    assert path.endswith(os.path.join("bin", "embed_backfill.py")), path
    assert os.path.isfile(path), f"resolved script does not exist: {path}"


def test_embed_backfill_path_has_no_double_bin():
    path = doctor._embed_backfill_script().replace("\\", "/")
    assert "/bin/bin/" not in path, f"double-bin regression: {path}"
