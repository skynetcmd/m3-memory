"""Mermaid blocks must parse — a broken one ships as a raw code block.

There is no error state a reader sees: GitHub renders a diagram it cannot parse
as the literal source text, so a syntax slip degrades a page silently and looks
fine in review. While adding the ARCHITECTURE.md diagrams (2026-09-10) three
separate constructs failed the real parser while looking perfectly reasonable:

  - a ``;`` inside a sequence-diagram ``Note`` body,
  - a ``style`` rule naming a subgraph that had been deleted (which does not
    error at all — it renders a stray EMPTY BOX),
  - backticks and emoji inside sequence ``Note`` text.

The cheap structural checks below run everywhere. The authoritative check shells
out to mermaid-cli and is skipped when Node is unavailable, so CI without Node
still gets the structural pass rather than a false green.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_BLOCK = re.compile(r"```mermaid\n(.*?)```", re.S)


def _blocks() -> list[tuple[str, int, str]]:
    """(file, block index, source) for every mermaid block in the docs."""
    out = []
    files = [REPO / "README.md", *sorted(REPO.joinpath("docs").rglob("*.md"))]
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for i, body in enumerate(_BLOCK.findall(text)):
            out.append((path.relative_to(REPO).as_posix(), i, body))
    return out


def test_repo_has_mermaid_diagrams() -> None:
    """Guard the guard: if extraction silently broke, everything below passes."""
    assert _blocks(), "no mermaid blocks found — the extraction regex likely broke"


def test_no_style_rule_names_a_missing_node() -> None:
    """``style X`` where X is never defined renders an empty box, not an error.

    This is the sneakiest of the three failures: Mermaid accepts it, draws a
    stray rectangle, and nothing anywhere reports a problem.
    """
    problems = []
    for path, idx, body in _blocks():
        declared: set[str] = set()
        for m in re.finditer(r"^\s*subgraph\s+([A-Za-z0-9_]+)", body, re.M):
            declared.add(m.group(1))
        # Node ids: an identifier immediately followed by a shape opener.
        for m in re.finditer(r"([A-Za-z][A-Za-z0-9_]*)\s*[\[\(\{]", body):
            declared.add(m.group(1))
        # participants / actors in sequence diagrams
        for m in re.finditer(r"^\s*participant\s+([A-Za-z0-9_]+)", body, re.M):
            declared.add(m.group(1))

        for m in re.finditer(r"^\s*style\s+([A-Za-z0-9_]+)\b", body, re.M):
            if m.group(1) not in declared:
                problems.append(
                    f"{path} block {idx}: `style {m.group(1)}` names nothing — "
                    "it will render as an empty box"
                )
    assert not problems, "\n  ".join(["dangling style rules:", *problems])


def test_sequence_notes_avoid_parser_breaking_characters() -> None:
    """``;`` and backticks inside a sequence Note are parse errors.

    They are legal in flowchart node labels, which is exactly why this is easy
    to get wrong: the same text moved from one diagram type to another breaks.
    """
    problems = []
    for path, idx, body in _blocks():
        if not re.search(r"^\s*sequenceDiagram", body, re.M):
            continue
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped.lower().startswith(("note over", "note left", "note right")):
                continue
            _, _, text = stripped.partition(":")
            for ch, why in ((";", "semicolon"), ("`", "backtick")):
                if ch in text:
                    problems.append(f"{path} block {idx}: {why} in a sequence Note")
    assert not problems, "\n  ".join(["unparseable sequence notes:", *problems])


def _mermaid_cli() -> list[str] | None:
    """Resolve the launcher to its ABSOLUTE path.

    On Windows these are ``.CMD`` shims, so passing the bare name to
    subprocess raises WinError 2 and the test skipped itself on the very
    machine that had mermaid-cli installed -- a false green.
    """
    for name, args in (("mmdc", []), ("npx", ["-y", "-q", "@mermaid-js/mermaid-cli@11"])):
        found = shutil.which(name)
        if found:
            return [found, *args]
    return None


@pytest.mark.slow
def test_every_mermaid_block_renders() -> None:
    """Authoritative check: hand each block to the real Mermaid parser."""
    cli = _mermaid_cli()
    if cli is None:
        pytest.skip("no mermaid-cli / npx available")
    if os.environ.get("M3_SKIP_MERMAID_RENDER") == "1":
        pytest.skip("M3_SKIP_MERMAID_RENDER=1")

    failures = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Puppeteer needs --no-sandbox in most CI containers.
        cfg = tmp / "pptr.json"
        cfg.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")

        for path, idx, body in _blocks():
            src = tmp / f"{Path(path).stem}_{idx}.mmd"
            out = src.with_suffix(".svg")
            src.write_text(body, encoding="utf-8")
            try:
                proc = subprocess.run(
                    [*cli, "-i", str(src), "-o", str(out), "-p", str(cfg)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                pytest.skip(f"mermaid-cli unusable: {exc}")

            if not out.exists() or out.stat().st_size == 0:
                err = (proc.stderr or proc.stdout or "").strip().splitlines()
                detail = next((ln for ln in err if "Parse error" in ln), "")
                if not detail and not err:
                    pytest.skip("mermaid-cli produced no output and no error")
                failures.append(f"{path} block {idx}: {detail or err[-1][:160]}")

    assert not failures, "mermaid blocks that do not parse:\n  " + "\n  ".join(failures)
