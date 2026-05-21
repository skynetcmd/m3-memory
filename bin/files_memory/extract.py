"""Fact extraction from leaves — phase 2.

Per-leaf LLM call that produces a list of atomic facts with
source_span (character offsets into the leaf) + confidence + candidate
entity names. Designed to be reused by both `inline` and `queue` modes.

Inline mode:  the ingester calls extract_facts_for_leaf() right after
              writing the leaf row.
Queue mode:   the ingester writes the leaf with extraction_status =
              'pending'; a separate drain pass calls extract_for_pending_leaves().

The extractor itself is mode-agnostic — it takes a leaf row, produces
fact records, writes them transactionally. Both modes share the same
prompt + parsing + entity-linking logic.

Public API:
    extract_facts_for_leaf(leaf_row, file_summary=None) -> ExtractionResult
    extract_for_pending_leaves(limit, db_path=None) -> dict (counts)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Optional

from embedding_utils import pack

from . import config
from .db import _db
from .embed import embed_texts

logger = logging.getLogger("files_memory.extract")


# ──────────────────────────────────────────────────────────────────────────────
# Data types
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class ExtractedFact:
    """One fact returned by the LLM. Maps to a row in `facts`."""
    statement: str
    source_span_start: int
    source_span_end: int
    confidence: float
    entities: list[str] = field(default_factory=list)  # candidate canonical names


@dataclass
class ExtractionResult:
    """Outcome of extracting from one leaf."""
    leaf_uuid: str
    status: str           # 'ok' | 'failed' | 'skipped_size' | 'skipped_type'
    fact_count: int = 0
    duration_ms: int = 0
    error: Optional[str] = None
    facts: list[ExtractedFact] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Prompt + LLM client (lifted from summarize.py — same pattern)
# ──────────────────────────────────────────────────────────────────────────────
_EXTRACT_PROMPT = """\
You extract atomic, verifiable facts from a section of a document. Output
strict JSON only, no prose, no markdown fences.

Schema:
{"facts": [
  {
    "statement": "<one self-contained factual claim, present tense, third person>",
    "source": "<the exact phrase from the section that supports this claim>",
    "confidence": <number 0.0 to 1.0; 1.0 = directly stated, 0.5 = inferred>,
    "entities": [<canonical names of entities mentioned, e.g. product names, people, places, technical terms>]
  }
]}

Rules:
- Extract ONLY facts the text directly states or strongly implies.
- Skip rhetorical questions, opinions, examples, and code.
- Each statement must be self-contained — no "this", "that", "the above".
- The "source" must be a verbatim substring of the section (we use it to
  locate the supporting phrase).
- If the section contains no extractable facts, return {"facts": []}.
- Maximum 8 facts per section.
"""


def _llm_endpoint() -> Optional[str]:
    """Read LLM endpoint from env. None = no LLM available."""
    return (
        os.environ.get("M3_FILES_EXTRACT_URL")
        or os.environ.get("M3_FILES_SUMMARY_URL")
        or os.environ.get("M3_LMSTUDIO_URL")
        or None
    )


def _llm_model() -> str:
    return (
        os.environ.get("M3_FILES_EXTRACT_MODEL")
        or os.environ.get("M3_FILES_SUMMARY_MODEL")
        or "qwen3-4b-instruct"
    )


def llm_available() -> bool:
    return bool(_llm_endpoint())


def _llm_call(content: str, max_tokens: int = 1024) -> Optional[str]:
    """Call the LLM endpoint with the extract prompt. Returns raw text or None."""
    endpoint = _llm_endpoint()
    if not endpoint:
        return None
    import httpx

    url = endpoint.rstrip("/") + "/v1/chat/completions"
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                url,
                json={
                    "model": _llm_model(),
                    "messages": [
                        {"role": "system", "content": _EXTRACT_PROMPT},
                        {"role": "user", "content": content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                    # Some endpoints honor a JSON mode hint; harmless if ignored.
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.debug("extract LLM call failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# JSON parsing — robust to common LLM mistakes
# ──────────────────────────────────────────────────────────────────────────────
def _parse_facts_json(raw: str) -> list[ExtractedFact]:
    """Parse the LLM output into ExtractedFact records.

    Robust to:
      - Markdown fences (```json ... ```)
      - Leading/trailing prose despite the prompt
      - Missing fields (filled with defaults)
      - confidence given as a string

    Raises ValueError if the response cannot be parsed at all.
    """
    if not raw:
        return []

    # Strip markdown fences
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n", "", cleaned)
        cleaned = re.sub(r"\n```\s*$", "", cleaned)

    # Find the first '{' and last '}' for safety against prose wrap.
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first < 0 or last <= first:
        raise ValueError(f"no JSON object found in LLM response: {raw[:200]!r}")
    obj_text = cleaned[first:last + 1]

    try:
        obj = json.loads(obj_text)
    except json.JSONDecodeError:
        # One retry: drop trailing commas (common LLM tic).
        obj_text2 = re.sub(r",(\s*[}\]])", r"\1", obj_text)
        obj = json.loads(obj_text2)  # second failure propagates

    raw_facts = obj.get("facts") or []
    if not isinstance(raw_facts, list):
        raise ValueError(f"'facts' field is not a list: {type(raw_facts).__name__}")

    out: list[ExtractedFact] = []
    for fact in raw_facts:
        if not isinstance(fact, dict):
            continue
        statement = (fact.get("statement") or "").strip()
        if not statement:
            continue
        source = (fact.get("source") or "").strip()
        try:
            conf = float(fact.get("confidence", 1.0))
        except (TypeError, ValueError):
            conf = 1.0
        conf = max(0.0, min(1.0, conf))
        entities_raw = fact.get("entities") or []
        if isinstance(entities_raw, list):
            entities = [e.strip() for e in entities_raw if isinstance(e, str) and e.strip()]
        else:
            entities = []
        # source_span filled in by the caller after locating `source`
        # within the leaf text. We hold the source phrase as a placeholder.
        out.append(ExtractedFact(
            statement=statement,
            source_span_start=-1,
            source_span_end=-1,
            confidence=conf,
            entities=entities,
        ))
        # Stash the source phrase in a private attribute the caller reads.
        out[-1]._source_phrase = source  # type: ignore[attr-defined]
    return out


def _resolve_source_span(leaf_text: str, source: str) -> tuple[int, int]:
    """Find the source phrase in leaf_text. Returns (start, end) or (-1, -1).

    Tries exact substring first; falls back to a simple normalized
    search (collapse whitespace) so quoted phrases with line breaks still
    match.
    """
    if not source:
        return (-1, -1)
    # Exact first
    idx = leaf_text.find(source)
    if idx >= 0:
        return (idx, idx + len(source))
    # Normalized: collapse all whitespace runs to single spaces in both.
    norm_re = re.compile(r"\s+")
    norm_source = norm_re.sub(" ", source).strip()
    # Build a mapping from norm-space position back to original offset.
    # Cheaper: just regex-search the original text with whitespace runs
    # treated as flexible.
    pattern = re.escape(norm_source).replace(r"\ ", r"\s+")
    m = re.search(pattern, leaf_text)
    if m:
        return (m.start(), m.end())
    return (-1, -1)


# ──────────────────────────────────────────────────────────────────────────────
# Single-leaf extraction
# ──────────────────────────────────────────────────────────────────────────────
def extract_facts_for_leaf(
    leaf_uuid: str,
    leaf_text: str,
    *,
    leaf_division_type: str = "",
    file_summary: Optional[str] = None,
    max_tokens: int = 1024,
) -> ExtractionResult:
    """Extract facts from one leaf's text. Pure: does NOT write to DB.

    The caller is responsible for transactional writes (see
    write_extraction_result). This split lets us call from inline mode
    (already inside a write txn) and from queue-drain mode (own txn).
    """
    t0 = time.perf_counter()
    if not leaf_text or not leaf_text.strip():
        return ExtractionResult(
            leaf_uuid=leaf_uuid, status="skipped_size",
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )
    if len(leaf_text) < config.EXTRACT_MIN_LEAF_CHARS:
        return ExtractionResult(
            leaf_uuid=leaf_uuid, status="skipped_size",
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    if not llm_available():
        return ExtractionResult(
            leaf_uuid=leaf_uuid, status="failed",
            error="no LLM endpoint configured",
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    prefix = f"Document context: {file_summary}\n\nSection:\n" if file_summary else "Section:\n"
    raw = _llm_call(prefix + leaf_text, max_tokens=max_tokens)
    if raw is None:
        return ExtractionResult(
            leaf_uuid=leaf_uuid, status="failed",
            error="LLM call returned None",
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    try:
        facts = _parse_facts_json(raw)
    except (ValueError, json.JSONDecodeError) as e:
        return ExtractionResult(
            leaf_uuid=leaf_uuid, status="failed",
            error=f"parse failed: {e}",
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Resolve source spans now that we have the leaf text.
    for fact in facts:
        src = getattr(fact, "_source_phrase", "")
        start, end = _resolve_source_span(leaf_text, src)
        fact.source_span_start = start if start >= 0 else 0
        fact.source_span_end = end if end >= 0 else min(len(leaf_text), 200)

    return ExtractionResult(
        leaf_uuid=leaf_uuid, status="ok",
        fact_count=len(facts),
        facts=facts,
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Persistence — writes the extraction result into files.db
# ──────────────────────────────────────────────────────────────────────────────
def write_extraction_result(
    conn: sqlite3.Connection,
    result: ExtractionResult,
    *,
    file_node_uuid: str,
    ingestion_run_uuid: str,
    extractor_version: str,
    model_id: Optional[str],
    embed_facts: bool = True,
    link_entities: bool = True,
) -> None:
    """Persist an ExtractionResult: facts, embeddings, entity refs, attempt log.

    Atomic — caller wraps this in a transaction (or we're inside _db()).
    """
    # Always write an attempt row, success or not.
    attempt_uuid = str(_uuid.uuid4())
    conn.execute(
        "INSERT INTO extraction_attempts("
        "uuid, leaf_uuid, ingestion_run, extractor_version, model_id, "
        "status, fact_count, duration_ms, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_uuid, result.leaf_uuid, ingestion_run_uuid,
            extractor_version, model_id,
            result.status, result.fact_count, result.duration_ms, result.error,
        ),
    )

    # Update leaf extraction_status.
    conn.execute(
        "UPDATE leaves SET extraction_status = ? WHERE uuid = ?",
        (result.status, result.leaf_uuid),
    )

    if result.status != "ok" or not result.facts:
        return

    # Insert facts.
    fact_uuids: list[str] = []
    fact_texts: list[str] = []
    for fact in result.facts:
        fuuid = str(_uuid.uuid4())
        conn.execute(
            "INSERT INTO facts("
            "uuid, leaf, file_node, statement, source_span_start, "
            "source_span_end, confidence, extraction_run) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fuuid, result.leaf_uuid, file_node_uuid,
                fact.statement, fact.source_span_start, fact.source_span_end,
                fact.confidence, ingestion_run_uuid,
            ),
        )
        fact_uuids.append(fuuid)
        fact_texts.append(fact.statement)

    # Embed facts in one batch.
    if embed_facts and fact_texts:
        try:
            vecs = embed_texts(fact_texts)
            for fuuid, (vec, model) in zip(fact_uuids, vecs):
                if vec is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO fact_embeddings"
                        "(fact_uuid, embedding, embed_model, dim) "
                        "VALUES (?, ?, ?, ?)",
                        (fuuid, pack(vec), model, len(vec)),
                    )
        except Exception as e:
            logger.warning("fact embedding batch failed: %s", e)

    # Entity linking — deferred to entities module to avoid circular import
    # of memory.entity from this layer.
    if link_entities:
        from .entities import link_facts_to_entities
        try:
            link_facts_to_entities(conn, fact_uuids, [f.entities for f in result.facts])
        except Exception as e:
            logger.warning("entity linking failed for leaf %s: %s", result.leaf_uuid, e)


# ──────────────────────────────────────────────────────────────────────────────
# Queue-drain entry point
# ──────────────────────────────────────────────────────────────────────────────
def extract_for_pending_leaves(
    limit: int = 100,
    *,
    db_path: Optional[str] = None,
) -> dict:
    """Drain leaves with extraction_status='pending'.

    Single-pass: pulls up to `limit` pending leaves, extracts each in
    sequence (no concurrency here — let inline mode be the parallel
    path; queue drain is cheap, run it repeatedly). Returns counts.

    Suitable for running from cron, a maintenance script, or the
    files_extract_pending MCP tool.
    """
    if not llm_available():
        return {"ok": 0, "failed": 0, "skipped": 0, "error": "no LLM endpoint configured"}

    counts = {"ok": 0, "failed": 0, "skipped": 0}
    with _db(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT l.uuid, l.text, l.division_type, l.file_node, l.ingestion_run, "
            "       fn.file_summary "
            "FROM leaves l "
            "JOIN file_nodes fn ON fn.uuid = l.file_node "
            "WHERE l.extraction_status = 'pending' "
            "  AND fn.superseded_by IS NULL "
            "ORDER BY l.created_at ASC "
            "LIMIT ?",
            (limit,),
        ).fetchall()

        for r in rows:
            result = extract_facts_for_leaf(
                r["uuid"], r["text"],
                leaf_division_type=r["division_type"],
                file_summary=r["file_summary"],
            )
            write_extraction_result(
                conn, result,
                file_node_uuid=r["file_node"],
                ingestion_run_uuid=r["ingestion_run"],
                extractor_version=config.EXTRACTOR_VERSION or "unknown",
                model_id=_llm_model(),
            )
            if result.status == "ok":
                counts["ok"] += 1
            elif result.status == "failed":
                counts["failed"] += 1
            else:
                counts["skipped"] += 1

    counts["model_id"] = _llm_model()
    return counts
