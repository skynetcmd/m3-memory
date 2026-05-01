# <a href="../../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Security Scan — 2026-05-01

> Snapshot of M3 Memory's security posture run by the maintainers. Reproducible — run the same tools yourself and compare. See [`docs/SECURITY.md`](../SECURITY.md) for the disclosure policy and [`docs/audits/`](.) for the full audit history.

---

## Scope

| Layer | Tool | Coverage |
|---|---|---|
| Static code analysis | Bandit 1.9.4 | `bin/` + `m3_memory/` (85 files, 28,980 LoC) |
| Secrets in tree | `git ls-files` + regex | Anthropic / OpenAI / Google / GitHub / Slack / AWS keys + private-key headers |
| Dependency CVEs | pip-audit | Active Python virtualenv |

## Results

| Layer | Status |
|---|---|
| Code patterns (Bandit) | ✅ Clean — 0 HIGH, 0 MEDIUM, 3 LOW (false positives) |
| Secrets in tree | ✅ Clean — no real keys/tokens/certs tracked |
| Core runtime deps | ✅ Clean — no CVEs in shipped library deps |
| Optional / bench-only deps | ⚠️ 14 CVEs, all in opt-in or bench-only packages |

**Headline:** the shipped library — what users get from `pip install m3-memory` — has no known security issues. The 14 CVEs are confined to bench/dev paths and don't reach end users.

---

## Bandit (static analysis)

3 LOW findings, all `B311` (`random.sample` not cryptographically secure):

- `bin/m3_enrich.py:475` — `_random.sample(groups, sample)`
- `bin/m3_enrich.py:490` — `_random.sample(b, ...)`
- `bin/m3_enrich.py:496` — `_random.sample(rest, ...)`

All three are statistical sampling for the `--sample N` CLI flag (pick N random conversations to enrich for dev/bench runs). Standard `random` is correct here; `secrets` would be wrong (we want reproducible sampling, not cryptographic entropy).

**Verdict:** No code action. The 7 documented Bandit skips in `pyproject.toml` (B101, B110, B112, B404, B603, B607, B608) remain justified.

To reproduce:
```bash
python -m bandit -c pyproject.toml -r bin/ m3_memory/
```

## Secrets scan

Two files contained pattern matches; both are intentional:

- `bin/chatlog_redaction.py` — owns the redaction pattern set; matches are `re.compile(...)` lines plus an in-docstring synthetic example.
- `tests/test_chatlog_redaction.py` — synthetic test fixtures.

No real secrets in the tree. `.env`, `.pem`, `.p12`, `secrets.json`, `credentials.json` — none tracked. `.gitignore` is doing its job.

To reproduce:
```bash
git ls-files | xargs grep -lE "AIza[0-9A-Za-z_-]{30,}|sk-ant-api[0-9]{2}-[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{32,}|gho_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|xoxb-[0-9]{10,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA|OPENSSH|EC|DSA) PRIVATE KEY-----"
```

## pip-audit (dependency CVEs)

14 CVEs across 3 packages. **Crucial scope context:**

| Package | Version | CVEs | Pulled in by | Affects shipped M3? |
|---|---|---|---|---|
| `transformers` | 4.49.0 | 12 | `sentence-transformers`, `FlagEmbedding`, `peft` | ❌ No — opt-in cross-encoder rerank only |
| `lxml` | 6.0.4 | 1 | `inscriptis`, `ir_datasets` | ❌ No — bench-only deps |
| `pip` | 26.0.1 | 1 | (build tooling) | ❌ No — not a runtime dependency |

**None of these packages appear in M3's core `dependencies` list in `pyproject.toml`.** They are transitive from optional or development extras only.

The CVEs only matter for:
- Developers who install dev extras (`m3-memory[dev]`) and use the bench harness
- Users who explicitly enable cross-encoder reranking (`rerank=True`), which is documented as opt-in

### Recommended actions (none urgent)

1. **`pip` 26.0.1 → 26.1** — trivial venv bump: `python -m pip install --upgrade pip`. Doesn't affect M3 itself.
2. **`lxml` 6.0.4 → 6.1.0** — bench-only impact. Bump in `benchmarks/` extras when we next pin them.
3. **`transformers` 4.49.0 → 4.53.0+** — bench/rerank-only impact. Bump the rerank optional-dependency floor when we next touch it.

### Going forward

CI runs `pip-audit --strict` against **core deps only** (not bench/dev extras) on every push. That gates merges on shipped-library CVEs without false-alarming on bench/dev transitives. See `.github/workflows/ci.yml`.

---

## Reproducing the full scan

```bash
# Bandit
python -m pip install bandit
python -m bandit -c pyproject.toml -r bin/ m3_memory/

# Secrets (Linux/macOS/WSL)
git ls-files | xargs grep -lE "AIza[0-9A-Za-z_-]{30,}|sk-ant-api[0-9]{2}|sk-[A-Za-z0-9]{32,}|gho_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN.*PRIVATE KEY-----"

# Dependency CVEs
python -m pip install pip-audit
python -m pip_audit --strict
```

Findings should match this report (modulo new CVEs landing upstream after 2026-05-01). Differences are interesting — please [open an issue](https://github.com/skynetcmd/m3-memory/issues) if your run finds something we missed.
