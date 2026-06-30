#!/usr/bin/env python3
"""LLM-judged promotion pipeline: tightened candidate selection + SLM judge.

Stage 1 (--select): high-precision candidate selection (~1-2k) from chatlog.
Stage 2 (--smoke N / --run): batched judge via LM Studio; distill PROMOTE
         items to crisp facts. Writes accepted to --out jsonl.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from collections import defaultdict

cp = importlib.util.module_from_spec(
    s := importlib.util.spec_from_file_location("cp", os.path.join(os.path.dirname(__file__), "chatlog_prune.py")))
s.loader.exec_module(cp)

# ── Tightened signals (strict; precision over recall) ─────────────────────────
T_DECISION = re.compile(r"\b(we (decided|chose|went with|will use)|decision:|"
                        r"settled on|going with\b|the plan is to|chosen approach|"
                        r"production footgun|the rule is|canonical (form|way) is)\b", re.I)
T_DIRECTIVE = re.compile(r"\b(from now on|going forward|always (use|do|prefer)|"
                         r"never (use|do)|by default (use|we)|prefer \w+ (over|to)|"
                         r"make sure to always|remember that .* (is|are|should))\b", re.I)
T_SPEC = re.compile(r"\b(hostname is|runs on port \d|listening on \d|"
                    r"\b\d{1,3}(\.\d{1,3}){3}:\d+|configured (to|with) |"
                    r"the (endpoint|url|path|model|db|database) (is|=|:) |"
                    r"installed (at|in) |API key |lives (at|in) /|stored (at|in) )", re.I)
# status / progress vocabulary that DISQUALIFIES even if otherwise matching
DISQUALIFY = re.compile(r"\b(ETA|r/s|tok/s|windows? done|poll(ing)?|workers? alive|"
                        r"in[_\s]?progress|\d+\s*/\s*\d+\s+(done|sessions|windows)|"
                        r"completion[:\s]+\d|so far|still (running|going)|"
                        r"\bstatus\b|\bhealthy\b|no action|stable\.)\b", re.I)

LM_URL = os.environ.get("LM_URL", "http://localhost:1234/v1/chat/completions")
LM_MODEL = os.environ.get("LM_MODEL", "google/gemma-4-26b-a4b")
LM_TOKEN = os.environ.get("LM_API_TOKEN", "")


def select(db: str, min_age=14.0, lo=120, hi=4000):
    now = time.time()
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    try:
        return _select(c, now, min_age, lo, hi)
    finally:
        c.close()  # close even if classify()/float() raises mid-loop


def _select(c, now, min_age, lo, hi):
    rows = c.execute("""SELECT id, CASE WHEN title LIKE 'user@%' THEN 'user'
        WHEN title LIKE 'assistant@%' THEN 'assistant' WHEN title LIKE 'system@%' THEN 'system'
        WHEN title LIKE 'tool@%' THEN 'tool' ELSE '' END role, content, importance, created_at
        FROM memory_items WHERE type='chat_log' AND is_deleted=0""").fetchall()
    cluster, norms = defaultdict(int), {}
    for r in rows:
        n = cp._norm_key(r["content"] or ""); norms[r["id"]] = n
        if n: cluster[n] += 1
    seen = set(); out = []
    class A: keep_imp_floor=0.4; status_min_cluster=5; generic_imp_max=0.3; no_generic=True
    for r in rows:
        content = (r["content"] or "").strip()
        if not (lo <= len(content) <= hi):
            continue
        if cp._age_days(r["created_at"], now) < min_age:
            continue
        if DISQUALIFY.search(content):
            continue
        imp = float(r["importance"]) if r["importance"] is not None else 0.3
        if cp.classify(r["role"], content, imp, norms[r["id"]], cluster[norms[r["id"]]], A())[0] is not None:
            continue
        sig = ("decision" if T_DECISION.search(content) else
               "directive" if T_DIRECTIVE.search(content) else
               "spec" if T_SPEC.search(content) else None)
        if not sig:
            continue
        k = norms[r["id"]]
        if k in seen:
            continue
        seen.add(k)
        out.append({"id": r["id"], "role": r["role"], "signal": sig,
                    "created_at": r["created_at"], "content": content})
    return out


RUBRIC = """You curate an AI agent's long-term memory. Decide if each chat turn contains a DURABLE, REUSABLE fact worth keeping forever.

PROMOTE only if it states something stable and reusable later:
- a decision WITH its rationale, a standing preference/directive/convention,
- a configuration/spec fact (hostnames, ports, paths, versions, hardware),
- a learned technical fact or non-obvious gotcha.

SKIP (most turns) if it is: status/progress, a one-off action narration, a
question, transient state, meta-chatter, or already-obvious.

For each item return: {"i":<index>,"verdict":"PROMOTE"|"SKIP",
"type":"fact"|"decision"|"preference"|"reference"|"knowledge",
"title":"<=8 words","fact":"<=2 sentences, standalone, no 'the assistant/user said'"}
Output ONLY a JSON array, one object per input item, same order."""


def judge(items):
    # Only ever speak HTTP(S) to the LLM endpoint. LM_URL is operator-supplied,
    # but a misconfigured file:/ or custom scheme would otherwise be opened
    # blindly (Bandit B310 / CWE-22); reject anything but http/https up front.
    if urllib.parse.urlparse(LM_URL).scheme not in ("http", "https"):
        raise ValueError(f"LM_URL must be http(s); refusing scheme in {LM_URL!r}")
    payload = "\n".join(f'[{i}] ({it["signal"]}/{it["role"]}) {it["content"][:1200]}'
                        for i, it in enumerate(items))
    body = json.dumps({"model": LM_MODEL, "temperature": 0, "max_tokens": 2200,
                       "messages": [{"role": "system", "content": RUBRIC},
                                    {"role": "user", "content": payload}]}).encode()
    req = urllib.request.Request(LM_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LM_TOKEN}"})
    with urllib.request.urlopen(req, timeout=300) as resp:  # nosec B310 - scheme guarded above
        data = json.load(resp)
    txt = data["choices"][0]["message"]["content"]
    m = re.search(r"\[.*\]", txt, re.S)
    return json.loads(m.group(0)) if m else []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--select", action="store_true")
    ap.add_argument("--smoke", type=int, default=0)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--batch", type=int, default=8)
    # Platform-appropriate temp dir (Bandit B108): /tmp does not exist on
    # Windows, where this project commonly runs. Override with --out as needed.
    ap.add_argument("--out",
                    default=os.path.join(tempfile.gettempdir(), "promote_accepted.jsonl"))
    ap.add_argument("--samples", type=int, default=8)
    args = ap.parse_args()

    cands = select(args.db)
    bysig = defaultdict(int)
    for c in cands: bysig[c["signal"]] += 1
    print(f"tightened candidates: {len(cands)}  by_signal={dict(bysig)}")
    if args.select:
        for c in cands[:args.samples]:
            print(f"  [{c['signal']}/{c['role']}] {' '.join(c['content'].split())[:160]}")
        return
    todo = cands[:args.smoke] if args.smoke else cands
    accepted = []; t0 = time.time()
    # UTF-8 + LF: chatlog facts carry em-dashes/quotes/emoji that the Windows
    # locale codepage can't encode — default text mode would UnicodeEncodeError
    # mid-run and lose minutes of LLM work. newline="" avoids CRLF in JSONL.
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        for b in range(0, len(todo), args.batch):
            batch = todo[b:b + args.batch]
            try:
                verdicts = judge(batch)
            except Exception as e:
                print(f"  batch {b}: ERROR {e}"); continue
            for it, v in zip(batch, verdicts):
                if isinstance(v, dict) and v.get("verdict") == "PROMOTE" and v.get("fact"):
                    rec = {"source_id": it["id"], "signal": it["signal"],
                           "type": v.get("type", "fact"), "title": v.get("title", ""),
                           "fact": v["fact"]}
                    accepted.append(rec); f.write(json.dumps(rec) + "\n"); f.flush()
            done = min(b + args.batch, len(todo))
            print(f"  judged {done}/{len(todo)}  accepted={len(accepted)}  ({time.time()-t0:.0f}s)")
    print(f"DONE accepted={len(accepted)} -> {args.out}")
    # show a few accepted
    for r in accepted[:args.samples]:
        print(f"  +{r['type']}: {r['title']} :: {r['fact'][:140]}")


if __name__ == "__main__":
    raise SystemExit(main())
