import os
from fastapi import APIRouter, HTTPException
from pathlib import Path

router = APIRouter(prefix="/tools/fs", tags=["filesystem"])

_DEFAULT_BASE = Path.home()
FS_BASE_DIR = Path(os.environ.get("MAC_AGENT_FS_BASE", str(_DEFAULT_BASE))).expanduser().resolve()


@router.post("/read")
def fs_read(body: dict):
    raw = body["path"]
    candidate = Path(raw).expanduser().resolve()
    try:
        candidate.relative_to(FS_BASE_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="path outside allowed base")
    return {"content": candidate.read_text(encoding="utf-8")}


@router.post("/write")
def fs_write(body: dict):
    raw = body["path"]
    candidate = Path(raw).expanduser().resolve()
    try:
        candidate.relative_to(FS_BASE_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="path outside allowed base")
    candidate.write_text(body["content"], encoding="utf-8")
    return {"status": "ok"}
