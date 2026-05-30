"""Standalone logic test for M3MemoryProvider.

Validates the provider's OWN logic — tool dispatch, circuit breaker, prefetch/
sync threading, the empty-query profile path — WITHOUT a hermes-agent checkout.

hermes-agent supplies `agent.memory_provider`, `tools.registry`, and
`hermes_constants`. We inject minimal stubs into sys.modules before importing
the provider, and swap in a fake M3Client so we test provider behavior, not the
live m3 catalog (that half is smoke-tested separately).

Run:  python .scratch/hermes-plugin/test_provider_logic.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent


# ── Stub the three hermes-agent-only modules ──────────────────────────────────

def _install_hermes_stubs() -> None:
    # agent.memory_provider.MemoryProvider — minimal ABC (plain object base;
    # the real one is an ABC, but our subclass implements every abstractmethod,
    # so a plain base exercises the same code).
    agent_pkg = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:  # noqa: D401 - stub
        pass

    mp_mod.MemoryProvider = MemoryProvider
    agent_pkg.memory_provider = mp_mod
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.memory_provider"] = mp_mod

    # tools.registry.tool_error — returns a JSON error string (mirrors hermes).
    tools_pkg = types.ModuleType("tools")
    reg_mod = types.ModuleType("tools.registry")

    def tool_error(msg: str) -> str:
        return json.dumps({"error": msg})

    reg_mod.tool_error = tool_error
    tools_pkg.registry = reg_mod
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = reg_mod

    # hermes_constants.get_hermes_home — a tmp path.
    hc_mod = types.ModuleType("hermes_constants")
    hc_mod.get_hermes_home = lambda: HERE / "_fake_hermes_home"
    sys.modules["hermes_constants"] = hc_mod


def _load_provider():
    """Import m3/__init__.py as a module after stubs are installed."""
    spec = importlib.util.spec_from_file_location(
        "m3_provider_under_test", HERE / "plugins" / "memory" / "m3" / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── Fake M3Client — records calls, controllable failures ──────────────────────

class FakeM3Client:
    def __init__(self, **kw):
        self.calls = []
        self.fail = False

    def _maybe_fail(self):
        if self.fail:
            raise RuntimeError("simulated m3 failure")

    def search(self, query, user_id, top_k):
        self.calls.append(("search", query, user_id, top_k))
        self._maybe_fail()
        return [{"content": f"hit for {query!r}", "score": 0.9}]

    def get_all(self, user_id, type):
        self.calls.append(("get_all", user_id, type))
        self._maybe_fail()
        return [{"content": "user likes spicy food"}]

    def conclude(self, content, user_id):
        self.calls.append(("conclude", content, user_id))
        self._maybe_fail()

    def chatlog_write(self, user_id, session_id, user_content, assistant_content):
        self.calls.append(("chatlog_write", session_id, user_content, assistant_content))
        self._maybe_fail()


# ── Test runner ───────────────────────────────────────────────────────────────

_PASS = 0
_FAIL = 0


def check(cond, label):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  PASS  {label}")
    else:
        _FAIL += 1
        print(f"  FAIL  {label}")


def main() -> int:
    _install_hermes_stubs()
    prov_mod = _load_provider()
    print("provider module loaded with stubbed hermes imports OK\n")

    # Build a provider with the fake client wired in.
    def fresh(fail=False):
        p = prov_mod.M3MemoryProvider()
        p.initialize(session_id="s1", user_id="alice")
        fc = FakeM3Client()
        fc.fail = fail
        p._client = fc  # bypass _get_client lazy import
        return p, fc

    # 1. metadata + schemas
    p, fc = fresh()
    check(p.name == "m3", "name == 'm3'")
    schemas = p.get_tool_schemas()
    names = {s["name"] for s in schemas}
    check(names == {"m3_profile", "m3_search", "m3_conclude"}, "3 tool schemas present")
    check(all("name" in s and "parameters" in s for s in schemas), "schemas are flat shape")
    check("alice" in p.system_prompt_block(), "system_prompt_block names the user")

    # 2. m3_search dispatch
    out = json.loads(p.handle_tool_call("m3_search", {"query": "tea", "top_k": 5}))
    check(out.get("count") == 1 and out["results"][0]["memory"].startswith("hit"),
          "m3_search returns structured results")
    check(("search", "tea", "alice", 5) in fc.calls, "m3_search forwarded query+user+top_k")

    # 3. m3_search top_k cap at 50
    p2, fc2 = fresh()
    p2.handle_tool_call("m3_search", {"query": "x", "top_k": 999})
    check(any(c[0] == "search" and c[3] == 50 for c in fc2.calls), "top_k capped at 50")

    # 4. m3_search missing query -> tool_error
    err = json.loads(p.handle_tool_call("m3_search", {}))
    check("error" in err, "m3_search missing query -> error")

    # 5. m3_profile (empty-query get_all path)
    out = json.loads(p.handle_tool_call("m3_profile", {}))
    check("spicy" in out.get("result", ""), "m3_profile returns user facts")
    check(any(c[0] == "get_all" and c[2] == "user_fact" for c in fc.calls),
          "m3_profile uses get_all(type='user_fact')")

    # 6. m3_conclude verbatim write
    out = json.loads(p.handle_tool_call("m3_conclude", {"conclusion": "alice uses vim"}))
    check(out.get("result") == "Fact stored.", "m3_conclude stores fact")
    check(("conclude", "alice uses vim", "alice") in fc.calls, "m3_conclude forwarded content")

    # 7. unknown tool
    err = json.loads(p.handle_tool_call("m3_bogus", {}))
    check("error" in err and "Unknown tool" in err["error"], "unknown tool -> error")

    # 8. circuit breaker: 5 failures trip it, then calls short-circuit
    pf, fcf = fresh(fail=True)
    for _ in range(5):
        pf.handle_tool_call("m3_search", {"query": "q"})
    check(pf._is_breaker_open(), "breaker open after 5 failures")
    before = len(fcf.calls)
    out = json.loads(pf.handle_tool_call("m3_search", {"query": "q"}))
    check("temporarily unavailable" in out.get("error", ""), "breaker short-circuits with message")
    check(len(fcf.calls) == before, "breaker prevents the underlying call")

    # 9. breaker resets after cooldown window
    pf._breaker_open_until = time.monotonic() - 1  # simulate cooldown elapsed
    check(not pf._is_breaker_open(), "breaker resets after cooldown")

    # 10. prefetch threading (queue -> join -> formatted block, then cleared)
    pp, fcp = fresh()
    pp.queue_prefetch("recall me")
    block = pp.prefetch("recall me")
    check(block.startswith("## m3 Memory"), "prefetch returns formatted block")
    check("hit for" in block, "prefetch block carries the recalled content")
    check(pp.prefetch("again") == "", "prefetch result cleared after read")

    # 11. sync_turn is non-blocking + eventually writes chatlog
    ps, fcs = fresh()
    ps.sync_turn("hello", "hi there", session_id="sess9")
    if ps._sync_thread:
        ps._sync_thread.join(timeout=5.0)
    check(any(c[0] == "chatlog_write" and c[1] == "sess9" for c in fcs.calls),
          "sync_turn writes chatlog with session_id")

    print(f"\n{_PASS} passed, {_FAIL} failed")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
