#!/usr/bin/env bash
ps aux | grep "uvicorn main:app" | grep -v grep || echo "mac-agent not running"

