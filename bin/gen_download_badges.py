#!/usr/bin/env python3
"""Generate download-count badge data (PyPI + GitHub) for the README.

Writes two shields.io *endpoint* JSON files that the README badges point at:

  * docs/badges/pypi-downloads.json  — estimated TOTAL PyPI downloads
        (pypistats "overall", WITHOUT mirrors — excludes bandersnatch/CI mirror
        bots, so it approximates real installs). pypistats keeps ~180 days; for a
        package younger than that this is effectively all-time.
  * docs/badges/github-clones.json   — TOTAL unique GitHub clones
        (the repo traffic API's rolling 14-day `uniques`). GitHub only retains a
        14-day window, so a running total is accumulated in
        docs/badges/clone-history.json across scheduled runs (dedup by day).

These are ESTIMATES by design — download/clone counts include automation and
can't be deduplicated to people; the numbers are labelled accordingly and the
mirror-excluded / unique variants are chosen to be the least-noisy signal.

The README references the JSON via a shields endpoint URL, e.g.:
    https://img.shields.io/endpoint?url=<raw json url>&style=flat-square
so shields renders the current committed number with no third-party number source.

Run in CI on a schedule; commit the result back. Locally:
    GITHUB_TOKEN=$(gh auth token) python bin/gen_download_badges.py \
        --repo skynetcmd/m3-memory --package m3-memory

Exit codes: 0 = badges written (or unchanged); 2 = hard error (network/auth).
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

GH_API = "https://api.github.com"
PYPISTATS_API = "https://pypistats.org/api/packages"

_UA = "m3-download-badge-generator"


def _fmt(n: int) -> str:
    """Human-compact count: 9909 -> '9.9k', 32273 -> '32k', 1200000 -> '1.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        k = n / 1000.0
        return f"{k:.1f}k".replace(".0k", "k") if k < 100 else f"{round(k)}k"
    m = n / 1_000_000.0
    return f"{m:.1f}M".replace(".0M", "M")


def _get_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url)  # nosec B310 — fixed https hosts below
    req.add_header("User-Agent", _UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return json.load(resp)


def _shields_endpoint(label: str, message: str, color: str) -> dict:
    """A shields.io 'endpoint' schema object (schemaVersion 1)."""
    return {
        "schemaVersion": 1,
        "label": label,
        "message": message,
        "color": color,
    }


# ── PyPI ─────────────────────────────────────────────────────────────────────

def pypi_total_without_mirrors(package: str) -> int:
    """Sum of daily downloads (category without_mirrors) over pypistats' window.

    without_mirrors excludes PyPI mirror bots (bandersnatch etc.), so it best
    approximates real installs. For a package younger than pypistats' ~180-day
    retention this is effectively the all-time total.
    """
    data = _get_json(f"{PYPISTATS_API}/{package}/overall")
    rows = data.get("data", [])
    return sum(
        int(r.get("downloads", 0))
        for r in rows
        if r.get("category") == "without_mirrors"
    )


# ── GitHub clones (running total across the 14-day window) ───────────────────

def github_clone_days(repo: str, token: str) -> list[tuple[str, int]]:
    """Return [(YYYY-MM-DD, unique_clones), ...] from the traffic API (14-day)."""
    url = f"{GH_API}/repos/{repo}/traffic/clones"
    data = _get_json(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    out: list[tuple[str, int]] = []
    for d in data.get("clones", []):
        ts = str(d.get("timestamp", ""))[:10]  # date only
        if ts:
            out.append((ts, int(d.get("uniques", 0))))
    return out


def accumulate_clone_total(history_path: Path, fresh_days: list[tuple[str, int]]) -> int:
    """Merge the 14-day window into a persisted per-day history (dedup by date,
    newest value wins) and return the running total of unique clones.

    GitHub only exposes 14 days, so a scheduled run stitches successive windows
    into an all-time-since-first-run total. A day already in history is updated to
    the latest reported value (the same day can appear in two overlapping windows).
    """
    history: dict[str, int] = {}
    if history_path.exists():
        try:
            history = {
                str(k): int(v)
                for k, v in json.loads(history_path.read_text("utf-8")).items()
            }
        except (json.JSONDecodeError, ValueError, TypeError):
            history = {}
    for day, uniques in fresh_days:
        # Newest window's value for a day wins (it's the authoritative latest).
        history[day] = uniques
    # Persist sorted for a stable, reviewable diff.
    ordered = dict(sorted(history.items()))
    history_path.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    return sum(ordered.values())


# ── write + main ─────────────────────────────────────────────────────────────

def _write_json(path: Path, obj: dict) -> bool:
    """Write pretty JSON; return True if the file content changed."""
    new = json.dumps(obj, indent=2) + "\n"
    old = path.read_text("utf-8") if path.exists() else None
    if new == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate PyPI + GitHub download badges.")
    ap.add_argument("--repo", default="skynetcmd/m3-memory", help="owner/name")
    ap.add_argument("--package", default="m3-memory", help="PyPI package name")
    ap.add_argument("--badges-dir", default="docs/badges", help="output directory")
    args = ap.parse_args()

    badges = Path(args.badges_dir)
    changed = False

    # PyPI — never fatal to the whole run if one source is down; report + continue.
    try:
        pypi_total = pypi_total_without_mirrors(args.package)
        if _write_json(
            badges / "pypi-downloads.json",
            _shields_endpoint("pypi downloads", f"{_fmt(pypi_total)}", "blue"),
        ):
            changed = True
        print(f"[pypi] total (without mirrors): {pypi_total:,} -> {_fmt(pypi_total)}")
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
        print(f"[pypi] WARNING: could not fetch downloads ({type(e).__name__}: {e})",
              file=sys.stderr)

    # GitHub clones — requires a token that can read the private traffic API.
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("M3_STARGAZER") or ""
    if not token:
        print("[github] no GITHUB_TOKEN/M3_STARGAZER — skipping clone badge "
              "(traffic API needs an admin/collaborator token).", file=sys.stderr)
    else:
        try:
            days = github_clone_days(args.repo, token)
            total = accumulate_clone_total(badges / "clone-history.json", days)
            if _write_json(
                badges / "github-clones.json",
                _shields_endpoint("github clones", f"{_fmt(total)}", "24292e"),
            ):
                changed = True
            print(f"[github] running unique-clone total: {total:,} -> {_fmt(total)}")
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            print(f"[github] WARNING: could not fetch clone traffic "
                  f"({type(e).__name__}: {e})", file=sys.stderr)

    print("changed" if changed else "unchanged")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(2) from None
