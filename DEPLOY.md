# Deploying Patreon Archive to the VPS

Same shape as the Consensus (Head2Head) deploy: the app runs in Docker bound to
a loopback port, and the host nginx reverse-proxies a subdomain to it.

- **Domain:** `archive.shay-h.com`
- **App port (loopback):** `127.0.0.1:8001`
- **Container:** `patreon-archive` (FastAPI + Node `patreon-dl` for live syncs)

## 1. Copy the project to the VPS

```bash
# From your workstation (excludes venv; data is synced separately below)
rsync -av --progress \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.log' \
  ./PatreonDL/ youruser@your-vps:/opt/patreon-archive/
```

## 2. Sync the archive data (the DB + raw downloads, ~1.3GB)

`data/` is git-ignored and not baked into the image — it's a bind-mount.

```bash
rsync -av --progress ./PatreonDL/data/ youruser@your-vps:/opt/patreon-archive/data/
```

You can re-run this anytime to push a freshly imported DB up to the server.

## 3. Configure secrets on the VPS

```bash
cd /opt/patreon-archive
cp .env.example .env
nano .env   # set PATREON_COOKIE, TMDB_API_KEY, etc.
```

Leave `PATREON_DL_COMMAND="patreon-dl"` — it's installed globally in the image.

## 4. Build & start

```bash
make deploy        # or: ./deploy.sh  — guards against a missing .env, then builds + starts
```

(Equivalent to `docker compose up -d --build`, with an `.env`/`data` sanity check.)

Verify it's healthy and listening on the loopback:

```bash
docker compose ps
curl -f http://127.0.0.1:8001/api/health   # -> {"status":"ok"}
```

## 5. Wire up nginx

```bash
sudo cp nginx.conf /etc/nginx/sites-available/archive.shay-h.com
sudo ln -s /etc/nginx/sites-available/archive.shay-h.com /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Point the `archive.shay-h.com` DNS A record at the VPS, then enable HTTPS:

```bash
sudo certbot --nginx -d archive.shay-h.com
```

## Everyday commands

```bash
make deploy    # build + (re)start, with .env guard   (./deploy.sh)
make logs      # tail app logs
make down      # stop
make restart   # restart
make ps        # status
make prune-raw # delete downloaded image media under data/raw (one-off)
```

The archive data lives in `./data` on the host and survives rebuilds.

## Disk usage / raw media

The app serves entirely from `data/patreon_archive.db`; the downloaded files
under `data/raw` are only patreon-dl's working area. Image media there is
**auto-pruned after each successful sync** (`PRUNE_RAW_MEDIA_AFTER_SYNC=true`),
keeping the `.patreon-dl` status caches and the DB. Because patreon-dl tracks
what's done via that status cache (not the files on disk), pruning never causes
re-downloads — syncs stay incremental. To clear pre-existing media in one shot,
run `make prune-raw`.
