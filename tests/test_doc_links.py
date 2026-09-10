"""Every internal doc link must resolve — file path and #anchor alike.

Broken links were fixed twice by hand in one session (2026-09-10): 12 CHANGELOG
entries missing a ``docs/`` prefix, then 4 dead anchors, one of which was
introduced by the very commit that fixed the others. Nothing caught either
round, because a markdown link is not executed.

Anchors are the half that rots invisibly: renaming a heading silently breaks
every link to it, and the link still *looks* fine in review.

GitHub's slug rules, which this mirrors:
  lowercase -> drop punctuation and emoji (keeping the spaces they sat in) ->
  spaces become dashes -> runs of dashes are PRESERVED.
So ``## Admin & Sync`` is ``#admin--sync`` (two dashes, from the dropped ``&``),
and a heading that opens with an emoji gains a LEADING dash. Both forms are
accepted here rather than guessed at, plus explicit ``<a id=...>`` targets.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Markdown links, minus images (![...]) and autolinks.
_LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)\)")
_HTML_ANCHOR = re.compile(r'<a\s+(?:id|name)="([^"]+)"')


def _anchors(text: str) -> set[str]:
    """Every fragment a link in this file could legitimately target."""
    out: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("#"):
            continue
        t = re.sub(r"^#+\s*", "", line).strip().lower()
        # Drop punctuation and emoji but KEEP the whitespace they occupied --
        # that is what produces GitHub's doubled dashes and leading dash.
        t = re.sub(r"[^\w\s-]", "", t, flags=re.UNICODE)
        base = t.strip().replace(" ", "-")
        out.add(base)
        out.add("-" + base)               # emoji-led heading
        out.add(re.sub(r"-+", "-", base))  # tolerate a collapsed form
    out |= set(_HTML_ANCHOR.findall(text))
    return out


# Placeholders that appear inside prose ABOUT link syntax, not as real links --
# e.g. WIKI.md explaining that the vault emits "`[text](page.md)`" hyperlinks.
_SYNTAX_EXAMPLES = {"page.md", "page.md#section", "path/to/file.md", "other-page.md"}

# CHANGELOG is history: an entry may correctly reference a path that has since
# been removed (docs/audits/ was deleted after its entry was written). Rewriting
# it would falsify the record, so only its ANCHORS are checked, not file paths.
_HISTORY_ONLY = {"CHANGELOG.md"}


def _doc_files() -> list[Path]:
    files = [REPO / "README.md", REPO / "INSTALL.md", REPO / "CHANGELOG.md"]
    files += sorted(REPO.joinpath("docs").rglob("*.md"))
    return [f for f in files if f.exists()]


def test_internal_doc_links_resolve() -> None:
    """No internal link may point at a missing file or a missing anchor."""
    problems: list[str] = []
    cache: dict[Path, set[str]] = {}

    for path in _doc_files():
        text = path.read_text(encoding="utf-8")
        own = _anchors(text)
        rel = path.relative_to(REPO).as_posix()

        for href in _LINK.findall(text):
            if href.startswith(("http://", "https://", "mailto:", "tel:")):
                continue
            if href in _SYNTAX_EXAMPLES:
                continue

            if href.startswith("#"):
                if href[1:] and href[1:] not in own:
                    problems.append(f"{rel}: dead same-page anchor {href}")
                elif not href[1:]:
                    problems.append(f"{rel}: empty link target `(#)`")
                continue

            target_path, _, frag = href.partition("#")
            if not target_path:
                continue
            target = (path.parent / target_path).resolve()

            if not target.exists():
                if path.name not in _HISTORY_ONLY:
                    problems.append(f"{rel}: missing file {href}")
                continue
            if frag and target.suffix == ".md":
                if target not in cache:
                    cache[target] = _anchors(target.read_text(encoding="utf-8"))
                if frag not in cache[target]:
                    problems.append(f"{rel}: dead anchor {href}")

    assert not problems, "broken internal doc links:\n  " + "\n  ".join(problems)
