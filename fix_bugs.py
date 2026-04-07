import re

def fix_custom_tool_bridge():
    path = "bin/custom_tool_bridge.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("import logging\nimport sys", "import logging\nimport sys\nimport httpx")
    # fix the conn issue
    content = re.sub(
        r'conn = None\n\s*try:\n\s*conn = ctx\.get_sqlite_conn\(\)\n\s*cursor = conn\.cursor\(\)',
        r'try:\n        with ctx.get_sqlite_conn() as conn:\n            cursor = conn.cursor()',
        content
    )
    content = content.replace("conn.commit()", "")
    content = content.replace("finally:\n        if conn:\n            conn.close()", "")
    
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def fix_pg_sync():
    path = "bin/pg_sync.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    # fix main
    content = re.sub(
        r'sl_conn = None\n\s*try:\n\s*logger\.info\("Connecting to local SQLite DB at %s...", DB_PATH\)\n\s*sl_conn = ctx\.get_sqlite_conn\(\)\n\n\s*sl_cur = sl_conn\.cursor\(\)',
        r'try:\n        logger.info("Connecting to local SQLite DB at %s...", DB_PATH)\n        with ctx.get_sqlite_conn() as sl_conn:\n            sl_cur = sl_conn.cursor()',
        content
    )
    content = content.replace(
        "finally:\n        if sl_conn:\n            sl_conn.close()",
        ""
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def fix_m3_sdk():
    path = "bin/m3_sdk.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("_CIRCUITS = {}", "_CIRCUITS: dict[str, dict[str, Any]] = {}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def fix_memory_core():
    path = "bin/memory_core.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("def _conn() -> sqlite3.Connection:", "def _conn() -> Any:")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        
def fix_debug_agent():
    path = "bin/debug_agent_bridge.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace(
        "def _conn() -> sqlite3.Connection:\n    c = ctx.get_sqlite_conn()\n    c.row_factory = sqlite3.Row\n    return c",
        "from contextlib import contextmanager\n@contextmanager\ndef _conn():\n    with ctx.get_sqlite_conn() as c:\n        c.row_factory = sqlite3.Row\n        yield c"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def fix_bench_memory():
    path = "bin/bench_memory.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("rows = conn.execute(", "conn.execute(")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def fix_aider_patch():
    path = "bin/aider_patch.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    content = content.replace("import prompt_toolkit.shortcuts as _pts", "import prompt_toolkit.shortcuts as _pts  # type: ignore")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

fix_custom_tool_bridge()
fix_pg_sync()
fix_m3_sdk()
fix_memory_core()
fix_debug_agent()
fix_bench_memory()
fix_aider_patch()
print("Fixed!")
