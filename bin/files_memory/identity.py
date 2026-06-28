"""File identity resolution + content hashing.

A file's `identity_key` is the stable handle that links file_node versions
of the same logical document across re-ingestion. Resolution order:

  1. Explicit `m3_doc_id` declared in the file (frontmatter / header
     comment). Wins unconditionally — survives rename and move.
  2. The absolute path (default). Survives content changes; breaks on
     rename/move (a renamed file is treated as new + old goes orphan).

Phase 3 will add a heuristic rename-detection helper that surfaces
"file X looks like a rename of file Y" candidates for user confirmation.
We never auto-merge by content similarity — false positives are
unrecoverable without audit.

Public API:
    file_content_sha256(path) -> str
    file_content_sha256_batch(paths) -> dict[str, str | None]
    detect_m3_doc_id(path, text=None) -> str | None
    resolve_identity_key(path, text=None) -> str
    filetype_for(path) -> str
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("files_memory.identity")

# ──────────────────────────────────────────────────────────────────────────────
# Filetype detection (extension-based; mime sniffing is a phase-2 upgrade)
# ──────────────────────────────────────────────────────────────────────────────
# Normalized filetype names. The chunker dispatcher keys on these.
FILETYPE_BY_EXT: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".rst": "rst",
    ".txt": "text",
    ".log": "text",
    ".pdf": "pdf",
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ini": "ini",
    ".cfg": "ini",
    ".conf": "ini",
    ".csv": "csv",
    ".tsv": "tsv",
    ".html": "html",
    ".htm": "html",
    ".xml": "xml",
    ".epub": "epub",
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".xlsx": "xlsx",
    ".xls": "xls",
    ".ipynb": "notebook",
    ".tex": "latex",
}


def filetype_for(path: str | Path) -> str:
    """Return a normalized filetype string. Unknown → 'unknown'."""
    ext = os.path.splitext(str(path))[1].lower()
    return FILETYPE_BY_EXT.get(ext, "unknown")


# ──────────────────────────────────────────────────────────────────────────────
# Content hashing
# ──────────────────────────────────────────────────────────────────────────────
_HASH_CHUNK = 64 * 1024  # 64 KiB; balances syscall overhead vs RSS


def file_content_sha256(path: str | Path) -> str:
    """Stream a file through SHA-256. Memory-bounded for large files."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(_HASH_CHUNK)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def file_content_sha256_batch(paths: list[str]) -> dict[str, str | None]:
    """Hash many files at once, returning ``{path: hex_sha256 | None}``.

    On batches of files (e.g. the staleness re-hash sweep) this routes through
    the native ``m3_core_rs.hash_files`` — rayon-parallel reads + hashing with
    the GIL released — which is markedly faster than a serial Python loop on
    large trees. The digest is byte-identical to ``file_content_sha256``.

    Unreadable files map to ``None`` (parity with the Python staleness path,
    which logs and skips rather than aborting the batch). Falls back to the
    per-file Python path when the native extension is unavailable or disabled
    via ``M3_CORE_RS_DISABLE`` — a missing/old wheel only makes this slower.
    """
    if not paths:
        return {}

    if os.environ.get("M3_CORE_RS_DISABLE", "0") != "1":
        try:
            import m3_core_rs
            if hasattr(m3_core_rs, "hash_files"):
                out: dict[str, str | None] = {}
                for rec in m3_core_rs.hash_files(paths):
                    out[rec["path"]] = rec["sha256"]  # None on read error
                return out
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("native hash_files unavailable, falling back: %s", e)

    # Pure-Python fallback.
    result: dict[str, str | None] = {}
    for p in paths:
        try:
            result[p] = file_content_sha256(p)
        except OSError as e:
            logger.debug("hash failed for %s: %s", p, e)
            result[p] = None
    return result


# ──────────────────────────────────────────────────────────────────────────────
# m3_doc_id detection
# ──────────────────────────────────────────────────────────────────────────────
# Three accepted forms, in priority order:
#   1. YAML frontmatter (Markdown / docs):  m3_doc_id: my-doc-id
#   2. Inline marker (any text file):       <!-- m3-doc-id: my-doc-id -->
#                                           # m3-doc-id: my-doc-id   (code)
#   3. (future) office-doc custom property — phase 2/3
#
# We scan only the first 4 KiB to bound cost. If you want m3_doc_id picked
# up, put it near the top of the file.
_HEAD_SCAN_BYTES = 4096

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?\n)?---\s*$",
    re.MULTILINE | re.DOTALL,
)
_FRONTMATTER_FIELD_RE = re.compile(
    r"^\s*m3[_-]doc[_-]id\s*:\s*['\"]?([A-Za-z0-9_./:-]+)['\"]?\s*$",
    re.MULTILINE,
)
_INLINE_RE = re.compile(
    r"m3[_-]doc[_-]id\s*[:=]\s*['\"]?([A-Za-z0-9_./:-]+)['\"]?",
)


def detect_m3_doc_id(path: str | Path, text: str | None = None) -> str | None:
    """Look for an explicit m3_doc_id declaration in the file head.

    Args:
        path: file path (used only when `text` is None — to read the head).
        text: optionally, the already-loaded file text. Skip disk I/O.

    Returns:
        The doc_id string if found, else None.
    """
    if text is None:
        try:
            with open(path, "rb") as f:
                raw = f.read(_HEAD_SCAN_BYTES)
            text = raw.decode("utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError) as e:
            logger.debug("detect_m3_doc_id: read failed for %s: %s", path, e)
            return None

    if not text:
        return None

    # 1. Try YAML frontmatter (only valid at file start).
    head = text[:_HEAD_SCAN_BYTES]
    fm = _FRONTMATTER_RE.match(head)
    if fm:
        fm_body = fm.group(1) or ""
        field = _FRONTMATTER_FIELD_RE.search(fm_body)
        if field:
            return field.group(1).strip()

    # 2. Inline marker anywhere in the head.
    inline = _INLINE_RE.search(head)
    if inline:
        return inline.group(1).strip()

    return None


def resolve_identity_key(path: str | Path, text: str | None = None) -> str:
    """Resolve the identity_key for a file.

    Returns m3_doc_id if declared; otherwise the absolute path. Always a
    non-empty string.
    """
    declared = detect_m3_doc_id(path, text=text)
    if declared:
        return f"doc_id:{declared}"
    return f"path:{os.path.abspath(str(path))}"


# ──────────────────────────────────────────────────────────────────────────────
# Binary detection (sniff first KiB)
# ──────────────────────────────────────────────────────────────────────────────
def looks_binary(path: str | Path, sniff_bytes: int = 8192) -> bool:
    """Cheap binary sniff: NUL byte or high non-printable ratio in head.

    Used by the walker AFTER mime/ext-based filtering to catch surprises
    (e.g. a .txt that's actually a binary blob).
    """
    try:
        with open(path, "rb") as f:
            buf = f.read(sniff_bytes)
    except OSError:
        return True  # unreadable = treat as binary

    if not buf:
        return False
    if b"\x00" in buf:
        return True

    # Heuristic: > 30% non-printable, non-whitespace bytes → binary.
    text_chars = bytes(range(32, 127)) + b"\n\r\t\b\f"
    nontext = sum(1 for b in buf if b not in text_chars)
    return (nontext / len(buf)) > 0.30
