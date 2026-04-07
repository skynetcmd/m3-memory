#!/bin/bash
# Start the Homelab Dashboard FastAPI Backend

cd [M3_MEMORY_ROOT]/homelab-dashboard/backend

# Activate the virtual environment
source venv/bin/activate

# Execute the backend server
exec python main.py