"""Replace mem0 with m3 — a one-line import swap, plus the m3-native extras.

Before (mem0):   from mem0 import Memory
After  (m3):     from m3_memory.langchain import Memory   # only this line changes

Run:  pip install "m3-memory[langchain]"  &&  python mem0_migration.py
m3 needs no API key or server; it self-configures on first use.
"""

from m3_memory.langchain import Memory  # ← the only change from a mem0 program

USER = "alex"


def main() -> None:
    # mem0 lets you pass user_id per call OR set it once; we set it once here so
    # the calls below stay short. (m3 enforces per-user tenancy — user_id is
    # required, either on the constructor or every call.)
    memory = Memory(user_id=USER)

    # --- mem0-identical usage: add() then search() -------------------------
    messages = [
        {"role": "user", "content": "I prefer dark mode and I'm allergic to peanuts."},
        {"role": "assistant", "content": "Noted — dark mode on, peanuts flagged."},
    ]
    memory.add(messages)

    # Read-your-writes: the turn is written synchronously and is immediately
    # searchable via m3's FTS index (a query sharing words with the stored text
    # matches now). m3 ALSO extracts richer facts + backfills vectors
    # asynchronously, so SEMANTIC-only matches (words not literally present)
    # sharpen a beat later.
    relevant = memory.search("dark mode", limit=3)
    print("recall (mem0-compatible shape):")
    for m in relevant["results"]:                    # {"results": [{"id","memory","score",...}]}
        score = m.get("score") or 0.0
        print(f"  - {m['memory']}   (score={score:.3f})")

    # --- what you GAIN over mem0 (same object, extra typed methods) --------
    # 1) CONTRADICTION: evolve a fact instead of stacking a conflicting copy.
    got = memory.search("dark mode", limit=1)["results"]
    if got:
        memory.supersede(got[0]["id"], "I switched to light mode.")
        print("\nsuperseded the dark-mode fact (no conflicting duplicate left behind)")

    # 2) TEMPORAL: what was true at a point in time (native bitemporal query).
    #    `as_of` is an m3-native extra on the SAME .search() signature.
    past = memory.search("lighting", as_of="2026-01-01")
    print(f"as-of query returned {len(past['results'])} historical fact(s)")

    # 3) Every result already carries m3's extra signal a plain mem0 user never
    #    sees — confidence + bitemporal validity — in result["metadata"].
    latest = memory.search("lighting", limit=1)["results"]
    if latest:
        md = latest[0].get("metadata", {})
        print(f"first-class signal on the result: "
              f"confidence={md.get('confidence')}, valid_from={md.get('valid_from')}")

    # 4) FORGETTING: commanded delete (GDPR Art. 17) — mem0 has no forget verb.
    # memory.forget()   # uncomment to wipe this user's memories


if __name__ == "__main__":
    main()
