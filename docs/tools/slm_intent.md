---
tool: bin/slm_intent.py
sha1: c626fc255c3a
mtime_utc: 2026-04-28T00:28:26.603595+00:00
generated_utc: 2026-04-28T15:48:17.547194+00:00
private: false
---

# bin/slm_intent.py

## Purpose

Small-Language-Model intent classifier with named-profile configs.

One compact LLM call that maps a user query (or other short text) to a
label from a fixed set — used by intent-aware retrieval, chatlog
triage, and benchmark harness routing. Each call site picks a
**profile** by name, and each profile is a YAML file pinning its own
endpoint URL, model, prompt, label vocabulary, and timeout.

Why profiles (vs. a single global config):
  - Bench harness wants a prompt tuned for LongMemEval categories.
  - Chatlog triage wants a different label set (sensitive / routine /
    administrative) and probably a faster model.
  - General memory routing wants a middle-ground prompt.
Profiles let each subsystem iterate on its own prompt file without
touching the others.

Resolution order for profile **content**:
  1. ``classify_intent(profile=...)`` kwarg
  2. ``M3_SLM_PROFILE`` env var
  3. ``"default"`` (must exist in one of the profile dirs)

Resolution order for profile **file location** (first match wins):
  1. ``M3_SLM_PROFILES_DIR`` env var — may be a single path OR a
     ``os.pathsep``-separated list (e.g. for bench harnesses that
     want to stack their own dir ahead of the repo default).
  2. ``<M3_MEMORY_ROOT>/config/slm/``

Gate: ``M3_SLM_CLASSIFIER={1|true|yes}``. When off, ``classify_intent``
returns ``None`` immediately — callers should treat that as "no intent
signal available, fall through to heuristics."

Config YAML shape::

    url: http://127.0.0.1:11434/v1/chat/completions
    model: qwen2.5:1.5b-instruct
    api_key_service: LM_API_TOKEN   # optional; looked up via auth_utils
    timeout_s: 10.0
    temperature: 0
    system: |
      <system prompt>
    labels:
      - label-one
      - label-two
    fallback: label-one              # returned when model output matches no label

Profiles are cached by name once loaded; call ``invalidate_cache()``
after editing a YAML for the change to take effect in a long-running
process.

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `M3_MEMORY_ROOT`
- `M3_SLM_CLASSIFIER`
- `M3_SLM_PROFILE`
- `M3_SLM_PROFILES_DIR`

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 432)
- `httpx.AsyncClient()` (line 489)
- `httpx.AsyncClient()` (line 539)


## Notable external imports

- `httpx`
- `yaml`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
