import os
import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/home", tags=["home"])

HOME_SERVICES = [
    os.getenv("HOME_ROUTER_URL"),
    os.getenv("HOME_NODE_RED_URL"),
]


@router.get("/discover")
async def discover():
    found = []
    async with httpx.AsyncClient(timeout=2) as client:
        for url in HOME_SERVICES:
            if not url:
                continue
            try:
                r = await client.get(f"{url}/ping")
                if r.status_code == 200:
                    found.append({"url": url, "status": "online"})
            except Exception:
                continue
    return {"services": found}

