#!/bin/bash
set -euo pipefail
# Housekeeping script for M3 Memory
# Rotates logs and clears temporary files

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$(dirname "$SCRIPT_DIR")/logs"
MAX_LOG_AGE_DAYS=7

echo "🧹 Starting Housekeeping..."

# 1. Clean up logs older than 7 days
if [ -d "$LOG_DIR" ]; then
    echo "📜 Rotating logs in $LOG_DIR..."
    find "$LOG_DIR" -type f -name "*.log" -mtime +$MAX_LOG_AGE_DAYS -delete
    echo "✅ Logs older than $MAX_LOG_AGE_DAYS days removed."
else
    echo "⚠️  Log directory $LOG_DIR not found."
fi

# 2. Clear AI Workspace temp files
TEMP_DIR="$HOME/.gemini/tmp/m3-memory"
if [ -d "$TEMP_DIR" ]; then
    echo "📁 Clearing AI workspace temporary directory..."
    rm -rf "${TEMP_DIR:?}"/*
    echo "✅ Workspace temp cleared."
fi

# 3. Check for oversized model cache
MODEL_SIZE=$(du -sh ~/.lmstudio/models | awk '{print $1}')
echo "📊 Current Model Cache Size: $MODEL_SIZE"

echo "✨ Housekeeping complete."
