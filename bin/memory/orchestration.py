"""Multi-agent orchestration: agent registry, notifications, and tasks.

Extracted verbatim from `memory_core.py` (formerly lines ~1620-2131). This
group is self-contained: it only needs `_db` / `_record_history` (from
`memory.db`) plus stdlib, and calls within the group (e.g.
`task_assign_impl` -> `notify_impl`). It must NOT import `memory_core` —
`memory_core` imports this module, so that would be a cycle.

Two names referenced here are intentionally NOT imported at module scope:

  - `_refresh_hint` (called by `agent_register_impl` / `agent_offline_impl`)
    is defined in `memory_core.py` itself and stays there. It is imported
    lazily, inside the function bodies that need it, exactly like
    `agent_set_trust_impl` already does for `memory.trust.set_agent_trust`.
    A module-level import would recreate the cycle this module exists to
    avoid; a deferred import resolves fine because by the time these impls
    are actually *called*, `memory_core` has finished importing.
  - `logger` is recreated locally via `logging.getLogger` (same underlying
    logger name is not required — these are fire-and-forget warnings).

See `docs/MEMORY_CORE_MODULARIZATION.md` for the migration plan.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from . import records
from .backends import dialect
from .db import _db, _record_history

logger = logging.getLogger("memory_core")

# ── Task Orchestration: State Machine + Helper Functions ─────────────────────────

TASK_STATE_TRANSITIONS = {
    "pending":     {"in_progress", "blocked", "cancelled"},
    "in_progress": {"blocked", "completed", "failed", "cancelled"},
    "blocked":     {"in_progress", "cancelled"},
    "completed":   set(),
    "failed":      set(),
    "cancelled":   set(),
}
VALID_TASK_STATES = frozenset(TASK_STATE_TRANSITIONS.keys())
TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
VALID_AGENT_STATUSES = frozenset({"active", "idle", "offline"})

def _validate_task_transition(prev: str, new: str):
    """Validates task state transitions. Returns None if valid, error string if invalid."""
    if new not in VALID_TASK_STATES:
        return f"Error: invalid task state '{new}'. Valid: {', '.join(sorted(VALID_TASK_STATES))}"
    if prev == new:
        return None
    allowed = TASK_STATE_TRANSITIONS.get(prev, set())
    if new not in allowed:
        return (f"Error: cannot transition task from '{prev}' to '{new}'. "
                f"Allowed from '{prev}': {sorted(allowed) or '(terminal)'}")
    return None

def _agent_exists(agent_id: str) -> bool:
    """Checks if an agent is registered in the agents table."""
    with _db() as db:
        p = dialect().param()
        row = db.execute(f"SELECT 1 FROM agents WHERE agent_id = {p}", (agent_id,)).fetchone()
        return row is not None

# ── Agent Registry (5 functions) ──────────────────────────────────────────────────

def agent_register_impl(agent_id: str, role: str = "", capabilities: list | None = None,
                        metadata: dict | None = None) -> str:
    """Registers or updates an agent in the registry.

    Every parameter but `agent_id` defaults, because the ToolSpec has always
    marked them optional: `required` lists only `agent_id`. An agent that read
    the contract and called with just an id got a TypeError from a tool that
    said the call was valid. The ToolSpec is the agent's contract, so the impl
    moves to meet it (section 12a) -- widening a signature breaks no existing
    caller, while narrowing the spec would break every agent already relying on
    the documented shape.
    """
    if not agent_id:
        return "Error: agent_id cannot be empty"

    now = datetime.now(timezone.utc).isoformat()
    caps_json = json.dumps(capabilities or [])
    meta_json = json.dumps(metadata or {})

    with _db() as db:
        p = dialect().param()
        db.execute(
            f"""INSERT INTO agents (agent_id, role, capabilities, metadata_json, status, last_seen, created_at)
               VALUES ({p}, {p}, {p}, {p}, 'active', {p}, {p})
               ON CONFLICT(agent_id) DO UPDATE SET
                 role=excluded.role,
                 capabilities=excluded.capabilities,
                 metadata_json=excluded.metadata_json,
                 status='active',
                 last_seen=excluded.last_seen""",
            (agent_id, role, caps_json, meta_json, now, now)
        )

    from memory_core import _refresh_hint
    return f"Registered: {agent_id} (role={role}, status=active)" + _refresh_hint(agent_id)

def agent_heartbeat_impl(agent_id: str) -> str:
    """Updates agent's last_seen timestamp and status to active.
    
    Note: calling this explicitly is now optional. `notifications_poll` performs 
    an implicit heartbeat automatically when an agent checks its mail.
    """
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        cur = db.execute(
            f"UPDATE agents SET last_seen = {p}, status = 'active' WHERE agent_id = {p}",
            (now, agent_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not registered"

    return f"Heartbeat: {agent_id} (last_seen={now})"

def agent_list_impl(status: str = "", role: str = "", as_records: bool = False) -> str:
    """Lists agents, optionally filtered by status and/or role."""
    where_clauses = []
    params = []
    p = dialect().param()

    if status:
        where_clauses.append(f"status = {p}")
        params.append(status)
    if role:
        where_clauses.append(f"role = {p}")
        params.append(role)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT agent_id, role, status, last_seen FROM agents {where} ORDER BY last_seen DESC",
            params
        ).fetchall()

    if not rows:
        # Note the empty case still honours as_records: a caller that opted into
        # structured output must not get a display string back just because the
        # result set was empty -- that is the branch a parser forgets to handle.
        return records.emit("(no agents)", records.as_records_payload(()), as_records)

    lines = [f"Agents ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['agent_id']}] role={row['role']} status={row['status']} last_seen={row['last_seen']}")

    return records.emit("\n".join(lines),
                        records.as_records_payload(rows), as_records)

def agent_get_impl(agent_id: str) -> str:
    """Retrieves detailed information about a single agent."""
    with _db() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT * FROM agents WHERE agent_id = {p}",
            (agent_id,)
        ).fetchone()

    if not row:
        return f"Error: agent '{agent_id}' not found"

    caps = json.loads(row["capabilities"] or "[]")
    meta = json.loads(row["metadata_json"] or "{}")

    _keys = row.keys()
    lines = [
        f"Agent: {row['agent_id']}",
        f"  Role: {row['role']}",
        f"  Status: {row['status']}",
        f"  Capabilities: {caps}",
        f"  Metadata: {meta}",
        f"  Trust: {row['trust_score'] if 'trust_score' in _keys else 'N/A'}",
        f"  Last Seen: {row['last_seen']}",
        f"  Created At: {row['created_at'] if 'created_at' in _keys else 'N/A'}",
    ]

    return "\n".join(lines)


def agent_set_trust_impl(agent_id: str, trust_score: float) -> str:
    """Explicitly set an agent's trust_score (clamped to [0.5, 1.0]).

    Trust weights an agent's assertions in confidence aggregation
    (knowledge-maintenance Phase 2). 1.0 = neutral. Upserts the agent row if it
    doesn't exist yet. Requires migration 036 (agents.trust_score).
    """
    from memory.trust import set_agent_trust
    if not agent_id:
        return "Error: agent_id is required"
    try:
        with _db() as db:
            value = set_agent_trust(db, agent_id, trust_score)
    except Exception as e:  # noqa: BLE001
        if "no column named trust_score" in str(e).lower() or "no such column" in str(e).lower():
            return "Error: trust_score unavailable — run migration 036 (trust_and_corroboration)"
        raise
    return f"Set trust for '{agent_id}' to {value:.2f}"


def agent_offline_impl(agent_id: str) -> str:
    """Marks an agent as offline."""
    with _db() as db:
        p = dialect().param()
        cur = db.execute(
            f"UPDATE agents SET status = 'offline' WHERE agent_id = {p}",
            (agent_id,)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: agent '{agent_id}' not found"

    from memory_core import _refresh_hint
    return f"Agent {agent_id} marked offline" + _refresh_hint(agent_id)

# ── Notifications (4 functions) ───────────────────────────────────────────────────

# ── Agent addressing: "type" or "type@instance" ───────────────────────────────
#
# `agent_id` was one flat identity with ONE ROW PER AGENT TYPE, so N running
# instances of the same agent shared one inbox. Measured 2026-09-12 with two
# sessions polling one identity:
#
#     session 1 sees: [856, 857]
#     session 2 sees: [856, 857]     <- same items, duplicate work
#     session 1 acks -> session 2 now sees: []   <- work silently vanished
#
# The second failure is the serious one: session 2 had already READ those items
# and they disappeared mid-flight, with no error and no trace.
#
# The scheme is `agent_type@instance_id`. "@" was chosen by elimination, not
# taste (see #170):
#
#   "-"  DISQUALIFIED: already inside 14 of 26 live ids, so `claude-code`
#        would parse as type "claude", instance "code".
#   ":"  DISQUALIFIED: on NTFS a ":" in a filename creates an ALTERNATE DATA
#        STREAM rather than the file. m3 names spill files and logs after
#        agents, so an id containing ":" writes data that is simply invisible.
#        Verified through the Win32 API.
#   "@"  CHOSEN: absent from every live id, already legal in m3's
#        collection-path charset, not a shell metacharacter, and `user@host`
#        makes the compound reading obvious.
#
# BACK-COMPAT IS THE POINT. `split_agent_id("claude-code")` returns
# ("claude-code", None) -- a bare type is unchanged, so every existing caller
# keeps working and instance addressing is purely additive. No migration, no
# schema change, no flag day.

_AGENT_SEP = "@"


def split_agent_id(agent_id: str) -> "tuple[str, str | None]":
    """('claude-code@abc', ) -> ('claude-code', 'abc'); ('claude-code',) -> (…, None).

    maxsplit=1 so an instance id may itself contain "@" without the type
    absorbing it.
    """
    if not agent_id or _AGENT_SEP not in agent_id:
        return agent_id, None
    head, _, tail = agent_id.partition(_AGENT_SEP)
    return head, (tail or None)


def agent_type_of(agent_id: str) -> str:
    """The type half. `agent_type_of("claude-code@abc") == "claude-code"`."""
    return split_agent_id(agent_id)[0]


def require_agent_id(agent_id: str, tool: str) -> str:
    """Refuse an empty agent_id instead of querying nobody's inbox.

    An empty id is not "no mail" -- it is "identity refused". It reaches an impl
    when the anti-spoofing guard in `catalog.dispatch` blanks an LLM-supplied
    id: an LLM-facing caller (`m3_call`) may not address an arbitrary agent
    unless the entry point opts in via `allow_caller_agent_id` (#144).

    Rendering those two as the same empty result is a §3 false negative on the
    one check an agent uses to decide whether it has work. Measured 2026-09-12:
    `m3_call notifications_poll agent_id=claude-code` returned "(empty)" while
    the direct impl returned 10 notifications, one an unread handoff.

    ONE owner rather than the same `if not agent_id` at five call sites -- a
    copied predicate is the defect independent of correctness (§10a), and the
    five would drift the moment one gained a nuance.
    """
    if not agent_id:
        raise ValueError(
            f"{tool} requires an agent_id. It was empty, which means the "
            f"caller's identity was refused by the anti-spoofing guard -- NOT "
            f"that the inbox is empty. An LLM-facing caller (m3_call) cannot "
            f"address an arbitrary agent; use the CLI (`m3 admin {tool} "
            f"--agent-id <id>`) or an entry point that sets "
            f"allow_caller_agent_id."
        )
    return agent_id


def _addressing_predicate(agent_id: str, param: str) -> "tuple[str, tuple]":
    """SQL fragment + params selecting the rows an inbox read should see.

    Two different questions, and conflating them is what made the original
    behaviour the worst of both:

      * a bare TYPE ("claude-code") is a FAN-OUT read -- it matches the type's
        own inbox AND every instance of it, so a broadcast reaches all sessions
        and a legacy caller keeps seeing everything it used to.
      * a QUALIFIED id ("claude-code@abc") is a DIRECT read -- exactly that
        instance, so one session's work cannot be claimed or acked by another.

    The prefix match goes through the dialect seam (``literal_prefix_match``),
    not a hand-built LIKE: the operator and the value rewrite are
    backend-specific, and an agent id containing "%" or "_" must stay literal
    rather than becoming a wildcard that matches other agents' inboxes.
    §10a -- call sites express intent, the dialect renders SQL.
    """
    _t, instance = split_agent_id(agent_id)
    if instance is not None:
        return f"agent_id = {param}", (agent_id,)
    frag, bound = dialect().literal_prefix_match(
        "agent_id", param, agent_id + _AGENT_SEP
    )
    return f"(agent_id = {param} OR {frag})", (agent_id, bound)

# ── Message state: ONE owner for the predicate ───────────────────────────────
# The four states are DERIVED, never stored. A `status` column would give "is
# this pending" two sources of truth that nothing forces to agree, and six live
# call sites already filter on `read_at IS NULL` -- one of them the waiter that
# delivers agent mail. A stored status would shadow those predicates rather than
# replace them, and the first write path that sets one without the other breaks
# delivery silently. §10a: a copied predicate is the defect independent of
# correctness, and the copies drift. P0 of this same effort was exactly that
# failure -- memory_inbox kept a private copy of the addressing rule and silently
# missed the #170 fix.
#
# Order matters. A row can satisfy several raw conditions at once (a completed
# row still carries its claimed_by), so these are evaluated most-terminal-first
# and the FIRST match wins.
MESSAGE_STATES = ("FAILED", "COMPLETED", "CLAIMED", "PENDING")


def message_state_sql(state: str) -> str:
    """SQL predicate selecting rows in ``state``. The single owner.

    Call sites express intent (`message_state_sql("PENDING")`); this renders the
    condition. Backend-neutral -- it is pure IS NULL / IS NOT NULL, no dialect
    function -- so it needs no Dialect method, but it lives here so that adding a
    state or changing one is one edit rather than a search for hand-written
    filters.
    """
    preds = {
        "FAILED": "failed_at IS NOT NULL",
        "COMPLETED": "failed_at IS NULL AND read_at IS NOT NULL",
        "CLAIMED": ("failed_at IS NULL AND read_at IS NULL "
                    "AND claimed_by IS NOT NULL"),
        "PENDING": ("failed_at IS NULL AND read_at IS NULL "
                    "AND claimed_by IS NULL"),
    }
    try:
        return preds[state]
    except KeyError:
        raise ValueError(
            f"unknown message state {state!r}; expected one of {MESSAGE_STATES}"
        ) from None


def message_state_of(row) -> str:
    """The state of a fetched row, by the SAME rules as :func:`message_state_sql`.

    Deliberately mirrors the SQL rather than re-deciding: if these two ever
    disagree, a row is filtered as one state and displayed as another. The test
    suite asserts they agree for every column combination.
    """
    def _set(name):
        try:
            return row[name] is not None
        except (KeyError, IndexError, TypeError):
            return False

    if _set("failed_at"):
        return "FAILED"
    if _set("read_at"):
        return "COMPLETED"
    if _set("claimed_by"):
        return "CLAIMED"
    return "PENDING"


def notify_impl(agent_id: str, kind: str, payload: dict = None,
                from_agent: str = "", from_session: str = "") -> str:
    """Sends a notification to an agent.

    ``from_agent`` / ``from_session`` are the RETURN ADDRESS. They are stamped
    into the payload as ``_from`` so the recipient can reply to the specific
    sender rather than guessing.

    Why a session matters and an agent name is not enough: ``agent_id`` is the
    PRIMARY KEY of the agents table, so N concurrent sessions of the SAME agent
    type collapse into ONE row. Measured 2026-09-13 on a live box: 5 live
    ``mcp.*`` bridges registered, but ``agent_list`` showed a single
    ``claude-code`` entry whose ``last_seen`` was just whoever wrote most
    recently. Addressing ``claude-code`` reaches whichever sister polls first;
    the others never see it. Cross-TYPE targeting (agy vs claude-code vs
    gemini-cli) was never ambiguous — sisters are.

    Both fields are optional and free-form: identity here is SELF-ASSERTED by
    design (see bin/mcp_proxy.py's module docstring). ``_from`` is a
    disambiguator so a reply can be routed, NOT a credential, and nothing
    downstream should treat it as proof of origin.
    """
    now = datetime.now(timezone.utc).isoformat()
    if from_agent or from_session:
        _addr = {k: v for k, v in (("agent", from_agent),
                                   ("session", from_session)) if v}
        if isinstance(payload, dict):
            # Copy, never mutate: stamping into the caller's own dict would leak
            # `_from` back into their object. Namespaced so it cannot collide
            # with their keys.
            payload = {**payload, "_from": _addr}
        elif payload is None:
            payload = {"_from": _addr}
        else:
            # Callers legitimately pass a bare string/list as the payload
            # (bin/files_memory/watch.py, and the addressing tests pass "for s1").
            # Wrapping preserves the original verbatim under `value` rather than
            # discarding it or crashing on dict(); found by the existing suite,
            # which is exactly what it is for.
            payload = {"value": payload, "_from": _addr}
    payload_json = json.dumps(payload if payload is not None else {})

    with _db() as db:
        _d = dialect()
        ph = _d.placeholder(4)
        # Backend-neutral generated-id read: RETURNING id on backends that support
        # it (PG/MariaDB), cur.lastrowid on SQLite — via the dialect, not a name check.
        cur = db.execute(
            f"INSERT INTO notifications (agent_id, kind, payload_json, created_at) "
            f"VALUES ({ph}){_d.returning_id_clause()}",
            (agent_id, kind, payload_json, now)
        )
        new_id = _d.last_insert_id(cur)

    return f"Notified {agent_id}: {kind} (id={new_id})"


def notifications_unread_ids_impl(agent_id: str) -> list[int]:
    """
    Returns a list of unread notification IDs for the given agent.
    Returns structured data rather than prose, strictly conforming to DESIGN_PHILOSOPHIES.md (3).
    """
    require_agent_id(agent_id, "notifications_unread_ids")
    p = dialect().param()
    pred, params = _addressing_predicate(agent_id, p)
    with _db() as db:
        rows = db.execute(
            f"SELECT id FROM notifications WHERE {pred} "
            f"AND read_at IS NULL ORDER BY id ASC",
            params,
        ).fetchall()
    return [row[0] for row in rows]

def _decode_payload_json(rows) -> list[dict]:
    """Rows with `payload_json` re-parsed into a real nested object.

    The column stores JSON as TEXT. Emitting it verbatim inside a records
    envelope would hand the caller a JSON string INSIDE JSON, forcing a second
    json.loads on one field -- the same double-encode that makes memory_get
    awkward to consume. A records caller asked for structure; give them
    structure. Undecodable text is left as-is rather than dropped, so a
    malformed row stays visible instead of silently vanishing.
    """
    out = []
    for rec in records.to_records(rows):
        raw = rec.get("payload_json")
        if isinstance(raw, str) and raw:
            try:
                decoded = json.loads(raw)
                # DOUBLE-ENCODED ROWS ARE REAL, NOT HYPOTHETICAL. Measured
                # 2026-09-14: every notification in the live store decodes to a
                # STRING on the first loads and to the dict on the second --
                # some writers pass an already-serialized payload to a column
                # that serializes again. Unwrapping here fixes the READ side for
                # rows that already exist; it cannot be fixed at the writer
                # alone, because the bad rows are durable. Bounded to one extra
                # unwrap so a genuine string payload (or a pathological chain)
                # cannot loop.
                if isinstance(decoded, str):
                    try:
                        decoded = json.loads(decoded)
                    except (ValueError, TypeError):
                        pass  # a real string payload, not a double-encode
                rec["payload"] = decoded
                rec.pop("payload_json", None)
            except (ValueError, TypeError):
                pass  # keep payload_json verbatim; a bad row must stay visible
        out.append(rec)
    return out


def notifications_poll_impl(agent_id: str, unread_only: bool = True, limit: int = 20,
                            as_records: bool = False) -> str:
    """Retrieves notifications for an agent.

    An empty `agent_id` is REFUSED, not queried. It reaches here when the
    anti-spoofing guard in `catalog.dispatch` blanks an LLM-supplied id (an
    LLM-facing caller such as `m3_call` may not poll an arbitrary inbox unless
    the entry point opts in via `allow_caller_agent_id`) -- so it means
    "identity refused", never "this agent has no mail".

    Reporting that as `Notifications for : (empty)` was a §3 false negative on
    the single check an agent uses to decide whether it has work. Measured
    2026-09-12: `m3_call notifications_poll agent_id=claude-code` returned
    "(empty)" while the direct impl returned 10 notifications, one of them an
    unread handoff. An agent that trusts the empty answer silently drops work
    that was addressed to it.

    The two conditions are not distinguishable downstream, so they must not
    share a rendering.
    """
    require_agent_id(agent_id, "notifications_poll")
    p = dialect().param()
    _pred, _addr = _addressing_predicate(agent_id, p)
    where_clause = f"WHERE {_pred}"
    params = list(_addr)

    if unread_only:
        where_clause += " AND read_at IS NULL"

    now_iso = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        # Implicit heartbeat: polling proves the agent is alive. We do this silently
        # so unregistered agents can still poll without erroring.
        db.execute(
            f"UPDATE agents SET last_seen = {p}, status = 'active' WHERE agent_id = {p}",
            (now_iso, agent_id)
        )

        rows = db.execute(
            f"SELECT id, kind, payload_json, created_at, read_at, received_at "
            f"FROM notifications {where_clause} ORDER BY created_at DESC LIMIT {p}",
            params + [limit]
        ).fetchall()

    if not rows:
        return records.emit(f"Notifications for {agent_id}: (empty)",
                            records.as_records_payload((), agent_id=agent_id,
                                                       unread_only=unread_only),
                            as_records)

    read_type = "unread" if unread_only else "total"
    lines = [f"Notifications for {agent_id} ({len(rows)} {read_type}):"]
    for row in rows:
        # received_at is shown only when SET. It was write-only until now --
        # written by notifications_mark_received, read by nothing -- so the <30s
        # receipt SLA it exists to satisfy could not actually be measured, and a
        # message that had been delivered but not acted on was indistinguishable
        # from one that never arrived. Omitted when NULL rather than rendered as
        # "received=None": NULL means "we do not know when this was received",
        # which is a different statement from a measurement (§12c), and padding
        # every line of the common case with a null would bury the signal.
        _recv = f" received={row['received_at']}" if row["received_at"] else ""
        lines.append(f"  [{row['id']}] kind={row['kind']} payload={row['payload_json']} created={row['created_at']}{_recv}")

    return records.emit("\n".join(lines),
                        records.as_records_payload(
                            _decode_payload_json(rows), agent_id=agent_id,
                            unread_only=unread_only),
                        as_records)

def notifications_mark_received_impl(agent_id: str) -> str:
    """Stamps transport receipt on an agent's undelivered notifications.

    This is what a transport waiter calls to satisfy the "acknowledge receipt
    within 30 seconds" SLA. It is deliberately NOT an ack: it writes
    ``received_at`` and never touches ``read_at``, so the unread flag -- the only
    record that the work is still pending -- survives. ``--unread_only`` keeps
    filtering on ``read_at`` and behaves identically before and after this call.

    Acking on detection was the tempting shortcut and it is wrong: it marks a
    message read that no agent has read. The usual counter-argument, that the
    task state machine covers the gap, was measured rather than assumed and is
    false for real traffic -- 29 of 30 recent notifications carried no task_id,
    so nothing else recorded the message as outstanding.

    Only rows that have not already been stamped are touched, so a waiter that
    fires repeatedly on the same WAL change records the FIRST receipt rather than
    the most recent poll -- which is the number a receipt SLA is measured
    against.
    """
    require_agent_id(agent_id, "notifications_mark_received")
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        pred, addr = _addressing_predicate(agent_id, p)
        cur = db.execute(
            f"UPDATE notifications SET received_at = {p} "
            f"WHERE {pred} AND received_at IS NULL",
            (now, *addr)
        )
        rowcount = cur.rowcount

    return f"Marked {rowcount} notifications received for {agent_id}"

def notifications_ack_impl(notification_id: int) -> str:
    """Marks a notification as read."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        cur = db.execute(
            f"UPDATE notifications SET read_at = {p} WHERE id = {p} AND read_at IS NULL",
            (now, notification_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: notification {notification_id} not found or already acked"

    return f"Acked notification {notification_id}"

def notifications_ack_all_impl(agent_id: str) -> str:
    """Marks all unread notifications for an agent as read."""
    require_agent_id(agent_id, "notifications_ack_all")
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        # Ack follows the SAME addressing rule as the read that produced the
        # list: a qualified id acks only that instance's rows, a bare type acks
        # the type and its instances. Any other pairing is how one session's
        # ack empties another's inbox -- the silent-theft half of #170.
        pred, addr = _addressing_predicate(agent_id, p)
        cur = db.execute(
            f"UPDATE notifications SET read_at = {p} WHERE {pred} AND read_at IS NULL",
            (now, *addr)
        )
        rowcount = cur.rowcount

    return f"Acked {rowcount} notifications for {agent_id}"

# ── Tasks (7 functions) ───────────────────────────────────────────────────────────

def task_create_impl(title: str, created_by: str, description: str = "", owner_agent: str = "", parent_task_id: str = "", metadata: dict = None) -> str:
    """Creates a new task."""
    if not title:
        return "Error: title cannot be empty"
    if not created_by:
        return "Error: created_by cannot be empty"

    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        db.execute(
            f"""INSERT INTO tasks (id, title, description, state, created_by, owner_agent, parent_task_id, metadata_json, created_at, updated_at)
               VALUES ({p}, {p}, {p}, 'pending', {p}, {p}, {p}, {p}, {p}, {p})""",
            (task_id, title, description, created_by, owner_agent or None, parent_task_id or None, json.dumps(metadata or {}), now, now)
        )

    return f"Task created: {task_id}"

def task_assign_impl(task_id: str, owner_agent: str) -> str:
    """Assigns a task to an agent and transitions state to in_progress."""
    with _db() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT state, created_by FROM tasks WHERE id = {p} AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    err = _validate_task_transition(prev_state, "in_progress")
    if err:
        return err

    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        db.execute(
            f"UPDATE tasks SET owner_agent = {p}, state = 'in_progress', updated_at = {p} WHERE id = {p}",
            (owner_agent, now, task_id)
        )

    _record_history(task_id, "task_state", prev_state, "in_progress", "state", owner_agent)

    # Fire-and-forget notification
    try:
        notify_impl(owner_agent, "task_assigned", {"task_id": task_id})
    except Exception as e:
        logger.warning(f"task_assigned notify failed for {owner_agent}: {e}")

    return f"Task {task_id} assigned to {owner_agent} (state=in_progress)"

def task_update_impl(task_id: str, state: str = "", description: str = "", metadata: dict = None, actor: str = "") -> str:
    """Updates a task's state, description, and/or metadata."""
    p = dialect().param()
    with _db() as db:
        row = db.execute(
            f"SELECT state, description, metadata_json, created_by, result_memory_id "
            f"FROM tasks WHERE id = {p} AND deleted_at IS NULL",
            (task_id,)
        ).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    prev_state = row["state"]
    new_state = state if state else prev_state

    if state:
        err = _validate_task_transition(prev_state, new_state)
        if err:
            return err

    now = datetime.now(timezone.utc).isoformat()
    updates = [f"updated_at = {p}"]
    params = [now]

    if state:
        updates.append(f"state = {p}")
        params.append(new_state)

    if description:
        updates.append(f"description = {p}")
        params.append(description)

    if metadata is not None:
        updates.append(f"metadata_json = {p}")
        params.append(json.dumps(metadata))

    if new_state in TERMINAL_TASK_STATES:
        updates.append(f"completed_at = {p}")
        params.append(now)

    params.append(task_id)

    with _db() as db:
        db.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE id = {p}",
            params
        )

    if state and prev_state != new_state:
        _record_history(task_id, "task_state", prev_state, new_state, "state", actor or "system")

        # Fire-and-forget notification if completed
        if new_state == "completed":
            try:
                notify_impl(row["created_by"], "task_completed", {"task_id": task_id})
            except Exception as e:
                logger.warning(f"task_completed notify failed for {row['created_by']}: {e}")

        # A task can complete carrying nothing to show for it. That is the same
        # silent-success shape as a call that reports ok while dropping your
        # input: the handoff LOOKS finished and the findings are nowhere. Seen
        # for real -- an agent-to-agent code review reached `completed` with
        # result_memory_id empty, so there was no link from the task to the
        # review and the next reader had to be told the id out of band.
        #
        # NOT an error: `failed` and `cancelled` legitimately have no result,
        # and 19 of 21 existing completed tasks predate this convention, so
        # blocking would break callers and reject honest "nothing was needed"
        # completions. Say it out loud instead, in the string the caller reads.
        if new_state == "completed" and not (row["result_memory_id"] or ""):
            return (f"Task {task_id} updated: state=completed "
                    f"(WARNING: no result_memory_id — nothing links this task to "
                    f"its findings. Call task_set_result to attach one.)")
        return f"Task {task_id} updated: state={new_state}"
    else:
        return f"Task {task_id} updated"

def task_set_result_impl(task_id: str, result_memory_id: str) -> str:
    """Sets the result memory for a task (without changing state)."""
    now = datetime.now(timezone.utc).isoformat()

    with _db() as db:
        p = dialect().param()
        cur = db.execute(
            f"UPDATE tasks SET result_memory_id = {p}, updated_at = {p} WHERE id = {p} AND deleted_at IS NULL",
            (result_memory_id, now, task_id)
        )
        rowcount = cur.rowcount

    if rowcount == 0:
        return f"Error: task '{task_id}' not found"

    return f"Task {task_id} result={result_memory_id}"

def task_get_impl(task_id: str, include_deleted: bool = False) -> str:
    """Retrieves detailed information about a task."""
    p = dialect().param()
    sql = f"SELECT * FROM tasks WHERE id = {p}"
    if not include_deleted:
        sql += " AND deleted_at IS NULL"
    with _db() as db:
        row = db.execute(sql, (task_id,)).fetchone()

    if not row:
        return f"Error: task '{task_id}' not found"

    lines = [
        f"Task: {row['id']}",
        f"  Title: {row['title']}",
        f"  Description: {row['description']}",
        f"  State: {row['state']}",
        f"  Created By: {row['created_by']}",
        f"  Owner: {row['owner_agent'] or '(unassigned)'}",
        f"  Parent Task: {row['parent_task_id'] or '(none)'}",
        # A completed task with no result is worth flagging where a reader
        # actually looks: "(none)" alone reads as a blank field, not as
        # "this task claims to be done and points at nothing".
        f"  Result Memory: {row['result_memory_id'] or ('(none) ⚠ completed with no findings attached' if row['state'] == 'completed' else '(none)')}",
        f"  Created At: {row['created_at']}",
        f"  Updated At: {row['updated_at']}",
        f"  Completed At: {row['completed_at'] or '(not completed)'}",
        f"  Deleted At: {row['deleted_at'] or '(not deleted)'}",
    ]

    return "\n".join(lines)

def task_delete_impl(task_id: str, hard: bool = False, actor: str = "") -> str:
    """Delete a task.

    Soft-delete (default): sets `deleted_at` so pg_sync propagates the
    tombstone to the warehouse and peers on the next run. The row stays
    in local SQLite and is filtered out of reads.

    Hard-delete: only allowed once the row is already tombstoned. Removes
    the row from local SQLite. Note that sync is UPSERT-only, so a hard
    delete on one peer does NOT remove the row on other peers — they
    converge via the soft-delete tombstone.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _db() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT state, deleted_at FROM tasks WHERE id = {p}",
            (task_id,)
        ).fetchone()

        if not row:
            return f"Error: task '{task_id}' not found"

        if hard:
            if row["deleted_at"] is None:
                return (
                    f"Error: task '{task_id}' must be soft-deleted before hard-delete. "
                    "Call task_delete with hard=False first."
                )
            db.execute(f"DELETE FROM tasks WHERE id = {p}", (task_id,))
            _record_history(task_id, "task_deleted", row["state"], "hard_deleted", "deleted_at", actor or "system")
            return f"Task {task_id} hard-deleted"

        if row["deleted_at"] is not None:
            return f"Task {task_id} already soft-deleted at {row['deleted_at']}"

        db.execute(
            f"UPDATE tasks SET deleted_at = {p}, updated_at = {p} WHERE id = {p}",
            (now, now, task_id)
        )

    _record_history(task_id, "task_deleted", row["state"], "soft_deleted", "deleted_at", actor or "system")
    return f"Task {task_id} soft-deleted (tombstone will sync on next pg_sync run)"

def task_list_impl(owner_agent: str = "", state: str = "", parent_task_id: str = "", limit: int = 20, include_deleted: bool = False, as_records: bool = False) -> str:
    """Lists tasks, optionally filtered by owner, state, and/or parent."""
    where_clauses = []
    params = []
    p = dialect().param()

    if not include_deleted:
        where_clauses.append("deleted_at IS NULL")
    if owner_agent:
        where_clauses.append(f"owner_agent = {p}")
        params.append(owner_agent)
    if state:
        where_clauses.append(f"state = {p}")
        params.append(state)
    if parent_task_id:
        where_clauses.append(f"parent_task_id = {p}")
        params.append(parent_task_id)

    where = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    with _db() as db:
        rows = db.execute(
            f"SELECT id, title, state, owner_agent FROM tasks {where} ORDER BY updated_at DESC LIMIT {p}",
            params + [limit]
        ).fetchall()

    if not rows:
        return records.emit("Tasks: (empty)", records.as_records_payload(()), as_records)

    lines = [f"Tasks ({len(rows)}):"]
    for row in rows:
        lines.append(f"  [{row['id'][:8]}] {row['title']} state={row['state']} owner={row['owner_agent']}")

    # Records carry the FULL id; the display line truncates to 8 chars for
    # width. A caller that parsed the display string got a prefix it then had
    # to disambiguate -- the fallback this param exists to remove.
    return records.emit("\n".join(lines), records.as_records_payload(rows), as_records)

def task_tree_impl(root_task_id: str, max_depth: int = 10, as_records: bool = False) -> str:
    """Displays a task and its subtasks in a tree structure. Tombstoned tasks are hidden."""
    max_depth = max(1, min(max_depth, 20))

    with _db() as db:
        p = dialect().param()
        row = db.execute(
            f"SELECT id, title, state, owner_agent FROM tasks WHERE id = {p} AND deleted_at IS NULL",
            (root_task_id,)
        ).fetchone()

        if not row:
            return records.emit(
                f"Error: task '{root_task_id}' not found",
                records.error_payload("not_found", task_id=root_task_id),
                as_records)

        rows = db.execute(
            f"""WITH RECURSIVE subtree(id, title, state, owner_agent, parent_task_id, depth) AS (
                SELECT id, title, state, owner_agent, parent_task_id, 0
                  FROM tasks WHERE id = {p} AND deleted_at IS NULL
                UNION ALL
                SELECT t.id, t.title, t.state, t.owner_agent, t.parent_task_id, s.depth + 1
                  FROM tasks t JOIN subtree s ON t.parent_task_id = s.id
                 WHERE s.depth + 1 <= {p} AND t.deleted_at IS NULL
            )
            SELECT * FROM subtree ORDER BY depth, id""",
            (root_task_id, max_depth)
        ).fetchall()

    if not rows:
        return records.emit(
            f"Error: task '{root_task_id}' not found",
            records.error_payload("not_found", task_id=root_task_id),
            as_records)

    lines = [f"Task tree from {root_task_id[:8]} (max_depth={max_depth}):"]
    for row in rows:
        indent = "  " * row["depth"]
        owner_str = row["owner_agent"] or "-"
        lines.append(f"{indent}[{row['id'][:8]}] {row['title']} ({row['state']}, owner={owner_str})")

    return "\n".join(lines)
