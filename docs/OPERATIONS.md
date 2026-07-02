# M3 Memory: Operations Playbook

Operator-focused workflows for running the memory brain day to day — not
installing it. Every command below maps to a real tool (MCP tool name or CLI
entrypoint); see [CLI_REFERENCE.md](CLI_REFERENCE.md) and
[API_REFERENCE.md](API_REFERENCE.md) for full argument surfaces.

> **Audience:** operators and power users who already have m3 installed and a
> chat-log hook wired. If you're setting up for the first time, start with
> [GETTING_STARTED.md](GETTING_STARTED.md).

---

## 1. Onboard an agent and verify chat-log capture

The single most important post-install check: **confirm turns are actually being
written.** A silently failing hook looks identical to a working one until you go
looking for a memory that was never saved.

1. **Check the subsystem is live.** Call the `chatlog_status` tool (or run the
   doctor). It reports mode, DB paths, row counts, queue depth, spill files,
   embed backlog, last-capture timestamp, and hook health:

   ```
   python bin/memory_doctor.py          # one-shot health report
   python bin/memory_doctor.py --fix    # attempt safe auto-repairs
   ```

2. **Prove capture end-to-end.** Have the agent take a turn, then confirm the row
   count moved and `last_capture` is recent. If `chatlog_status` shows a healthy
   mode but the row count is flat and the session is more than a few minutes old,
   **the hook is not writing** — the most common cause is a split-brain root
   (the MCP server and the Stop/PreCompact hook resolving different engine roots).
   See the split-brain section of [CHATLOG.md](CHATLOG.md) and the root-resolution
   rules in [ARCHITECTURE.md](ARCHITECTURE.md).

3. **Confirm the agent is registered.** Use the agent listing to verify a
   heartbeat exists for the onboarded agent id.

> **Rule of thumb:** never trust "the hook is configured" — trust "the row count
> went up." Capture failures are silent by design (a hook must never crash the
> host), so verification is on you.

---

## 2. Promote a note into a canonical (`belief`) memory

Chat-log turns are ephemeral raw material. Durable knowledge should live as a
first-class memory item you deliberately wrote.

- **Write it explicitly** with `memory_write`, giving it a clear `title`,
  `type` (e.g. `belief` / `user_fact` / `project`), and a real `importance`
  (higher = more retrieval weight, more decay resistance).
- **Make it survive indefinitely** by pinning (see §4) if it's canon that must
  never be aged out — org security policy, core preferences, homelab topology.
- **Link it** to related memories with `memory_link` (`related`, `supports`,
  `extends`, `references`, …) so graph traversal surfaces it alongside kin.

When a promoted belief later turns out to be *wrong* (not just stale), don't
edit it in place — supersede it (§3) so the audit trail is preserved.

---

## 3. Correct a belief: supersede, don't overwrite

Use `memory_supersede` to record an intentional update where new truth replaces
old. It is **non-destructive and bitemporal**:

- The old row is retained, marked `is_deleted=1`, with `valid_to` closed at the
  supersession point, and a `supersedes` edge (new → old) is written.
- The old memory stays reachable by id and via `memory_history`; an
  `as_of`-filtered retrieval before the supersession point still sees it valid.
- Only what changed needs to be passed — empty `type`/`title`/`scope` and a
  negative `importance` mean "inherit from the old memory."

```
memory_supersede(old_id=<uuid>, content="the corrected fact")
```

Prefer this over `memory_write`'s automatic contradiction detection when you know
*exactly* which prior memory you're replacing — supersede is deterministic;
auto-detection fires on a cosine + title heuristic.

---

## 4. Pin the memories that must never rot

Pinning marks a memory as canon that lifecycle maintenance must leave alone.

```
memory_pin(id=<uuid-or-8char-prefix>)
memory_unpin(id=<uuid-or-8char-prefix>)
```

**What a pin protects against — lifecycle *aging* only:**

| Force | Pinned behavior |
|---|---|
| Importance decay | exempt — importance held steady |
| Confidence decay-toward-neutral | exempt |
| Expiry purge (`expires_at`) | exempt — never purged |
| Retention TTL / max-count purge | exempt — never auto-archived |

**What a pin does *not* protect against — correction by new truth:**

- **Supersession still applies.** A pinned memory can be superseded (§3) when it
  becomes wrong. Pin means *"don't let this rot,"* not *"never update this."*
  Pinning "the NAS is at .54" shouldn't stop m3 from correcting it when the NAS
  moves to .55. (This boundary is enforced by
  `tests/test_pinned.py::test_pinned_row_can_still_be_superseded`.)

**When to pin:** core user preferences, org/security policies, stable
infrastructure topology — facts whose value is independent of how recently they
were accessed. **When not to pin:** anything expected to change; pin it after it
stabilizes, or supersede-and-repin.

---

## 5. Review and prune noisy memory

Run maintenance deliberately rather than only on the background cognitive loop:

- **`memory_dedup`** — collapse near-duplicate rows (cosine threshold). Start with
  a dry run to see what would merge before committing.
- **`memory_consolidate`** — merge overlapping notes into a single stronger memory.
- **`memory_maintenance`** — the lifecycle pass: importance decay, confidence
  reinforcement, expiry purge, retention enforcement. Pinned rows (§4) are
  skipped by every stage.

Curation subagents exist for larger passes (`curate-memory`, `curate-chatlog`)
when many writes have accumulated after a long session.

---

## 6. Inspect a memory's bitemporal timeline

To answer "how did this belief evolve — and did we correct it?":

```
memory_history(memory_id=<uuid>, limit=20)
```

It returns the audit trail: create, update, delete, and **supersede** events. Pair
it with the `supersedes` edges (`memory_link` types) to walk a full supersession
chain backward from the current memory to its earliest predecessor.

---

## 7. Debug why a result did (or didn't) rank

Both `memory_search` and `memory_suggest` can return a score breakdown. Pass
`explain=true` to `memory_search` (or use `memory_suggest`, which always
explains) to get, per result:

- a `_explanation` block with the numeric components — `vector`, `bm25`,
  `title_overlap`, `importance`, `recency_bonus`, `temporal_boost`, `raw_hybrid`;
- a human-readable **`reason`** string synthesized from those components
  (e.g. *"strong semantic match; title overlaps the query; high importance"*).

Reading the breakdown:

- **Under-ranked** (a memory you expected didn't surface high) — check whether
  `vector`/`bm25` are both weak (phrasing mismatch → consider better titles or an
  `intent_hint`), or whether a stronger row is legitimately winning.
- **Over-ranked** (noise ranking too high) — often a spurious `title_overlap` or
  an inflated `importance`; consider superseding or dedup.
- Use `recency_bias` for "current"/"latest" queries where newer should win.

For deeper ranking tuning, see the retrieval/ranking env knobs documented in
[ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md).
