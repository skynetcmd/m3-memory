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


# The mermaid-cli version this suite is validated against. Install exactly this
# (`npm i -g @mermaid-js/mermaid-cli@<ver>`); the npx fallback fetches the same.
MERMAID_CLI_VERSION = "11.17.0"
_NPX_OPT_IN = "M3_ALLOW_NPX_MERMAID"


def _mermaid_cli() -> list[str] | None:
    """Resolve the launcher to its ABSOLUTE path.

    On Windows these are ``.CMD`` shims, so passing the bare name to
    subprocess raises WinError 2 and the test skipped itself on the very
    machine that had mermaid-cli installed -- a false green.

    An installed ``mmdc`` is used when present. The ``npx`` fallback DOWNLOADS
    AND EXECUTES a package from the npm registry during a test run, so it is
    opt-in (``M3_ALLOW_NPX_MERMAID=1``) and fetches the exact pinned version,
    never a floating ``@11``.
    """
    found = shutil.which("mmdc")
    if found:
        return [found]
    if os.environ.get(_NPX_OPT_IN) == "1":
        npx = shutil.which("npx")
        if npx:
            return [npx, "-y", "-q", f"@mermaid-js/mermaid-cli@{MERMAID_CLI_VERSION}"]
    return None


def _render(cli: list[str], src: Path, cfg: Path) -> tuple[bool, subprocess.CompletedProcess]:
    """Render one .mmd and report whether an SVG actually came out.

    Success is the OUTPUT FILE, not the exit code: mermaid-cli has been observed
    to exit 0 while writing nothing. Single owner so the canary and the real
    blocks are judged by identical rules — if they could drift, a canary that
    "passed" would prove nothing about the blocks.
    """
    out = src.with_suffix(".svg")
    try:
        proc = subprocess.run(
            [*cli, "-i", str(src), "-o", str(out), "-p", str(cfg)],
            capture_output=True, text=True, timeout=180,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        pytest.skip(f"mermaid-cli unusable: {exc}")
    return (out.exists() and out.stat().st_size > 0), proc


@pytest.mark.slow
def test_every_mermaid_block_renders() -> None:
    """Authoritative check: hand each block to the real Mermaid parser."""
    cli = _mermaid_cli()
    if cli is None:
        pytest.skip(f"no mmdc installed (npm i -g @mermaid-js/mermaid-cli@{MERMAID_CLI_VERSION}); "
                    f"the npx download fallback is off unless {_NPX_OPT_IN}=1")
    if os.environ.get("M3_SKIP_MERMAID_RENDER") == "1":
        pytest.skip("M3_SKIP_MERMAID_RENDER=1")

    failures = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Puppeteer needs --no-sandbox in most CI containers.
        cfg = tmp / "pptr.json"
        cfg.write_text(json.dumps({"args": ["--no-sandbox"]}), encoding="utf-8")

        # Does the TOOLCHAIN work at all? Answered once, with a diagram whose
        # correctness is not in question, BEFORE any real block is judged.
        #
        # This used to be inferred per block from the error text, matching
        # "MODULE_NOT_FOUND" / "command not found". That list could only ever
        # name the spellings already seen: under WSL, `npx` resolved to the
        # WINDOWS shim on the inherited Windows PATH, a /bin/sh script whose
        # `exec node` failed as `exec: node: not found` — a spelling absent from
        # the list. Every block then "failed to parse", and the suite reported
        # ten documentation defects on a machine whose docs were fine.
        #
        # A canary removes the guesswork instead of extending it: if a diagram
        # known to be valid will not render, the environment is broken and the
        # test has nothing to say about our docs. Anything that fails after the
        # canary passed is a genuine documentation defect.
        canary = tmp / "_canary.mmd"
        canary.write_text("graph TD\n  A[a] --> B[b]\n", encoding="utf-8")
        ok, proc = _render(cli, canary, cfg)
        if not ok:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            pytest.skip("mermaid-cli cannot render a known-good diagram, so this "
                        "environment cannot judge ours: "
                        + (err[-1][:200] if err else "no output and no error"))

        for path, idx, body in _blocks():
            src = tmp / f"{Path(path).stem}_{idx}.mmd"
            src.write_text(body, encoding="utf-8")
            ok, proc = _render(cli, src, cfg)
            if ok:
                continue
            err_str = proc.stderr or proc.stdout or ""
            err = err_str.strip().splitlines()
            detail = next((ln for ln in err if "Parse error" in ln), "")
            failures.append(f"{path} block {idx}: {detail or (err[-1][:160] if err else 'no output and no error')}")

    assert not failures, "mermaid blocks that do not parse:\n  " + "\n  ".join(failures)


def test_npx_fallback_is_off_by_default(monkeypatch):
    """Without the opt-in, a box with npx but no mmdc must NOT download code."""
    monkeypatch.delenv(_NPX_OPT_IN, raising=False)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/npx" if n == "npx" else None)
    assert _mermaid_cli() is None


def test_npx_fallback_when_opted_in_fetches_the_exact_pin(monkeypatch):
    monkeypatch.setenv(_NPX_OPT_IN, "1")
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/npx" if n == "npx" else None)
    cli = _mermaid_cli()
    assert cli is not None and cli[-1] == f"@mermaid-js/mermaid-cli@{MERMAID_CLI_VERSION}"
    assert re.fullmatch(r"\d+\.\d+\.\d+", MERMAID_CLI_VERSION), "pin an exact version, not a range"


def test_installed_mmdc_wins_over_npx(monkeypatch):
    monkeypatch.setenv(_NPX_OPT_IN, "1")
    monkeypatch.setattr(shutil, "which", lambda n: f"/usr/bin/{n}")
    assert _mermaid_cli() == ["/usr/bin/mmdc"]
