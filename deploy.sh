#!/bin/bash
# One-command deploy/redeploy for the VPS.
# Guards against a missing .env, then (re)builds and starts the stack.
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "ERROR: .env not found."
    echo "  cp .env.example .env  &&  edit it (PATREON_COOKIE, TMDB_API_KEY)"
    exit 1
fi

if [ ! -d data ]; then
    echo "WARNING: ./data not found — the archive DB/downloads live here."
    echo "  rsync the data dir up, or it will start empty."
fi

echo "Building and starting patreon-archive..."
docker compose up -d --build

echo ""
echo "Status:"
docker compose ps

echo ""
echo "Health:"
curl -fsS http://127.0.0.1:8001/api/health && echo " OK" \
    || echo "  not healthy yet — check: docker compose logs -f"
