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
