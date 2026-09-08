# Security scan — 2026-09-08

Scope: the shipped library (`bin/`, `m3_memory/`) at commit `84899b1a103f`,
version `2026.9.8.0`.

**Headline: no HIGH-severity static-analysis findings; one real dependency CVE
cluster, fixed by raising a version floor.**

Every command below is reproducible on your own checkout — no private tooling.

---

## 1. Static analysis — `bandit`

```bash
python -m bandit -c pyproject.toml -r bin m3_memory
```

| Metric | Value |
|---|---|
| Files scanned | 314 |
| Lines of code | 95,883 |
| Findings (with project skip-list) | **0** |
| Findings (unfiltered) | 1,178 |
| **HIGH severity (unfiltered)** | **0** |

The skip-list lives in `pyproject.toml` under `[tool.bandit]` and each entry
carries its reasoning inline. A clean filtered scan is not evidence on its own,
so the unfiltered run is published too:

| Test | Severity | Count | Why it is suppressed |
|---|---|---|---|
| B608 | MEDIUM | 556 | f-string SQL. Every site inlines generated `?`/`%s` placeholder lists or static column names; user values reach the DB through the parameter tuple. Bandit's regex cannot tell safe from unsafe here. |
| B110 / B112 | LOW | 219 | `try/except/pass` and `/continue` in fail-open bridge and daemon paths. |
| B603 / B607 | LOW | 275 | `subprocess` with `shell=False` and static argv against OS binaries (`git`, `docker`, …). |
| B404 | LOW | 63 | Importing `subprocess` is not itself a vulnerability. |
| B101 | LOW | 53 | `assert` outside tests, used as internal invariants. |
| B311 | LOW | 4 | `random.sample` for statistical `--sample N` selection, not entropy. |

### Two rules NOT previously on the skip-list, reviewed this scan

Both are false positives, recorded here rather than silently suppressed:

- **B105 "hardcoded password string" ×6** — all in `bin/memory_core.py`, and all
  dict *keys* in a symbol-routing table (`"M3_ALLOW_CLOUD_FALLBACK":
  "memory.config"`). Bandit's heuristic reads a string literal next to a colon
  as a credential assignment. No secret is present.
- **B606 "start process with no shell" ×2** — `bin/m3_core/runtime.py:104` and
  `m3_memory/cli.py:87`, both `os.execv(sys.executable, …)` re-execing the
  **current Python interpreter** with a controlled argv to fix stdio encoding on
  Windows. No shell, and the executable path is `sys.executable`, never user
  input.

## 2. Secrets scan

```bash
git ls-files -z | xargs -0 grep -lE \
  'sk-ant-[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{30,}'
```

**One file matches: `bin/chatlog_redaction.py`** — the redaction engine itself,
whose job is to hold those patterns as regexes so it can scrub them out of chat
logs. No credential material in the tree.

## 3. Dependency CVEs — `pip-audit`

```bash
python -m pip_audit
```

The raw environment reports 235 advisories across 32 packages, but that measures
a *developer* machine with bench, docs and example extras installed. What ships
is narrower — `pyproject.toml` `[project].dependencies` is three packages:

| Core dependency | Advisories |
|---|---|
| `fastmcp` | 0 |
| `httpx` | 0 |
| **`mcp` 1.27.0** | **3** |

### The one real finding

`mcp` 1.27.0 carries **PYSEC-2026-3481/3482/3483**, all fixed by **1.28.1**:

| ID | Affects | Applies to m3? |
|---|---|---|
| PYSEC-2026-3482 | SSE / Streamable HTTP transports route by session id alone | **No** — m3 speaks MCP over **stdio** |
| PYSEC-2026-3483 | Deprecated WebSocket server transport accepts handshakes | **No** — not used |
| PYSEC-2026-3481 | Experimental `enable_tasks()` default handlers | **No** — not enabled |

All three are in **server transports m3 does not run**, so real-world exposure is
low. The floor was raised anyway — a dependency with known CVEs should not be
installable just because our particular call path avoids them:

```diff
- "mcp>=1.27.0,<2",
+ "mcp>=1.28.1,<2",
```

The previous range already *permitted* 1.28.1, so a fresh install was already
getting the fix; the change makes it explicit and stops a pinned environment
from resolving backwards.

### Everything else

The remaining 29 vulnerable packages are **not shipped-library dependencies**:
`nltk` (62), `pypdf` (40), `glances` (29), `pillow` (26), `aiohttp` (14) and the
rest arrive via optional extras, the examples tree, or the developer's own
environment. They are out of scope for what `pip install m3-memory` puts on a
user's machine.

---

## Gap this scan closes

The previous published audit was **2026-05-01** — four months before this one,
while `SECURITY.md` claimed scans were run and published "periodically". That
gap was itself a defect: a documented practice with no evidence behind it is a
claim, not a control. Reported by the maintainer on 2026-09-08.
