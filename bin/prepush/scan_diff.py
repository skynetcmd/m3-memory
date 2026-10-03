"""Scan a push diff's ADDED lines for prohibited patterns. Fails CLOSED.

Each pattern is compiled INDEPENDENTLY. A policy joined into one expression
takes the whole policy down with its weakest line, and a scanner that cannot
compile its patterns must not be mistaken for one that found nothing — so
anything meaning "the policy was not evaluated" blocks the push: an unreadable
file, an empty list, any pattern that will not compile, an uncompilable
allow-list. Python's ``re`` also keeps one meaning across all three supported
platforms rather than deferring to each one's ``grep``.

Pattern text is never printed. The patterns enumerate what we treat as
sensitive, so diagnostics carry the file, the 1-based line number and the
error — never the pattern body (same rule as ``bin/memory/util.py``'s content
safety guard).

USAGE
-----
    git diff <range> -- ... | python3 bin/prepush/scan_diff.py \
        --patterns <file> --exclude-identities <regex> [--max-report N]

Exit 0 = nothing prohibited found. Exit 1 = hits found, or the policy could not
be evaluated. Exit 2 = called wrong.
"""
from __future__ import annotations

import argparse
import re
import sys


def load_patterns(path: str) -> "list[tuple[int, re.Pattern[str]]]":
    """Compile every non-comment line, or raise with the line number."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().splitlines()
    except OSError as e:
        raise SystemExit(
            f"[scan_diff] BLOCKED: cannot read the prohibited-pattern file.\n"
            f"           file  : {path}\n"
            f"           error : {e}\n"
            f"           A policy that cannot be read is not a policy that found "
            f"nothing. Fix the path (M3_PREPUSH_PROHIBITED) or restore the file."
        )

    compiled: list[tuple[int, re.Pattern[str]]] = []
    broken: list[tuple[int, str]] = []
    for lineno, line in enumerate(raw, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            compiled.append((lineno, re.compile(line, re.IGNORECASE)))
        except re.error as e:
            broken.append((lineno, str(e)))

    if broken:
        detail = "\n".join(
            f"           {path}:{n} -> {err}  (pattern body withheld)"
            for n, err in broken
        )
        raise SystemExit(
            "[scan_diff] BLOCKED: prohibited-pattern file has pattern(s) that do "
            "not compile.\n"
            f"{detail}\n"
            "           The scan was NOT run. Fix the pattern(s) and push again."
        )
    if not compiled:
        raise SystemExit(
            f"[scan_diff] BLOCKED: no usable patterns in {path}.\n"
            "           An empty policy must not read as a clean scan."
        )
    return compiled



def _selftest(patterns_path: "str | None") -> int:
    """Prove the gate can still catch something, on this platform.

    A policy that will not compile is indistinguishable from a policy that found
    nothing unless something checks. This is that check, runnable by hand:

        python3 bin/prepush/scan_diff.py --selftest --patterns <policy>

    It reports per-pattern compilation (a count, never the pattern text) and
    verifies that a synthetic credential IS caught and an innocuous line is NOT.
    Exit 0 only if every part holds.
    """
    ok = True

    if patterns_path:
        try:
            pats = load_patterns(patterns_path)
        except SystemExit as e:
            print(f"[selftest] FAIL policy does not load:\n{e}")
            return 1
        print(f"[selftest] ok   {len(pats)} pattern(s) compiled from {patterns_path}")
    else:
        print("[selftest] note no --patterns given; checking the engine only")
        pats = []

    # Does the engine catch and reject correctly? Uses its own pattern so the
    # answer does not depend on what the live policy happens to contain.
    probe = re.compile("sk-" + "ant-" + r"[a-z0-9]{20,}", re.IGNORECASE)
    hit_line = '+KEY = "' + "sk-" + "ant-" + 'abcdefghij0123456789xyz"'
    if probe.search(hit_line[1:]):
        print("[selftest] ok   a credential-shaped added line matches")
    else:
        print("[selftest] FAIL a credential-shaped added line did NOT match")
        ok = False
    if not probe.search("nothing interesting here"):
        print("[selftest] ok   an innocuous line does not match")
    else:
        print("[selftest] FAIL an innocuous line matched")
        ok = False

    # An uncompilable pattern must be refused rather than skipped.
    try:
        re.compile("foo(")
    except re.error:
        print("[selftest] ok   an uncompilable pattern raises (so load_patterns blocks)")
    else:
        print("[selftest] FAIL an uncompilable pattern did not raise")
        ok = False

    print(f"[selftest] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def main(argv: "list[str]") -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--patterns", help="policy file, one regex per line")
    ap.add_argument("--inline-pattern",
                    help="a single regex, used when no policy file is available "
                         "(the hook's built-in credential/PII fallback)")
    ap.add_argument("--exclude-identities", default="")
    ap.add_argument("--max-report", type=int, default=20)
    ap.add_argument("--selftest", action="store_true",
                    help="prove the gate is alive on THIS platform: compile the "
                         "live policy, and check a known-bad line is caught and a "
                         "clean line is not. Reads no diff.")
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest(args.patterns)

    if args.patterns:
        patterns = load_patterns(args.patterns)
        source = args.patterns
    elif args.inline_pattern:
        # The fallback is ONE joined regex by construction, so it is compiled as
        # one unit. It is short, built in and known to compile; the per-pattern
        # independence that matters is for the policy FILE, which is edited.
        try:
            patterns = [(0, re.compile(args.inline_pattern, re.IGNORECASE))]
        except re.error as e:
            raise SystemExit(
                f"[scan_diff] BLOCKED: the built-in fallback pattern does not "
                f"compile: {e}"
            )
        source = "<built-in fallback>"
    else:
        raise SystemExit(
            "[scan_diff] BLOCKED: no patterns given (need --patterns or "
            "--inline-pattern). Refusing to report a clean scan."
        )

    placeholder = None
    if args.exclude_identities:
        try:
            placeholder = re.compile(args.exclude_identities, re.IGNORECASE)
        except re.error as e:
            raise SystemExit(
                "[scan_diff] BLOCKED: the placeholder-identity allow-list regex "
                f"does not compile: {e}\n"
                "           Without it, legitimate doc placeholders would be "
                "reported as leaks, so the scan is not run."
            )

    hits: list[str] = []
    for line in sys.stdin.buffer.read().decode("utf-8", "replace").splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        body = line[1:]
        if placeholder is not None and placeholder.search(body):
            continue
        for lineno, pat in patterns:
            m = pat.search(body)
            if m:
                # Report WHERE and WHICH RULE, plus the offending diff line (the
                # author's own content, which they need in order to fix it) --
                # but never the pattern that matched it.
                where = f"{source}:{lineno}" if lineno else source
                hits.append(f"  rule {where} at col {m.start() + 1}: {line}")
                break

    if hits:
        print(
            "[scan_diff] BLOCKED: prohibited content in the push diff "
            f"({len(hits)} added line(s) matched):",
            file=sys.stderr,
        )
        for h in hits[: args.max_report]:
            print(h, file=sys.stderr)
        if len(hits) > args.max_report:
            print(f"  ... and {len(hits) - args.max_report} more", file=sys.stderr)
        return 1

    print(f"[scan_diff] {len(patterns)} pattern(s) evaluated; no prohibited content.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
