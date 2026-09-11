# Installer builds a nested `.venv` inside the installed payload and runs every step with it

**Status:** open · **Found:** 2026-09-11 · **Severity:** low blast radius today, latent footgun
**Components:** `bin/setup_memory.py`, `bin/generate_configs.py`

## Symptom

`m3 update` on an installed (pipx) deployment prints:

```
[3/6] Installing cross-platform dependencies...
  -> Warning: <site-packages>/m3_memory/requirements.txt not found. Installing defaults...
...
[8/8] Generating MCP configuration files...
[generate_configs] WARN: could not seed shared embedder config: No module named 'm3_memory'
Generated claude-settings.json (<site-packages>/m3_memory/.venv/Scripts/python.exe, ...)
```

## Root cause

`bin/setup_memory.py:14-17` derives its interpreter from the script's own location,
unconditionally:

```python
BASE   = pathlib.Path(__file__).parent.parent.resolve()
VENV   = BASE / ".venv"
PY     = VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")
```

When the installed payload runs, `BASE` is `<site-packages>/m3_memory`, so the installer
**creates a second venv nested inside site-packages** (`setup_memory.py:44`) and uses it as
`PY` for every subsequent subprocess step.

Confirmed from the nested venv's own `pyvenv.cfg`:

```
command = <pipx>/venvs/m3-memory/Scripts/python.exe -m venv <site-packages>/m3_memory/.venv
```

That nested interpreter has `include-system-site-packages = false` and no `m3_memory`
installed, so `generate_configs.py:112`'s bare
`from m3_memory.embedder_admin import seed_shared_config` raises `ModuleNotFoundError`.
The same wrong `BASE` explains the missing `requirements.txt` at step [3/6].

Reproduced with the ambient environment cleared (`-E`, empty `PYTHONPATH`, outside the dev checkout):

| interpreter | `import m3_memory` |
|---|---|
| `<site-packages>/m3_memory/.venv/Scripts/python.exe` | `ModuleNotFoundError: No module named 'm3_memory'` |
| `<pipx>/venvs/m3-memory/Scripts/python.exe` | OK -> installed payload |

> Note: an *uncleared* shell gave a false pass — both interpreters imported `m3_memory`
> from the **dev checkout** via an ambient `.pth`/cwd leak. The contaminated test says the
> bug does not exist. Always isolate before concluding.

## Why `generate_configs` is not itself at fault

`generate_configs._resolve_python_cmd` already has the correct guard —
`_is_installed_layout()` exists specifically to reject a `.venv` sitting beside an installed
wheel, and its docstring names this exact footgun ("a stray leftover, NOT the canonical
interpreter"). `setup_memory.py` has no equivalent check and bypasses that resolver by
launching subprocesses with its own hardcoded `PY`.

## Impact

1. **~196 MB of duplicated dependencies** installed into site-packages and never used by the
   running system (the full `fastmcp httpx numpy keyring cryptography psycopg2-binary` tree).
2. **Shared embedder config is not seeded during install** — self-corrected here only because
   `m3 doctor --fix` re-runs `generate_configs.py` with the outer pipx interpreter.
3. **Generated agent configs briefly point at the stray interpreter.** Install wrote
   `claude-settings.json` against `.venv/Scripts/python.exe`; doctor rewrote it to
   `<pipx>/venvs/m3-memory/Scripts/python.exe`. A bare `m3 update` with no doctor run
   leaves hosts pointed at an interpreter that may lack deps or later vanish — the precise
   ModuleNotFound failure mode `_resolve_python_cmd` was written to prevent.

Consistent with the existing finding that `m3 doctor --fix` and the installer disagree about
which path is canonical (see the `bridge_path` / recorded-install investigation, 2026-09-09).

## Suggested fix

Give `setup_memory.py` the same installed-layout guard `generate_configs.py` already has:
when `BASE` is a `site-packages`/`dist-packages` tree, use `sys.executable` (the pipx
interpreter that is definitionally running the installer) instead of creating or using a
nested `.venv`. Reuse `generate_configs._is_installed_layout` rather than restating the
predicate — a second copy will drift.

Cleanup for already-affected installs: the nested `<site-packages>/m3_memory/.venv` is inert
and can be removed once the installer stops writing agent configs that reference it.

## Verification checklist

- [ ] `m3 update` on a pipx install creates no `<site-packages>/m3_memory/.venv`
- [ ] No `could not seed shared embedder config` warning during install
- [ ] `claude-settings.json` names the pipx interpreter straight out of install, before any doctor run
- [ ] `m3 doctor` reports no repointing needed immediately after `m3 update`
