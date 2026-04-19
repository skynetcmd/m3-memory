import os
from fastapi import APIRouter, HTTPException
from pathlib import Path

router = APIRouter(prefix="/tools/fs", tags=["filesystem"])

_DEFAULT_BASE = Path.home()
FS_BASE_DIR = Path(os.environ.get("MAC_AGENT_FS_BASE", str(_DEFAULT_BASE))).expanduser().resolve()


def _safe_path(raw: str) -> Path:
    candidate = Path(raw).expanduser().resolve()
    if not candidate.is_relative_to(FS_BASE_DIR):
        raise HTTPException(status_code=400, detail="path outside allowed base")
    return candidate


@router.post("/read")
def fs_read(body: dict):
    path = _safe_path(body["path"])
    return {"content": path.read_text(encoding="utf-8")}


@router.post("/write")
def fs_write(body: dict):
    path = _safe_path(body["path"])
    path.write_text(body["content"], encoding="utf-8")
    return {"status": "ok"}
