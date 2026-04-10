import os
import sqlite3
from fastapi import APIRouter
from .embeddings import embed

DB_PATH = os.getenv("MEMORY_DB_PATH", "memory/store.sqlite")
router = APIRouter(prefix="/memory", tags=["memory"])


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY,
            text TEXT,
            vector BLOB
        )
        """
    )
    return conn


@router.post("/write")
def write_memory(item: dict):
    text = item["text"]
    vec = embed([text])[0]
    conn = get_conn()
    conn.execute(
        "INSERT INTO memories (text, vector) VALUES (?, ?)",
        (text, bytes(str(vec), encoding="utf-8")),
    )
    conn.commit()
    return {"status": "ok"}


@router.post("/query")
def query_memory(item: dict):
    query = item["query"]
    _ = embed([query])[0]
    # TODO: implement real vector search (FAISS/HNSW)
    return {"results": []}

