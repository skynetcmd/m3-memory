---
paths:
  - "tests/**/*.py"
  - "conftest.py"
---

# A test must not touch the developer's real state

Tests have written into real config more than once. Each was silent, and the
suite stayed green:

- `generate_configs` writes `.mcp.json` into the repository root
  unconditionally — three tests reach it directly or through setup.
- `chatlog_config.CONFIG_PATH` is resolved **at import**, so the
  environment-variable half of the sandbox does not cover it. A test that saved
  chatlog config rewrote the real `~/.m3/config/.chatlog_config.json` with a
  temporary `db_path`; afterwards live capture pointed at a pytest temp
  directory that is later deleted.
- A mocked `subprocess.run` that treats the last argument as a path to create
  will create directories in the checkout.

So: any module-level constant holding a path must be re-pointed **per test**,
not just overridden by env var. Never let a test resolve a warehouse or
production DSN as a fallback.

Prefer a capability marker that skips over a test that silently exercises
nothing — a test reporting a state the machine is not in is worse than an
honest skip.

# Do not monkeypatch a `memory.*` attribute by dotted string

`monkeypatch.setattr("memory.backends.active_backend", ...)` resolves
`backends` as a PACKAGE ATTRIBUTE. `memory/__init__.py` imports its submodules
eagerly but NOT `backends`, and conftest's `_restore_memory_modules` purges the
whole `memory.*` namespace whenever a test replaced a module in it — so a
rebuilt `memory` may not carry the attribute and the patch raises
`AttributeError`. A module-level `import memory.backends` does not fix it: that
runs at collection time and does not survive the purge.

Reach the module with `importlib.import_module` and patch that object:

```python
import importlib
mb = importlib.import_module("memory.backends")
monkeypatch.setattr(mb, "active_backend", lambda: _Backend())
```

`import memory.backends` is NOT good enough, in or out of a test function: it is
a no-op when the submodule is already in `sys.modules`, so it never rebinds the
attribute on a rebuilt parent, and the following `memory.backends` access raises
the same `AttributeError`. `importlib.import_module` reads `sys.modules`
directly. Pinned by `tests/test_memory_submodule_access_is_attribute_free.py`.

This is also the correct target on its merits — the code under test resolves
these names from `sys.modules` at call time, so the live module object is what
it reads. A dotted string adds a resolution step that can fail independently of
the behaviour under test.

Related: a fixture that POPS a `memory.*` submodule on teardown counts as a
replacement to that safety net and triggers the whole-namespace purge. Restore
the object you were handed instead.

# conftest's namespace check tears down AFTER monkeypatch

`tests/conftest.py` has an autouse teardown that fails the test which leaves
`memory.*` incoherent (no `backends` attribute, or `dialect` shadowed by its
submodule). Conftest autouse fixtures are instantiated first, so they finalize
LAST — after `monkeypatch` has undone its changes.

So a test that uses `monkeypatch.delattr`/`setattr` to create such a state is
still holding it when that check runs, and errors on itself. Undo it in a
test-local fixture (which finalizes first) or an explicit `try/finally`:

```python
@pytest.fixture
def parent_without_the_attribute():
    parent = sys.modules["memory"]
    saved = parent.backends
    delattr(parent, "backends")
    try:
        yield parent
    finally:
        parent.backends = saved
```

Note also that mutating a cached module object cannot be undone by the restore
path — conftest puts the same mutated object back.

The check blames only the test that BROKE the namespace, never one that
inherited it, and purges afterwards so a single breakage cannot cascade. The
first version blamed whichever test was running and turned one cause into 2819
teardown errors.
