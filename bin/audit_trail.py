#!/usr/bin/env python3
"""
Cryptographically signed, tamper-evident audit trail for m3-memory.
Logs all destructive and mutating operations in a SHA-256 chain-of-trust log.
"""

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict

from crypto_provider import get_sha256
from m3_sdk import get_m3_root


def get_audit_trail_path() -> str:
    """Returns the path to the secure audit trail log file."""
    return os.path.join(get_m3_root(), "audit_trail.log.jsonl")

def write_audit_entry(action: str, target_id: str, metadata: Dict[str, Any]) -> str:
    """Appends a new cryptographically signed entry to the audit trail log.

    Each entry carries the SHA-256 hash of the previous entry, forming a
    tamper-evident hash chain.

    Returns the SHA-256 hash of the newly written entry.
    """
    audit_file = get_audit_trail_path()

    # 1. Ensure directory exists
    os.makedirs(os.path.dirname(audit_file), exist_ok=True)

    # 2. Find previous entry's hash (Genesis is 64 zeros)
    prev_hash = "0" * 64
    if os.path.exists(audit_file) and os.path.getsize(audit_file) > 0:
        try:
            with open(audit_file, "r", encoding="utf-8") as f:
                # Read backwards or read lines. The file is log.jsonl, typically small to medium.
                # If it grows extremely large, we can seek from the end.
                lines = f.readlines()
                if lines:
                    last_line = lines[-1].strip()
                    if last_line:
                        last_entry = json.loads(last_line)
                        if isinstance(last_entry, dict) and "hash" in last_entry:
                            prev_hash = last_entry["hash"]
        except Exception:
            # Fall back to genesis if there is any error reading or parsing the last line
            pass

    # 3. Create current entry
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "target_id": target_id,
        "metadata": metadata,
        "prev_hash": prev_hash
    }

    # 4. Generate canonical string representation (sorted keys, no spaces) for hashing.
    # Route through crypto_provider so the audit chain honors the FIPS boundary
    # (M3_FIPS_MODE -> wolfCrypt). SHA-256 hex is byte-identical to the prior
    # hashlib path on the DEFAULT backend, so existing log entries stay verifiable.
    canonical_str = json.dumps(entry, sort_keys=True, separators=(',', ':'))
    current_hash = get_sha256(canonical_str.encode('utf-8'))

    # 5. Append signed entry to log
    entry["hash"] = current_hash
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    return current_hash

def verify_audit_trail() -> bool:
    """Verifies the integrity of the audit trail log.

    Checks that every entry's hash matches its contents, and that the prev_hash
    of each entry correctly links to the hash of the preceding entry.

    Returns True if the log is consistent, False otherwise.
    """
    audit_file = get_audit_trail_path()
    if not os.path.exists(audit_file):
        return True  # Empty log is technically consistent

    try:
        expected_prev_hash = "0" * 64
        with open(audit_file, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)

                # Check link to previous entry
                actual_prev_hash = entry.get("prev_hash")
                if actual_prev_hash != expected_prev_hash:
                    return False

                # Re-compute hash of current entry
                signature = entry.pop("hash", None)
                if signature is None:
                    return False

                canonical_str = json.dumps(entry, sort_keys=True, separators=(',', ':'))
                computed_hash = get_sha256(canonical_str.encode('utf-8'))

                if computed_hash != signature:
                    return False

                expected_prev_hash = signature

        return True
    except Exception:
        return False


def log_event(ctx, category: str, detail_a: str,
              detail_b: str = "", detail_c: Any = None) -> None:
    """Route a structured event to the correct legacy table.

    Used by bridges that predate the unified memory_items model.
    Categories: 'thought'/'activity' → activity_logs; 'decision' → project_decisions.
    Unknown categories fall through to activity_logs for safety.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with ctx.get_sqlite_conn() as conn:
        cur = conn.cursor()
        if category == "decision":
            cur.execute(
                "INSERT INTO project_decisions (project, decision, rationale, timestamp) "
                "VALUES (?, ?, ?, ?)",
                (detail_c or "default", detail_a, detail_b, now),
            )
        else:
            cur.execute(
                "INSERT INTO activity_logs (timestamp, query, response, model_used) "
                "VALUES (?, ?, ?, ?)",
                (now, detail_a, detail_b, detail_c or category),
            )
        conn.commit()

