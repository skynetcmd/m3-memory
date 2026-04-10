from fastapi import FastAPI
from router.router import router as router_router
from memory.memory_api import router as memory_router
from tools.filesystem import router as fs_router
from tools.router_call import router as llm_tool_router
from tools.home_discovery import router as home_router

app = FastAPI(title="mac-agent")

app.include_router(router_router)
app.include_router(memory_router)
app.include_router(fs_router)
app.include_router(llm_tool_router)
app.include_router(home_router)

