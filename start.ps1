# Local development launcher (Windows).
# Runs the FastAPI app with auto-reload from the project root.
$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

& .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt | Out-Null

Write-Host "Starting Patreon Archive on http://localhost:8000 ..."
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
