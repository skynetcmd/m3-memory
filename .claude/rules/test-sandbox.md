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
