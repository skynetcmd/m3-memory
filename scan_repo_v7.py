#!/usr/bin/env python3
"""Scan orchestrator for the m3-memory security pipeline on LXC 504.

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
"""
import argparse, json, os, stat, subprocess, sys, time
from datetime import datetime
from pathlib import Path
import urllib.request, urllib.parse

DD_URL = os.environ.get('DD_URL', 'http://10.21.40.54:8080')

# Scanner talks to DefectDojo over plain HTTP inside the homelab LAN, or HTTPS
# if deployed differently. Reject any DD_URL scheme outside this whitelist so
# a bad env var (or future misconfig) can't silently pivot urlopen to file://
# or ftp:// — which would let a local file read masquerade as a DD call.
_ALLOWED_SCHEMES = ('http://', 'https://')
if not DD_URL.startswith(_ALLOWED_SCHEMES):
    print(f"ERROR: DD_URL={DD_URL!r} must start with http:// or https://", file=sys.stderr)
    sys.exit(2)


def _exchange_credentials_for_token(username: str, password: str) -> str | None:
    """POST credentials to /api/v2/api-token-auth/ and return the API Key string.

    Returns None on any failure; caller logs + falls through to the next
    source. Uses urllib so the resolver has zero third-party deps — the
    `requests` import used by upload_import() is deferred to run-time.
    """
    url = f'{DD_URL}/api/v2/api-token-auth/'
    payload = json.dumps({'username': username, 'password': password}).encode('utf-8')
    req = urllib.request.Request(
        url, data=payload, method='POST',
        headers={'Content-Type': 'application/json'},
    )
    try:
        # B310: DD_URL scheme whitelisted to http/https at module load.
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            data = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as e:
        print(f"WARNING: credential exchange against {url} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    token = (data.get('token') or '').strip() if isinstance(data, dict) else ''
    return token or None


def _read_secret_file(path: Path) -> str | None:
    """Read a mode-0600 secret file; warn on loose perms, return stripped contents."""
    if not path.exists():
        return None
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return None
    if mode & 0o077:
        print(f"WARNING: {path} is mode {oct(mode)}; should be 0600", file=sys.stderr)
    try:
        return path.read_text(encoding='utf-8').strip() or None
    except OSError:
        return None


def _load_dd_token() -> str:
    """Resolve a DefectDojo API Key from env vars or the file-based vault.

    Accepts either a static token or username:password credentials at each
    tier (env > ~/.config/defectdojo > /etc/defectdojo). Credentials are
    exchanged for the same API Key the UI shows, so the transport path is
    identical regardless of input shape.
    """
    # Tier 1: env token wins (what CI typically sets)
    env_token = os.environ.get('DD_TOKEN', '').strip()
    if env_token:
        return env_token
    # Tier 1b: env username/password, exchanged
    env_user = os.environ.get('DD_USERNAME', '').strip()
    env_pass = os.environ.get('DD_PASSWORD', '').strip()
    if env_user and env_pass:
        token = _exchange_credentials_for_token(env_user, env_pass)
        if token:
            return token

    # Tiers 2 and 3: filesystem. For each directory, check token file then
    # credentials file — operator can drop whichever shape they prefer.
    dirs = [
        Path.home() / '.config' / 'defectdojo',
        Path('/etc/defectdojo'),
    ]
    for d in dirs:
        # Static token file
        token = _read_secret_file(d / 'token')
        if token:
            return token
        # Credentials file (user:pass on a single line, or user and password on two lines)
        raw = _read_secret_file(d / 'credentials')
        if not raw:
            continue
        if ':' in raw.splitlines()[0]:
            u, _, p = raw.splitlines()[0].partition(':')
        else:
            lines = [x.strip() for x in raw.splitlines() if x.strip()]
            if len(lines) < 2:
                print(f"WARNING: {d/'credentials'} must be 'user:pass' or two lines", file=sys.stderr)
                continue
            u, p = lines[0], lines[1]
        token = _exchange_credentials_for_token(u.strip(), p.strip())
        if token:
            return token

    print(
        "ERROR: no DefectDojo credentials configured.\n"
        "  Provide one of the following and rerun:\n"
        "    export DD_TOKEN=<api-key>\n"
        "    export DD_USERNAME=<user> DD_PASSWORD=<pass>\n"
        "    echo <api-key> > ~/.config/defectdojo/token && chmod 600 ~/.config/defectdojo/token\n"
        "    echo 'user:pass' > ~/.config/defectdojo/credentials && chmod 600 ~/.config/defectdojo/credentials\n"
        "    sudo mkdir -p /etc/defectdojo && echo <api-key> > /etc/defectdojo/token && sudo chmod 600 /etc/defectdojo/token\n"
        "    sudo mkdir -p /etc/defectdojo && echo 'user:pass' > /etc/defectdojo/credentials && sudo chmod 600 /etc/defectdojo/credentials",
        file=sys.stderr,
    )
    sys.exit(2)


DD_TOKEN = _load_dd_token()

# Ensure all scanner paths are in the PATH
os.environ['PATH'] = '/usr/local/bin:/usr/bin:/bin:/root/.local/bin'

SCANNERS = [
    # Core Scanners
    ('gitleaks',  ['gitleaks', 'detect', '--source', '{repo}', '--report-format', 'json', '--report-path', '{out}/gitleaks.json', '--no-banner', '--exit-code', '0'], 'gitleaks.json', 'Gitleaks Scan'),
    ('trufflehog',['trufflehog', 'filesystem', '{repo}', '--json', '--no-update', '--only-verified', '--exclude-paths', '/data/bin/trufflehog_exclude.txt'], 'trufflehog.jsonl', 'Trufflehog Scan'),
    ('trivy',     ['trivy', 'fs', '--scanners', 'vuln,secret,misconfig', '--format', 'json', '--output', '{out}/trivy.json', '{repo}'], 'trivy.json', 'Trivy Scan'),
    ('semgrep',   ['semgrep', '--config=p/ci', '--sarif', '--output={out}/semgrep.sarif', '--quiet', '--metrics=off', '{repo}'], 'semgrep.sarif', 'SARIF'),
    ('bandit',    ['bandit', '-r', '{repo}', '-c', '{repo}/pyproject.toml', '-f', 'json', '-o', '{out}/bandit.json', '--exit-zero'], 'bandit.json', 'Bandit Scan'),
    ('pip-audit', ['bash', '-c', 'find {repo} -name requirements*.txt | head -1 | xargs -I R pip-audit -r R -f json -o {out}/pip-audit.json || echo [] > {out}/pip-audit.json'], 'pip-audit.json', 'pip-audit Scan'),
    ('checkov',   ['checkov', '-d', '{repo}', '-o', 'sarif', '--output-file-path', '{out}', '--quiet', '--soft-fail'], 'results_sarif.sarif', 'SARIF'),
    
    # Auxiliary Scanners
    ('osv-scanner',['osv-scanner', '-r', '{repo}', '--format', 'json', '--output', '{out}/osv-scanner.json'], 'osv-scanner.json', 'OSV Scan'),
    ('safety',    ['safety', 'check', '-r', '{repo}/requirements.txt', '--json', '--output', '{out}/safety.json'], 'safety.json', 'Safety Scan'),
    ('scancode',  ['scancode', '--json-pp', '{out}/scancode.json', '--license', '--copyright', '--info', '--quiet', '{repo}'], 'scancode.json', 'ScanCode Scan'),
    ('kubescape', ['kubescape', 'scan', '{repo}', '--format', 'sarif', '--output', '{out}/kubescape.sarif'], 'kubescape.sarif', 'SARIF'),

    # Quality Scanners - Mypy now uses repo root + excludes to avoid duplicate module name errors
    ('ruff',      ['ruff', 'check', '--format', 'sarif', '--output-file', '{out}/ruff.sarif', '{repo}/bin', '{repo}/memory', '{repo}/m3_memory'], 'ruff.sarif', 'SARIF'),
    ('mypy',      ['mypy', '{repo}', '--ignore-missing-imports', '--explicit-package-bases', '--exclude', 'examples/', '--exclude', 'tests/', '--exclude', 'benchmarks/'], 'mypy.txt', None),
]

def run_scanner(name, argv_tmpl, out_dir, repo_path):
    argv = [a.format(out=str(out_dir), repo=repo_path) for a in argv_tmpl]
    t0 = time.time()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=1800)
        elapsed = time.time() - t0
        if name == 'trufflehog':
            (out_dir / 'trufflehog.jsonl').write_text(r.stdout)
        elif name == 'mypy':
             (out_dir / 'mypy.txt').write_text(r.stdout + r.stderr)
        print(f'[{name}] rc={r.returncode} {elapsed:.1f}s', flush=True)
        return True
    except Exception as e:
        print(f'[{name}] ERROR: {e}', flush=True)
        return False

def dd_request(path, method='GET', data=None):
    url = f'{DD_URL}{path}'
    req = urllib.request.Request(url, method=method, headers={'Authorization': f'Token {DD_TOKEN}', 'Content-Type': 'application/json'})
    body = json.dumps(data).encode() if data else None
    try:
        # B310: DD_URL scheme whitelisted to http/https at module load.
        with urllib.request.urlopen(req, body, timeout=30) as resp:  # nosec B310
            return resp.status, resp.read().decode()
    except Exception as e:
        return 500, str(e)

def ensure_product_and_engagement(product_name, engagement_name):
    code, body = dd_request(f'/api/v2/products/?name={urllib.parse.quote(product_name)}')
    results = json.loads(body).get('results', [])
    product_id = results[0]['id'] if results else json.loads(dd_request('/api/v2/products/', 'POST', {'name': product_name, 'description': product_name, 'prod_type': 1})[1])['id']
    today = datetime.now().strftime('%Y-%m-%d')
    eng_id = json.loads(dd_request('/api/v2/engagements/', 'POST', {
        'name': engagement_name, 'product': product_id, 'target_start': today, 'target_end': today,
        'status': 'In Progress', 'engagement_type': 'CI/CD'
    })[1])['id']
    print(f'Created engagement {engagement_name} id={eng_id}')
    return eng_id

def upload_import(engagement_id, scan_type, file_path):
    import requests
    url = f'{DD_URL}/api/v2/import-scan/'
    with open(file_path, 'rb') as f:
        # Scan files can be multi-MB (SARIF, trivy JSON); give the DD
        # ingester a generous read budget. Connect is the short leg.
        r = requests.post(
            url,
            headers={'Authorization': f'Token {DD_TOKEN}'},
            data={'engagement': engagement_id, 'scan_type': scan_type, 'active': True, 'verified': False},
            files={'file': f},
            timeout=(10, 300),
        )
    return r.status_code, r.text[:200]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('repo')
    ap.add_argument('--engagement-name', default=None)
    args = ap.parse_args()
    repo = Path(args.repo).resolve()
    ts = datetime.now().strftime('%Y%m%dT%H%M%SZ')
    out_dir = Path('/data/reports') / repo.name / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f'== scanning {repo} -> {out_dir}', flush=True)
    for name, tmpl, fname, _ in SCANNERS:
        run_scanner(name, tmpl, out_dir, str(repo))
    eng_name = args.engagement_name or f'scan {ts}'
    eng_id = ensure_product_and_engagement(repo.name, eng_name)
    for name, tmpl, fname, dd_type in SCANNERS:
        if not dd_type: continue
        fpath = out_dir / fname
        if fpath.exists() and fpath.stat().st_size > 0:
            code, body = upload_import(eng_id, dd_type, fpath)
            print(f'[{name}] upload {dd_type}: {code} {body[:120]}')
    print(f'== done; reports in {out_dir}, DefectDojo: {DD_URL}')

if __name__ == '__main__':
    main()
