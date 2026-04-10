#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."

if [ -f ".venv/bin/activate" ]; then
  source .venv/bin/activate
fi

export $(grep -v '^#' .env | xargs)
uvicorn main:app --host 127.0.0.1 --port 8000 --reload

