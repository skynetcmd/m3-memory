---
tool: scan_repo_v7.py
sha1: 748ccf7325a2
mtime_utc: 2026-04-22T01:03:02.092891+00:00
generated_utc: 2026-04-22T02:11:24.935928+00:00
private: false
---

# scan_repo_v7.py

## Purpose

Scan orchestrator for the m3-memory security pipeline on LXC 504.

Runs the standard scanner suite (gitleaks / trufflehog / trivy / semgrep /
bandit / pip-audit / checkov / osv-scanner / safety / scancode / kubescape
/ ruff / mypy) against a checkout and uploads each report to DefectDojo.

DefectDojo credential resolution (first non-empty source wins):
  1. DD_TOKEN env var                                  → API Key (direct)
  2. DD_USERNAME + DD_PASSWORD env vars                → API Key (fetched via /api/v2/api-token-auth/)
  3. ~/.config/defectdojo/token  (single line)         → API Key (direct)
  4. ~/.config/defectdojo/credentials (user:pass)      → API Key (fetched)
  5. /etc/defectdojo/token       (LXC-wide fallback)   → API Key (direct)
  6. /etc/defectdojo/credentials (LXC-wide fallback)   → API Key (fetched)

DefectDojo exposes two authentication paths in the admin UI: "API Key"
(long-lived per-user token shown under each user's profile) and an
"Auth Header" path reached by POSTing {username, password} to
/api/v2/api-token-auth/, which *returns the same API Key* the UI shows.
The wire format for both is identical once we have the token:
    Authorization: Token <api-key>
So the resolver supports both inputs but converges on a single static
token at the transport layer — no JWT refresh loop, no in-memory expiry
tracking. Credential files are expected at mode 0600; loose permissions
warn but still load (operator may have relaxed for a service account).

No hardcoded fallback. If no source yields a token the script exits 2
with a setup hint covering both input shapes.

## Entry points

- `def main()` (line 228)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `repo` |  | — | Positional — required; script exits with argparse error if omitted. | str | Resolves PATH to an absolute repo root and scans every file under it. |
| `--engagement-name` |  | None | Auto-names the DefectDojo engagement `scan <UTC-timestamp>`. | str | Uses NAME as the DefectDojo engagement title; lets you group related runs. |

## Environment variables read

- `DD_PASSWORD`
- `DD_TOKEN`
- `DD_URL`
- `DD_USERNAME`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `argv`` (line 178)

**http**

- `requests.post()  → `url`` (line 219)


## Notable external imports

- `requests`
- `stat`

## File dependencies (repo paths referenced)

- `/data/bin/trufflehog_exclude.txt`
- `bandit.json`
- `find {repo} -name requirements*.txt | head -1 | xargs -I R pip-audit -r R -f json -o {out}/pip-audit.json || echo [] > {out}/pip-audit.json`
- `mypy.txt`
- `osv-scanner.json`
- `pip-audit.json`
- `safety.json`
- `scancode.json`
- `trivy.json`
- `{out}/bandit.json`
- `{out}/gitleaks.json`
- `{out}/osv-scanner.json`
- `{out}/safety.json`
- `{out}/scancode.json`
- `{out}/trivy.json`
- `{repo}/pyproject.toml`
- `{repo}/requirements.txt`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
