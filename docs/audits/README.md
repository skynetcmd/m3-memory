# <a href="../../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Audit History

Dated reports from periodic security and quality scans. Each entry is reproducible — every report includes the exact commands used, so anyone can verify on their own machine.

For the disclosure policy and security design, see [`docs/SECURITY.md`](../SECURITY.md).

---

## Reports

| Date | Type | Report | Headline |
|---|---|---|---|
| 2026-05-01 | Security (Bandit + secrets + pip-audit) | [security-scan-2026-05-01.md](security-scan-2026-05-01.md) | Clean shipped library; 14 CVEs all in opt-in / bench-only deps |

---

## What gets audited

- **Static code analysis** — Bandit, configured via `pyproject.toml` `[tool.bandit]`
- **Secrets in tree** — regex scan against tracked files for known token formats (Anthropic, OpenAI, Google, GitHub, Slack, AWS, private keys)
- **Dependency CVEs** — pip-audit against the active environment, scoped per-report

---

## Cadence

Manual scans land here when the maintainers run them. CI runs `pip-audit --strict` on **core dependencies only** (no `[dev]`, no opt-in rerank path) on every push, so new CVEs in shipped-library deps fail the build immediately. See [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) for the exact job.

If you'd like the cadence formalized (monthly, quarterly, on-release), [open an issue](https://github.com/skynetcmd/m3-memory/issues).

---

## Found something?

If you reproduce the scans and find a discrepancy, please [open an issue](https://github.com/skynetcmd/m3-memory/issues) — that's faster than a private vulnerability report for non-sensitive findings. For an actual security vulnerability, follow the disclosure process in [`docs/SECURITY.md`](../SECURITY.md).
