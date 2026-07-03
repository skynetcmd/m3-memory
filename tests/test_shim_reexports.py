"""Guard against ruff --fix (or a hand edit) stripping shim re-exports.

The memory_core modularization (§2) is a FACADE: bin/memory/*.py modules
re-export symbols from each other and from memory_core, and downstream code does
`from memory.embed import _augment_embed_text_with_anchors` etc. A re-exported
symbol has no direct caller in its own module, so `ruff --fix`'s unused-import
autofix will silently STRIP it — which breaks the import chain at runtime, not at
lint time. That exact bug shipped live once (commit 4d46b97 dropped
_augment_embed_text_with_anchors + _chunk_for_sliding_window from memory/embed.py
and broke memory_write). This test makes that failure loud and pre-push-catchable.

Approach (self-contained, no baseline snapshot needed): for every `from .sibling
import NAME` in each shim module, assert the module actually exposes NAME after
import. If ruff strips a re-export, the name vanishes and this fails.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# The facade modules whose cross-module re-exports we protect. memory_core is
# the legacy shim; memory/* are the extracted submodules that re-export through
# it. Add a module here when it starts re-exporting sibling symbols.
_SHIM_MODULES = [
    "memory.embed",
    "memory.write",
    "memory.search",
    "memory.entity",
    "memory.chunking",
    "memory.textprep",
    "memory.db",
    "memory.chroma",
    "memory.fts",
    "memory.config",
    "memory_core",
]


def _module_file(mod_name: str) -> Path:
    """Resolve the .py file for a bin/-rooted module without importing it."""
    rel = mod_name.replace(".", "/") + ".py"
    return _BIN / rel


def _relative_import_names(py_file: Path) -> list[tuple[str, str]]:
    """Every MODULE-LEVEL `from .sibling import NAME` -> (module, NAME) pairs.

    Only RELATIVE imports (level >= 1) at MODULE SCOPE are re-exports — a relative
    import nested inside a function/class body is a lazy/local import (used
    internally, deliberately NOT a module attribute), so we must not flag it. We
    therefore iterate `tree.body` (top level) only, not `ast.walk` (all nodes).
    `import *` is skipped; aliased imports bind their asname."""
    src = py_file.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(py_file))
    out: list[tuple[str, str]] = []
    for node in tree.body:  # module scope only — NOT ast.walk (that includes fn bodies)
        if isinstance(node, ast.ImportFrom) and node.level and node.level >= 1:
            for alias in node.names:
                if alias.name == "*":
                    continue
                out.append((node.module or "", alias.asname or alias.name))
    return out


@pytest.mark.parametrize("mod_name", _SHIM_MODULES)
def test_shim_module_imports(mod_name):
    """The facade module itself must import (a stripped/renamed re-export that a
    top-level statement depends on would already fail here)."""
    importlib.import_module(mod_name)


@pytest.mark.parametrize("mod_name", _SHIM_MODULES)
def test_relative_reexports_are_present(mod_name):
    """Every name the module pulls in via `from .sibling import NAME` must be an
    attribute of the imported module — i.e. the re-export actually resolved and
    was not stripped. This is the ruff-strip guard."""
    py_file = _module_file(mod_name)
    if not py_file.exists():
        pytest.skip(f"{py_file} not found")
    mod = importlib.import_module(mod_name)
    expected = _relative_import_names(py_file)
    missing = [name for _submod, name in expected if not hasattr(mod, name)]
    assert not missing, (
        f"{mod_name} declares `from .<sibling> import ...` for {missing}, but "
        f"the imported module does not expose them — a re-export was stripped "
        f"(likely `ruff --fix` on this facade). Restore the import(s) and mark "
        f"the block `# noqa: F401`."
    )


def test_known_critical_reexports():
    """Belt-and-braces: the specific names that broke live (4d46b97) must resolve
    both on their owning module AND through the shim path downstream uses."""
    from memory import embed
    for name in (
        "_augment_embed_text_with_anchors",
        "_chunk_for_sliding_window",
        "_content_hash",
        "MAX_CHARS_PER_CHUNK",
        "STRIDE_CHARS",
    ):
        assert hasattr(embed, name), f"memory.embed lost re-export {name!r}"
    # write.py imports several of these FROM .embed — prove that path works.
    importlib.import_module("memory.write")
