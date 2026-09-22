"""Public API parity oracle for the memory_core modularization migration.

Captures every previously-public symbol of `bin/memory_core.py` (name,
signature, type, value-for-constants) into a JSON fixture under
.scratch/migration_baseline/memory_core_api_snapshot.json.

On first run (or when M3_MIGRATION_REFRESH_SNAPSHOT=1), writes the
snapshot. On subsequent runs, compares the current API against the
saved snapshot and fails if any public symbol changed.

The migration must keep this test green at every phase boundary.

What we DON'T snapshot (intentionally):
- Function bodies (they will change as we split modules).
- Closure cells (private state).
- Module-level mutable state (counters, caches) — these are runtime,
  not API.

What we DO snapshot:
- Set of names exported from `memory_core`.
- Whether each is a function, async function, class, or constant.
- Signature for callables (parameters + defaults + return annotation).
- Value (or repr) for constants.
- Module docstring presence.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import sys
import unittest
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN_DIR))

# COMMITTED, next to the test that owns it — the snapshot-testing norm (jest:
# "commit snapshots and review them as part of your regular code review
# process"; syrupy: "the __snapshots__ directory and all its children should be
# committed along with your test code"). It previously lived under the
# gitignored .scratch/, which meant a fresh clone or a CI runner had no baseline
# and the test seeded its own — passing without checking anything.
SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "memory_core_api_snapshot.json"


def _classify_symbol(obj) -> str:
    """Return a stable label for what kind of symbol this is.

    `functools._lru_cache_wrapper` instances (produced by `@lru_cache`)
    fail `inspect.isfunction` but ARE callable wrappers around a real
    function. Treat them as functions so adding `@lru_cache` to an
    existing helper doesn't trip parity as a function -> constant drift.
    """
    if inspect.iscoroutinefunction(obj):
        return "async_function"
    if inspect.isfunction(obj):
        return "function"
    # lru_cache wraps the original in functools._lru_cache_wrapper.
    # Detect via the canonical attribute `__wrapped__` + callable + non-class
    # rather than importing the private class, which moves across versions.
    if callable(obj) and hasattr(obj, "__wrapped__") and not inspect.isclass(obj):
        wrapped = obj.__wrapped__
        if inspect.iscoroutinefunction(wrapped):
            return "async_function"
        if inspect.isfunction(wrapped):
            return "function"
    if inspect.isclass(obj):
        return "class"
    if inspect.isbuiltin(obj):
        return "builtin"
    if inspect.ismethod(obj):
        return "method"
    if inspect.ismodule(obj):
        return "module"
    return "constant"


def _canonical_annotation(text: str) -> str:
    """Render `Optional[X]` / `Union[...]` the same on every Python version.

    Python 3.14 renders a `typing.Optional[str]` annotation as `str | None`,
    while 3.11/3.12 render it literally as `Optional[str]` — the SAME source
    annotation, two different reprs. Snapshotting the raw string therefore makes
    the baseline disagree with itself across the CI matrix (dev box on 3.14, CI
    on 3.11 + 3.12) even though nothing about the API changed.

    Normalise toward the PEP 604 form, which is what the code actually writes.
    """
    text = re.sub(r"\btyping\.", "", text)     # `typing.Optional` -> `Optional`

    def _split_top(inner: str) -> "list[str]":
        """Split on commas that are NOT inside nested brackets."""
        parts, depth, buf = [], 0, []
        for ch in inner:
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(buf).strip()); buf = []
            else:
                buf.append(ch)
        if buf:
            parts.append("".join(buf).strip())
        return parts

    def _rewrite(name: str, text: str) -> str:
        """Replace the FIRST `name[...]`, matching its bracket properly.

        A plain regex cannot do this: `Optional[tuple[str, str]]` has nested
        brackets, so `[^][]*` never matches and the substitution silently
        no-ops — which is exactly how the first attempt at this passed locally
        and still failed on the CI matrix.
        """
        i = text.find(name + "[")
        if i < 0:
            return text
        start = i + len(name)
        depth = 0
        for j in range(start, len(text)):
            if text[j] == "[":
                depth += 1
            elif text[j] == "]":
                depth -= 1
                if depth == 0:
                    inner = text[start + 1:j]
                    parts = _split_top(inner)
                    if name == "Optional":
                        repl = f"{inner.strip()} | None"
                    else:
                        repl = " | ".join(parts)
                    return text[:i] + repl + text[j + 1:]
        return text

    prev = None
    while prev != text:                       # nested: Optional[Union[a, b]]
        prev = text
        text = _rewrite("Optional", text)
        text = _rewrite("Union", text)
    return text


def _defining_file(obj) -> "str | None":
    """Absolute path of the module that DEFINED `obj`, or None if unknowable."""
    mod_name = getattr(obj, "__module__", None)
    if not isinstance(mod_name, str) or not mod_name:
        return None
    mod = sys.modules.get(mod_name)
    path = getattr(mod, "__file__", None) if mod is not None else None
    return os.path.abspath(path) if path else None


def _is_foreign(obj) -> bool:
    """True when `obj` was defined outside m3, so its shape is not ours to pin.

    WHY THIS EXISTS. The oracle guards the memory_core modularization: it must
    catch OUR refactor changing OUR public API. `memory_core.py:58` does
    `from datetime import datetime, timezone`, so stdlib objects land in
    `dir(memory_core)` and get snapshotted as if they were m3's API. m3's
    migration cannot change them -- but CPython can, and did:

        inspect.signature(datetime)  3.12 -> ValueError (no __text_signature__)
                                     3.15 -> (year, month, day, hour=0, ...)

    CPython converted `datetime` to Argument Clinic, so all five datetime types
    gained `__text_signature__`. The snapshot dutifully recorded a "public API
    change" that m3 had nothing to do with, failing this gate on 3.15.

    That is the THIRD instance of this pattern, not a one-off: `_ENV_DEPENDENT_NAMES`
    already hand-excludes `_ThreadLock` (`threading.Lock` changed from builtin
    factory to class in 3.14) and `m3_core_rs`. There are 36 such borrowed
    symbols in `dir(memory_core)` today -- typing generics, compiled regexes,
    locks, semaphores -- each a tripwire for the next interpreter bump. A rule
    retires the list.

    ⚠ NAMES ARE STILL TRACKED. Only the SIGNATURE is skipped. A refactor that
    drops a re-export is a real break and must still fail, so `kind` and
    `value_repr` are captured as before -- this mirrors how modules are already
    handled ("tracked separately ... but not against signatures").

    ⚠ OWNERSHIP IS DECIDED BY FILE LOCATION, NOT BY MODULE-NAME PREFIX. A first
    version of this used a prefix allowlist ("memory", "m3_", ...) and
    misclassified FIVE real m3 functions as foreign -- `_pack`, `_unpack`,
    `_infer_change_agent_util` (bin/embedding_utils.py), `get_best_llm`,
    `apply_thinking_suppression` (bin/llm_failover.py) -- because m3's own
    top-level `bin/` modules carry no shared prefix. Silently dropping coverage
    for real API is worse than the bug this fixes. Asking where the defining
    module's FILE lives cannot be fooled by a naming convention.
    """
    path = _defining_file(obj)
    if path is None:
        # Unknown provenance (C builtins with no module entry, constants whose
        # __module__ is None). Treat as ours and keep the coverage: a false
        # "ours" costs a possible version-flip, a false "foreign" costs real
        # API coverage, and the second is the worse error.
        return False
    return not path.startswith(str(BIN_DIR))


def _capture_signature(obj) -> str | None:
    """Return string form of the call signature, or None when not applicable.

    None for non-callables, for foreign objects (see :func:`_is_foreign`), and
    for callables whose signature the interpreter will not report.
    """
    if not callable(obj):
        return None
    if _is_foreign(obj):
        return None
    try:
        sig = inspect.signature(obj)
        return _canonical_annotation(str(sig))
    except (TypeError, ValueError):
        return None


# Machine-specific path fragments, longest-first so the most specific wins.
# Built at call time because tmp_path and the engine root move per run/box.
def _volatile_substitutions() -> "list[tuple[str, str]]":
    """(literal, placeholder) pairs that make a value_repr machine-independent."""
    import tempfile

    subs = []
    for env in ("M3_ENGINE_ROOT", "M3_CONFIG_ROOT", "M3_MEMORY_ROOT"):
        v = os.environ.get(env)
        if v:
            subs.append((v, f"<{env}>"))
    subs.append((str(Path(__file__).resolve().parents[1]), "<REPO_ROOT>"))
    subs.append((tempfile.gettempdir(), "<TMP>"))
    subs.append((str(Path.home()), "<HOME>"))
    # Longest first: <REPO_ROOT> must win over <HOME> when nested under it.
    subs.sort(key=lambda kv: len(kv[0]), reverse=True)
    return subs


def _normalize_value(text: str) -> str:
    """Replace machine-specific paths in a repr with stable placeholders.

    Snapshot testing's standard answer to generated/environment values — jest
    calls these "property matchers", syrupy calls them matchers/serializers.
    Without it four constants (BASE_DIR, DB_PATH, ARCHIVE_DB_PATH,
    DEFAULT_ENTITY_VOCAB_YAML) bake in an absolute path AND a per-run pytest
    tmpdir, which (a) leaks the developer's username and (b) makes the snapshet
    diff on every machine and every run — so it could never be committed, and an
    uncommitted snapshot means CI silently seeds instead of enforcing.

    Both separator styles are handled, because a repr may carry the path with
    escaped backslashes (as `str` reprs do on Windows) or with forward slashes
    (as `WindowsPath` reprs do).
    """
    for literal, placeholder in _volatile_substitutions():
        if not literal:
            continue
        fwd = literal.replace("\\", "/")
        esc = literal.replace("\\", "\\\\")
        for variant in (esc, literal, fwd):
            if variant and variant in text:
                text = text.replace(variant, placeholder)
    # pytest's per-run tmpdir counter (…/pytest-3122/…) survives the <TMP>
    # substitution on some platforms; collapse it so runs compare equal.
    text = re.sub(r"pytest-of-[^/\\\\'\"]+", "pytest-of-<USER>", text)
    text = re.sub(r"pytest-\d+", "pytest-<N>", text)
    return text


_ADDRESS_RE = re.compile(r"0x[0-9A-Fa-f]{6,}")


def _canonical_repr(obj) -> str:
    """`repr(obj)` with unordered containers put in a deterministic order.

    WHY (found 2026-09-21). `set`/`frozenset` reprs follow iteration order,
    which depends on string hashing and so on `PYTHONHASHSEED` -- randomized per
    process. Measured on unmodified code, two consecutive runs produced

        frozenset({'they', 'it', 'this', 'okay', ...})
        frozenset({'it', 'they', 'hi', 'yes', ...})

    for the same constant, and nine constants are frozensets
    (`ENTITY_SEED_STOPLIST`, `TERMINAL_TASK_STATES`, `VALID_SCOPES`, ...).

    ⚠ THIS IS NOT CURRENTLY A FAILING GATE, and the distinction matters: the
    comparison in `test_public_api_matches_snapshot` checks `kind` and
    `signature` ONLY -- `value_repr` is captured and deliberately not asserted
    (see the comment there about module-level mutable state). So unordered
    reprs could not fail a run. What they DID do is make the committed snapshot
    churn on every reseed and bake ten raw memory addresses
    (`<asyncio.locks.Semaphore object at 0x...>`) into a tracked file.

    Sorting makes the captured value a function of CONTENT, so a reseed produces
    a clean diff and the field becomes trustworthy if it is ever compared. Which
    is the open item: the module docstring promises "Value (or repr) for
    constants" is snapshotted, and nothing enforces it -- a declared capability
    with no enforcement site (DESIGN §3). Turning it on is a separate change; it
    needs the volatile entries (semaphores, locks, caches) classified first.

    Dict order is NOT sorted: dicts preserve insertion order by language
    guarantee, and that order is part of the API a refactor could change.
    """
    if isinstance(obj, (set, frozenset)):
        kind = "frozenset" if isinstance(obj, frozenset) else "set"
        try:
            items = sorted(obj, key=lambda x: (type(x).__name__, repr(x)))
        except Exception:  # noqa: BLE001 -- unorderable: fall back to repr order
            items = list(obj)
        inner = ", ".join(_canonical_repr(i) for i in items)
        return f"{kind}({{{inner}}})" if items else f"{kind}()"
    if isinstance(obj, (list, tuple)):
        inner = ", ".join(_canonical_repr(i) for i in obj)
        if isinstance(obj, tuple):
            return f"({inner},)" if len(obj) == 1 else f"({inner})"
        return f"[{inner}]"
    if isinstance(obj, dict):
        # KEY order is preserved (insertion order is a language guarantee and
        # part of the API); VALUES are canonicalised, because a dict of sets is
        # otherwise still hash-ordered inside. `TASK_STATE_TRANSITIONS` maps each
        # state to a set of successors and was the last entry that varied
        # between runs after the set branch above was added.
        inner = ", ".join(
            f"{_canonical_repr(k)}: {_canonical_repr(v)}" for k, v in obj.items()
        )
        return f"{{{inner}}}"
    # Default `object.__repr__` embeds the instance's memory ADDRESS, which is
    # different on every run: ten entries here are locks and semaphores whose
    # repr is `<asyncio.locks.Semaphore object at 0x000001D43D89B390 [...]>`.
    # Baking those into a committed file guarantees a diff on every reseed for
    # no information -- the address says nothing about the API. Replace it with
    # a placeholder and keep the rest of the repr, which is the part that
    # carries meaning (`[unlocked, value:4]`).
    return _ADDRESS_RE.sub("0x<ADDR>", repr(obj))


def _capture_value(obj) -> str | None:
    """For constants, capture a stable, machine-independent repr.

    None for callables/classes."""
    if callable(obj) or inspect.isclass(obj) or inspect.ismodule(obj):
        return None
    try:
        return _normalize_value(_canonical_repr(obj))
    except Exception:
        return "<unrepr>"


# Names whose presence/kind is environment-dependent, not a stable API contract,
# so they must not be snapshotted. `m3_core_rs` is the optional native extension:
# it's a module when the wheel is installed and `None` when absent or disabled
# via M3_CORE_RS_DISABLE — so its snapshot "kind" flips module<->constant between
# a dev box (ext present) and CI (ext absent), which is not an API change.
#
# `_ThreadLock` is `threading.Lock`, which the stdlib changed shape on: it is a
# builtin factory function through 3.12 and a real class from 3.14, so its
# snapshot "kind" flips builtin<->class purely by interpreter version (dev box
# on 3.14, CI on 3.11/3.12). Also not an API change of ours.
_ENV_DEPENDENT_NAMES = frozenset({"m3_core_rs", "_ThreadLock"})


def _is_public(name: str) -> bool:
    """A symbol is 'public' for our purposes if it doesn't start with __
    AND it's not an obvious re-exported standard-library or third-party module."""
    if name.startswith("__"):
        return False
    if name in _ENV_DEPENDENT_NAMES:
        return False
    return True


def _build_snapshot():
    """Import memory_core and walk its FULL public surface. Returns a JSON-safe dict.

    memory_core re-exports most of its public API lazily via a PEP-562
    `__getattr__` + `_LAZY_IMPORTS` table (see bin/memory_core.py). Those names
    are NOT in `vars(memory_core)` until first accessed, but they ARE reported
    by `dir()` (the module defines `__dir__`). Walking `vars()` alone therefore
    captured only the ~83 eagerly-bound symbols and missed ~230 lazy ones —
    making the snapshot depend on whatever happened to be materialized by import
    order, which is exactly the non-determinism this parity test must avoid.

    Build the name set from `dir()` and resolve each via `getattr` so every lazy
    re-export is materialized and captured deterministically.
    """
    import memory_core  # noqa: F401 — picked up by the snapshot
    symbols = {}
    for name in dir(memory_core):
        if not _is_public(name):
            continue
        try:
            obj = getattr(memory_core, name)
        except AttributeError:
            # A name advertised by __dir__ but not resolvable — record as absent
            # so a genuine break still shows up rather than silently vanishing.
            symbols[name] = {"kind": "unresolved", "signature": None, "value_repr": None}
            continue
        # Skip standard-library and third-party modules that memory_core
        # imports into its namespace. We only care about its OWN symbols
        # and symbols it explicitly re-exports.
        if inspect.ismodule(obj):
            # Modules are tracked separately to detect "did the import surface
            # change" but not against signatures.
            symbols[name] = {"kind": "module", "module_name": getattr(obj, "__name__", "?")}
            continue
        symbols[name] = {
            "kind": _classify_symbol(obj),
            "signature": _capture_signature(obj),
            "value_repr": _capture_value(obj),
        }
    return {
        "module": "memory_core",
        "n_symbols": len(symbols),
        "module_doc_present": bool(getattr(__import__("memory_core"), "__doc__", None)),
        "symbols": symbols,
    }


class MemoryCoreParityTests(unittest.TestCase):
    """Snapshot-driven parity. The fixture lives outside `tests/` so it
    doesn't end up in committed test data."""

    def test_public_api_matches_snapshot(self):
        current = _build_snapshot()

        refresh = os.environ.get("M3_MIGRATION_REFRESH_SNAPSHOT")

        # Never auto-seed on CI. A freshly-written snapshot always matches
        # itself, so seeding there means the test passes without ever enforcing
        # anything — on every push, forever, because each runner is a clean
        # clone. jest disables snapshot auto-write under CI for exactly this
        # reason ("since new snapshots automatically pass, they should not pass
        # a test run on a CI system"). Fail loudly instead (DESIGN §3).
        if not SNAPSHOT_PATH.exists() and os.environ.get("CI") and not refresh:
            self.fail(
                f"Parity snapshot missing at {SNAPSHOT_PATH}. It is committed to "
                "the repo — a CI run must never generate its own baseline, or the "
                "test silently passes without checking anything. If the public API "
                "changed on purpose, regenerate locally with "
                "M3_MIGRATION_REFRESH_SNAPSHOT=1 and commit the result."
            )

        if not SNAPSHOT_PATH.exists() or refresh:
            SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
            SNAPSHOT_PATH.write_text(json.dumps(current, indent=2, sort_keys=True))
            self.skipTest(
                f"Snapshot written to {SNAPSHOT_PATH}. Re-run without "
                "M3_MIGRATION_REFRESH_SNAPSHOT to enforce parity."
            )

        saved = json.loads(SNAPSHOT_PATH.read_text())

        # Compare structurally so the failure messages point at the
        # specific symbol that changed.
        cur_names = set(current["symbols"])
        old_names = set(saved["symbols"])

        added = cur_names - old_names
        removed = old_names - cur_names
        self.assertFalse(
            removed,
            f"public API symbols REMOVED (would break callers): {sorted(removed)}"
        )
        if added:
            # Adding new public symbols is fine; the migration may legitimately
            # expose a helper that was always there but didn't appear in the
            # baseline. Print but don't fail.
            print(f"  NOTE: new public symbols added: {sorted(added)}")

        # For symbols that exist in both, compare their stable shape.
        mismatches = []
        for name in sorted(cur_names & old_names):
            c = current["symbols"][name]
            s = saved["symbols"][name]
            if c.get("kind") != s.get("kind"):
                mismatches.append(f"{name}: kind {s['kind']} -> {c['kind']}")
            if c.get("signature") != s.get("signature"):
                mismatches.append(
                    f"{name}: signature changed\n    was: {s.get('signature')}\n    now: {c.get('signature')}"
                )
            # Constant value drift: only flag for things we can compare.
            # Module-level mutable state (counters, caches) will have
            # different reprs at different times; we skip those by allowing
            # value_repr to drift WITHIN A KIND (which is why kind drift is
            # checked separately above).
        self.assertFalse(
            mismatches,
            "public API changed:\n  " + "\n  ".join(mismatches)
        )


if __name__ == "__main__":
    unittest.main()
