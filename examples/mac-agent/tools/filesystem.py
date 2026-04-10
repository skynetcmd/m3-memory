from fastapi import APIRouter
from pathlib import Path

router = APIRouter(prefix="/tools/fs", tags=["filesystem"])


@router.post("/read")
def fs_read(body: dict):
    path = Path(body["path"]).expanduser()
    return {"content": path.read_text(encoding="utf-8")}


@router.post("/write")
def fs_write(body: dict):
    path = Path(body["path"]).expanduser()
    path.write_text(body["content"], encoding="utf-8")
    return {"status": "ok"}

