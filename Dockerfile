# Patreon Archive - FastAPI app that serves the archive and can run live
# Patreon syncs via the Node-based `patreon-dl` CLI.
FROM python:3.13-slim

# Install Node.js 20 (for patreon-dl) and curl (for the healthcheck).
# patreon-dl pulls in better-sqlite3, which has no prebuilt binary for this
# platform and must be compiled from source, so build-essential is installed
# for the npm step and then purged to keep the runtime image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs build-essential \
    && npm install -g patreon-dl \
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code. The `data/` directory is provided at runtime via a
# bind mount (see docker-compose.yml), so it is intentionally not copied here.
COPY app ./app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# Templates and static files are referenced relative to the working dir, so the
# process must run from /app.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
