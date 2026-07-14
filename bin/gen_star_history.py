#!/usr/bin/env python3
"""Generate docs/star-history.svg from the GitHub stargazers API.

GitHub now requires an authenticated token to read star data, so the public
star-history.com embed no longer renders anonymously in the README. This script
fetches the repo's stargazer timestamps with a token (the CI GITHUB_TOKEN is
enough — only `Metadata: read` / `contents: read` is needed) and renders a
self-contained cumulative-stars line chart as an SVG committed into the repo.
The README embeds that committed file, so no token is ever exposed publicly and
no third-party service is involved.

Run in CI on a schedule; commit the result back to main. Locally:

    GITHUB_TOKEN=$(gh auth token) python bin/gen_star_history.py \
        --repo skynetcmd/m3-memory --out docs/star-history.svg

Exit codes: 0 = SVG written (or unchanged), 2 = hard error (auth/network).
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
STAR_ACCEPT = "application/vnd.github.star+json"  # includes `starred_at`


def _get(url: str, token: str) -> tuple[list, dict]:
    req = urllib.request.Request(url)
    req.add_header("Accept", STAR_ACCEPT)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "m3-star-history-generator")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted host)
        body = json.loads(resp.read().decode("utf-8"))
        return body, dict(resp.headers)


def _parse_next(link_header: str | None) -> str | None:
    """Extract the rel="next" URL from a GitHub Link header, if present."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segs = part.split(";")
        if len(segs) < 2:
            continue
        url = segs[0].strip().strip("<>")
        if any('rel="next"' in s for s in segs[1:]):
            return url
    return None


def fetch_starred_at(repo: str, token: str) -> list[datetime]:
    """All stargazer timestamps, oldest first. Paginates the whole history."""
    times: list[datetime] = []
    url = f"{API}/repos/{repo}/stargazers?per_page=100"
    while url:
        body, headers = _get(url, token)
        for row in body:
            ts = row.get("starred_at") if isinstance(row, dict) else None
            if ts:
                times.append(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
        url = _parse_next(headers.get("Link"))
    times.sort()
    return times


def render_svg(repo: str, times: list[datetime], generated: datetime) -> str:
    """A theme-aware cumulative-stars line chart. Self-contained, no external refs."""
    W, H = 800, 400
    pad_l, pad_r, pad_t, pad_b = 60, 20, 30, 40
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    total = len(times)

    # Build cumulative series as (x_fraction, cumulative_count).
    if total >= 2:
        t0, t1 = times[0], times[-1]
        span = (t1 - t0).total_seconds() or 1.0
        pts = [(((t - t0).total_seconds() / span), i + 1) for i, t in enumerate(times)]
        # ensure the line starts at zero on the left edge
        pts = [(0.0, 0)] + pts
    else:
        pts = [(0.0, 0), (1.0, total)]

    max_y = max(total, 1)

    def px(fx: float) -> float:
        return pad_l + fx * plot_w

    def py(count: int) -> float:
        return pad_t + plot_h - (count / max_y) * plot_h

    poly = " ".join(f"{px(fx):.1f},{py(c):.1f}" for fx, c in pts)
    area = f"{px(0):.1f},{py(0):.1f} " + poly + f" {px(1):.1f},{py(0):.1f}"

    # y-axis gridlines (0, mid, max)
    yticks = sorted({0, max_y // 2, max_y})
    grid = "".join(
        f'<line x1="{pad_l}" y1="{py(y):.1f}" x2="{W - pad_r}" y2="{py(y):.1f}" class="grid"/>'
        f'<text x="{pad_l - 8}" y="{py(y) + 4:.1f}" text-anchor="end" class="tick">{y}</text>'
        for y in yticks
    )

    first = times[0].strftime("%Y-%m-%d") if times else "—"
    last = times[-1].strftime("%Y-%m-%d") if times else "—"
    stamp = generated.strftime("%Y-%m-%d")

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="Star history for {repo}: {total} stars">
  <title>Star history for {repo}</title>
  <style>
    .bg {{ fill: #ffffff; }}
    .grid {{ stroke: #e1e4e8; stroke-width: 1; }}
    .tick, .axis {{ fill: #57606a; font: 12px -apple-system, Segoe UI, sans-serif; }}
    .title {{ fill: #24292f; font: 600 15px -apple-system, Segoe UI, sans-serif; }}
    .line {{ fill: none; stroke: #2f81f7; stroke-width: 2.5; stroke-linejoin: round; }}
    .area {{ fill: #2f81f7; opacity: 0.10; }}
    @media (prefers-color-scheme: dark) {{
      .bg {{ fill: #0d1117; }}
      .grid {{ stroke: #21262d; }}
      .tick, .axis {{ fill: #8b949e; }}
      .title {{ fill: #e6edf3; }}
    }}
  </style>
  <rect class="bg" x="0" y="0" width="{W}" height="{H}" rx="6"/>
  <text class="title" x="{pad_l}" y="20">⭐ {repo} — {total} stars</text>
  {grid}
  <polygon class="area" points="{area}"/>
  <polyline class="line" points="{poly}"/>
  <text class="axis" x="{pad_l}" y="{H - 12}" text-anchor="start">{first}</text>
  <text class="axis" x="{W - pad_r}" y="{H - 12}" text-anchor="end">{last}</text>
  <text class="axis" x="{W // 2}" y="{H - 12}" text-anchor="middle">generated {stamp}</text>
</svg>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default="skynetcmd/m3-memory")
    ap.add_argument("--out", default="docs/star-history.svg")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("ERROR: GITHUB_TOKEN (or GH_TOKEN) is required.", file=sys.stderr)
        return 2

    try:
        times = fetch_starred_at(args.repo, token)
    except urllib.error.HTTPError as e:
        print(f"ERROR: GitHub API {e.code} for {args.repo}: {e.reason}", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as e:
        print(f"ERROR: network failure: {e}", file=sys.stderr)
        return 2

    # Deterministic generated-date from the latest star (or today if no stars),
    # so a re-run with no new stars produces a byte-identical SVG and no commit.
    generated = times[-1] if times else datetime.now(timezone.utc)
    svg = render_svg(args.repo, times, generated)

    out = args.out
    existing = None
    if os.path.exists(out):
        with open(out, encoding="utf-8") as fh:
            existing = fh.read()
    if existing == svg:
        print(f"star-history.svg unchanged ({len(times)} stars).")
        return 0

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(svg)
    print(f"Wrote {out} ({len(times)} stars).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
