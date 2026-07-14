"""Regression: memory_bridge wrappers must strip the injected `timeout` param.

mcp_tool_catalog injects a `timeout` parameter into every tool's published
schema (dispatch machinery, user-selectable per call). The catalog's own
execute_tool() pops it before invoking the impl, but the memory_bridge typed-
function path is a SEPARATE dispatcher — it built the wrapper from the schema
(so `timeout` is a real kwarg on the generated _impl) yet never popped it,
passing it straight to spec.impl(**args). Any impl with a strict signature
(chatlog_search_impl, chatlog_status_impl, ...) then raised
"unexpected keyword argument 'timeout'". This test guards both wrapper
branches (sync + async).
"""
import asyncio


def _strict_sync_impl(query):
    # No **kwargs — reproduces the strict signature that used to crash.
    return f"sync:{query}"


async def _strict_async_impl(query):
    return f"async:{query}"


def _spec(impl, is_async):
    import mcp_tool_catalog as cat

    return cat.ToolSpec(
        name="_probe",
        description="probe",
        parameters={
            "type": "object",
            # `timeout` is present because mcp_tool_catalog injects it into every
            # tool schema — so the generated wrapper signature accepts it and it
            # reaches _wrapper(), which must pop it before the strict impl.
            "properties": {
                "query": {"type": "string"},
                "timeout": {"type": "number"},
            },
            "required": ["query"],
        },
        impl=impl,
        is_async=is_async,
    )


def test_sync_wrapper_strips_injected_timeout():
    import memory_bridge

    fn = memory_bridge._build_typed_function(_spec(_strict_sync_impl, is_async=False))
    # Passing the injected timeout must NOT reach the strict impl.
    out = fn(query="hi", timeout=30)
    assert out == "sync:hi"
    assert "unexpected keyword" not in out


def test_async_wrapper_strips_injected_timeout():
    import memory_bridge

    fn = memory_bridge._build_typed_function(_spec(_strict_async_impl, is_async=True))
    out = asyncio.run(fn(query="hi", timeout=30))
    assert out == "async:hi"
    assert "unexpected keyword" not in out


# ── Sync-impl offload to a worker thread (event-loop-blocking fix) ────────────
# On the MCP registration path (for_mcp=True), a SYNC impl must run via
# asyncio.to_thread so its blocking SQLite work does not freeze the single
# stdio-server event loop. Off the MCP path (for_mcp=False, the module-level
# exposure used by tests/direct callers), it must stay a plain sync function
# returning a value — NOT a coroutine.
import inspect
import threading


def test_sync_impl_stays_sync_off_mcp_path():
    """for_mcp=False (default): sync impl -> plain sync fn returning a value.
    Direct callers like task_create('title', ...) depend on this."""
    import memory_bridge

    fn = memory_bridge._build_typed_function(_spec(_strict_sync_impl, is_async=False))
    assert not inspect.iscoroutinefunction(fn)
    assert fn(query="hi") == "sync:hi"


def test_sync_impl_offloaded_on_mcp_path():
    """for_mcp=True: sync impl -> async fn that runs the impl in a WORKER thread
    (off the event loop), preserving the string result."""
    import memory_bridge

    seen = {}

    def _thread_probe_impl(query):
        seen["thread"] = threading.current_thread().name
        return f"sync:{query}"

    fn = memory_bridge._build_typed_function(
        _spec(_thread_probe_impl, is_async=False), for_mcp=True
    )
    assert inspect.iscoroutinefunction(fn), "MCP-path sync impl must become async"

    async def _drive():
        loop_thread = threading.current_thread().name
        out = await fn(query="hi", timeout=30)
        return loop_thread, out

    loop_thread, out = asyncio.run(_drive())
    assert out == "sync:hi"
    assert seen["thread"] != loop_thread, "impl must run OFF the event-loop thread"


def test_async_impl_unaffected_by_for_mcp():
    """An async impl is already off-loop-friendly; for_mcp doesn't change it —
    it stays an async fn either way."""
    import memory_bridge

    f_plain = memory_bridge._build_typed_function(_spec(_strict_async_impl, is_async=True))
    f_mcp = memory_bridge._build_typed_function(
        _spec(_strict_async_impl, is_async=True), for_mcp=True
    )
    assert inspect.iscoroutinefunction(f_plain)
    assert inspect.iscoroutinefunction(f_mcp)
    assert asyncio.run(f_mcp(query="hi", timeout=1)) == "async:hi"
