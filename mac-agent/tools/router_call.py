import os
import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/tools/llm", tags=["llm"])

ROUTER_URL = os.getenv("ROUTER_URL", "http://localhost:8000/v1/chat/completions")


@router.post("/chat")
async def llm_chat(body: dict):
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(ROUTER_URL, json=body)
    return resp.json()

