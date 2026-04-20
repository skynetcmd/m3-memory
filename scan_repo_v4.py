#!/usr/bin/env python3
import argparse, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
import urllib.request, urllib.parse

DD_URL = os.environ.get('DD_URL', 'http://10.21.40.54:8080')
DD_TOKEN = os.environ.get('DD_TOKEN', '98251a5a1cce6de054429349827f99e6ffa5b335')

# Ensure all scanner paths are in the PATH
os.environ['PATH'] = '/usr/local/bin:/usr/bin:/bin:/root/.local/bin'

SCANNERS = [
    # Gitleaks switched to JSON + "Gitleaks Scan" type for better DefectDojo compatibility
    ('gitleaks',  ['gitleaks', 'detect', '--source', '{repo}', '--report-format', 'json', '--report-path', '{out}/gitleaks.json', '--no-banner', '--exit-code', '0'], 'gitleaks.json', 'Gitleaks Scan'),
    ('trufflehog',['trufflehog', 'filesystem', '{repo}', '--json', '--no-update', '--only-verified', '--exclude-paths', '/data/bin/trufflehog_exclude.txt'], 'trufflehog.jsonl', 'Trufflehog Scan'),
    ('trivy',     ['trivy', 'fs', '--scanners', 'vuln,secret,misconfig', '--format', 'json', '--output', '{out}/trivy.json', '{repo}'], 'trivy.json', 'Trivy Scan'),
    ('semgrep',   ['semgrep', '--config=p/ci', '--sarif', '--output={out}/semgrep.sarif', '--quiet', '--metrics=off', '{repo}'], 'semgrep.sarif', 'SARIF'),
    ('bandit',    ['bandit', '-r', '{repo}', '-c', '{repo}/pyproject.toml', '-f', 'json', '-o', '{out}/bandit.json', '--exit-zero'], 'bandit.json', 'Bandit Scan'),
    ('pip-audit', ['bash', '-c', 'find {repo} -name requirements*.txt | head -1 | xargs -I R pip-audit -r R -f json -o {out}/pip-audit.json || echo [] > {out}/pip-audit.json'], 'pip-audit.json', 'pip-audit Scan'),
    ('checkov',   ['checkov', '-d', '{repo}', '-o', 'sarif', '--output-file-path', '{out}', '--quiet', '--soft-fail'], 'results_sarif.sarif', 'SARIF'),
    ('osv-scanner',['osv-scanner', '-r', '{repo}', '--format', 'json', '--output', '{out}/osv-scanner.json'], 'osv-scanner.json', 'OSV Scan'),
    ('safety',    ['safety', 'check', '-r', '{repo}/requirements.txt', '--json', '--output', '{out}/safety.json'], 'safety.json', 'Safety Scan'),
    ('scancode',  ['scancode', '--json-pp', '{out}/scancode.json', '--license', '--copyright', '--info', '--quiet', '{repo}'], 'scancode.json', 'ScanCode Scan'),
    ('kubescape', ['kubescape', 'scan', '{repo}', '--format', 'sarif', '--output', '{out}/kubescape.sarif'], 'kubescape.sarif', 'SARIF'),
    ('ruff',      ['ruff', 'check', '--format', 'sarif', '--output-file', '{out}/ruff.sarif', '{repo}'], 'ruff.sarif', 'SARIF'),
    ('mypy',      ['mypy', '{repo}', '--ignore-missing-imports', '--explicit-package-bases'], 'mypy.txt', None),
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
        with urllib.request.urlopen(req, body) as resp:
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
        r = requests.post(url, headers={'Authorization': f'Token {DD_TOKEN}'},
            data={'engagement': engagement_id, 'scan_type': scan_type, 'active': True, 'verified': False},
            files={'file': f})
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
