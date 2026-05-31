#!/bin/bash
# Local development launcher (Linux/macOS).
# Runs the FastAPI app with auto-reload from the project root.
set -e

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt >/dev/null

echo "Starting Patreon Archive on http://localhost:8000 ..."
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
