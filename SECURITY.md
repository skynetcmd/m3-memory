# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 2026.04.x (latest) | ✅ Yes |

## Reporting a Vulnerability

If you discover a security vulnerability, **please do not open a public GitHub issue**.

Instead, report it via GitHub's private vulnerability reporting:
1. Go to the [Security tab](https://github.com/skynetcmd/m3-memory/security) of this repository
2. Click **"Report a vulnerability"**
3. Provide a description, steps to reproduce, and potential impact

We will acknowledge your report within 48 hours and aim to release a fix within 14 days for confirmed vulnerabilities.

## Security Design

M3 Memory is designed with security as a first-class concern:

- **Credential storage** — AES-256 encrypted vault (PBKDF2-HMAC-SHA256, 600K iterations). API keys and secrets never stored in plaintext. OS keyring integration (Keychain on macOS, Credential Manager on Windows).
- **Content integrity** — SHA-256 hash computed and stored on every write. `memory_verify` re-computes and compares to detect post-write tampering.
- **Input safety** — write boundary rejects XSS, SQL injection, Python code injection, and prompt injection patterns before data reaches storage.
- **Search safety** — FTS5 operator sanitization prevents query injection.
- **Network hardening** — circuit breaker (3-failure threshold), strict timeouts, API tokens never logged.
- **Data locality** — all data remains on your hardware by default. Optional sync to PostgreSQL is under your control.

## Scope

In-scope for vulnerability reports:
- Authentication/authorization bypass
- Credential vault weaknesses
- Input sanitization bypasses (XSS, injection, poisoning)
- Data exfiltration vulnerabilities
- Tamper detection failures

Out of scope:
- Issues requiring physical access to the machine
- Social engineering attacks
- Vulnerabilities in third-party dependencies (report to the upstream project)
