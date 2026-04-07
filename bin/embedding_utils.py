"""
Shared embedding and vector-math utilities for MCP bridges.

Consolidates duplicated code from memory_bridge.py and debug_agent_bridge.py:
  - Binary packing/unpacking for embedding storage
  - Cosine similarity (numpy-accelerated with pure-Python fallback)
  - Model size parsing for dynamic model selection
  - Change-agent inference from agent_id/model_id hints
"""

import logging
import re
import struct
import unicodedata

logger = logging.getLogger(__name__)

# ── String Sanitization ──────────────────────────────────────────────────────
def sanitize(text: str) -> str:
    """
    Robust UTF-8 sanitization (M12). 
    Removes control characters and normalizes Unicode while preserving 
    emojis, CJK, and other multi-byte characters.
    """
    if not text:
        return ""
    # NFKC normalization handles compatibility characters
    normalized = unicodedata.normalize('NFKC', text)
    # Remove control characters but keep common whitespace and all valid printables
    return "".join(ch for ch in normalized if unicodedata.category(ch)[0] != "C" or ch in "\n\r\t")

# ── numpy (optional) ─────────────────────────────────────────────────────────
try:
    import numpy as _np
    HAS_NUMPY = True
except ImportError:
    _np = None  # type: ignore[assignment]
    HAS_NUMPY = False


# ── Binary packing ───────────────────────────────────────────────────────────
def pack(floats: list[float]) -> bytes:
    """Pack a list of floats into a compact binary blob (4 bytes per float)."""
    return struct.pack(f"{len(floats)}f", *floats)


def unpack(blob: bytes) -> list[float]:
    """Unpack a binary blob back into a list of floats."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


# ── Cosine similarity ────────────────────────────────────────────────────────
def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two vectors. Uses numpy if available."""
    if HAS_NUMPY:
        va = _np.array(a, dtype=_np.float32)
        vb = _np.array(b, dtype=_np.float32)
        denom = float(_np.linalg.norm(va) * _np.linalg.norm(vb))
        return float(_np.dot(va, vb) / denom) if denom > 0 else 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / (mag_a * mag_b) if (mag_a and mag_b) else 0.0


def batch_cosine(query: list[float], matrix: list[list[float]]) -> list[float]:
    """
    Cosine similarity of one query vector against a list of vectors.
    Robustly handles inhomogeneous dimensions by filtering out non-matching vectors.
    """
    if not matrix:
        return []

    q_dim = len(query)
    # Filter out vectors with different dimensions to prevent numpy ValueError
    valid_indices = [i for i, v in enumerate(matrix) if len(v) == q_dim]
    
    if not valid_indices:
        return [0.0] * len(matrix)

    if HAS_NUMPY:
        try:
            q = _np.array(query, dtype=_np.float32)
            # Create a full results array initialized to 0.0
            results = [0.0] * len(matrix)
            
            # Perform batch operation on valid subset
            valid_matrix = [matrix[i] for i in valid_indices]
            m = _np.array(valid_matrix, dtype=_np.float32)
            
            q_norm = _np.linalg.norm(q)
            m_norms = _np.linalg.norm(m, axis=1)
            
            norms = m_norms * q_norm
            # Avoid division by zero
            norms = _np.where(norms == 0, 1e-10, norms)
            
            subset_scores = (m @ q / norms).tolist()
            
            # Map subset scores back to original indices
            for i, score in zip(valid_indices, subset_scores):
                results[i] = score
            return results
        except Exception as exc:
            logger.warning(f"numpy batch_cosine failed: {exc}. Falling back to list comprehension.")

    return [cosine(query, v) if len(v) == q_dim else 0.0 for v in matrix]


# ── Model size parsing ───────────────────────────────────────────────────────
def parse_model_size(model_id: str) -> float:
    """Extract numeric size in billions from a model ID string.

    Examples:
        'llama-70b'     → 70.0
        'qwen2.5-0.5b'  → 0.5
        'nomic-embed-*' → 0.1 (embedding models treated as smallest)
    """
    match = re.search(r'(\d+(?:\.\d+)?)[bB]', model_id.lower())
    if match:
        return float(match.group(1))
    if any(k in model_id.lower() for k in ("embed", "nomic", "jina", "bge", "e5", "gte", "minilm")):
        return 0.1
    return 0.0


def parse_model_size_with_id(model_id: str) -> tuple[float, str]:
    """Like parse_model_size but returns (size, original_id) for sorting."""
    return parse_model_size(model_id), model_id


# ── Change-agent inference ───────────────────────────────────────────────────
VALID_CHANGE_AGENTS = frozenset({
    "claude", "gemini", "aider", "openclaw", "deepseek", "grok",
    "manual", "system", "unknown",
})


def infer_change_agent(agent_id: str = "", model_id: str = "",
                       default: str = "unknown") -> str:
    """Infer the change_agent platform from agent_id and model_id hints."""
    combined = f"{agent_id} {model_id}".lower()
    if any(k in combined for k in ("claude", "sonnet", "opus", "haiku", "anthropic")):
        return "claude"
    if any(k in combined for k in ("gemini", "gemma", "google")):
        return "gemini"
    if "aider" in combined:
        return "aider"
    if any(k in combined for k in ("openclaw", "claw")):
        return "openclaw"
    if any(k in combined for k in ("deepseek", "r1")):
        return "deepseek"
    if any(k in combined for k in ("grok", "xai")):
        return "grok"
    if any(k in combined for k in ("system", "auditor", "cron", "debug_agent")):
        return "system"
    if "manual" in combined:
        return "manual"
    return default
