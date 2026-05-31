# Patreon Archive - FastAPI app that serves the archive and can run live
# Patreon syncs via the Node-based `patreon-dl` CLI.
FROM python:3.13-slim

# Install Node.js 20 (for patreon-dl) and curl (for the healthcheck).
# patreon-dl pulls in better-sqlite3, which has no prebuilt binary for this
# platform and must be compiled from source, so build-essential is installed
# for the npm step and then purged to keep the runtime image small.
# patreon-dl v3 also drives a headless Chromium (via Puppeteer) to fetch posts;
# the `chromium` package supplies the shared libraries that Chromium needs to
# launch (libglib, libnss3, libgbm, ...) — without it fetching fails with
# "error while loading shared libraries: libglib-2.0.so.0".
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs build-essential chromium \
    && npm install -g patreon-dl \
    && apt-get purge -y --auto-remove build-essential \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# patreon-dl runs its bundled Chromium as root here (the bind-mounted data dir
# is only writable as root) and exposes no way to set browser args. Chrome
# refuses its sandbox as root, so wrap the binary to always pass --no-sandbox.
RUN CHROME="$(find /root/.cache/puppeteer -type f -name chrome | head -n1)" \
    && if [ -n "$CHROME" ]; then \
         mv "$CHROME" "$CHROME-bin" \
         && printf '#!/bin/sh\nexec "%s" --no-sandbox --disable-gpu --disable-dev-shm-usage "$@"\n' "$CHROME-bin" > "$CHROME" \
         && chmod +x "$CHROME"; \
       else echo "WARNING: bundled Chromium not found; skipping no-sandbox wrapper"; fi

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
