# Patreon Archive Service (FastAPI + SQLite)

This project creates a local service to:

- Download posts from creators you are subscribed to (using `patreon-dl`)
- Store post metadata/content in SQLite
- Browse and search posts in a local web UI

## Why this approach

`patreon-dl` is actively maintained and already handles Patreon session-cookie auth, media extraction, and edge cases around embedded media. This service adds:

- A normalized database for search and filtering
- A simple API you can extend
- A local UI for browsing and full-text search

## Features in this starter

- Sync endpoint per creator
- Stores title, content, links, tags, publish timestamp, raw payload
- SQLite full-text search via FTS5
- Minimal browser UI

## Setup

1. Install prerequisites:
   - Python 3.11+
   - Node.js 20+
   - `patreon-dl` (`npm i -g patreon-dl`) or use `npx patreon-dl`

2. Create and activate a virtual environment.

3. Install Python dependencies:

   ```
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and fill:
   - `PATREON_COOKIE` (from your own logged-in Patreon session)
   - `PATREON_DL_COMMAND` (`patreon-dl` or `npx patreon-dl`)

5. Run:

   ```
   uvicorn app.main:app --reload
   ```

6. Open http://127.0.0.1:8000

## Accounts / login

The web UI is gated by a login. There is **no public sign-up** — accounts are created
from the command line:

```
python -m app.manage_users add <username>      # prompts for a password
python -m app.manage_users list
python -m app.manage_users passwd <username>
python -m app.manage_users delete <username>
```

Inside Docker, run it against the container:

```
docker compose exec app python -m app.manage_users add shay
```

Sessions are signed cookies (only the user id is stored). The signing key comes from
`SESSION_SECRET` in `.env`; if unset, one is generated and persisted to
`data/.session_secret` so logins survive restarts. In production (behind nginx TLS) set
`SESSION_COOKIE_SECURE=true` so the cookie is HTTPS-only.

Favourites (the ☆ on each post, and the **Favourites** tab) are stored in the database
against your account, so they follow you across browsers and devices.

## Archive modes

Each creator is archived in one of three modes, picked from the dropdown next to the
creator box when you first sync them (and changeable by re-syncing with a different
mode). The mode decides what the archive actually keeps:

| Mode | Post body | Links | Attached files | TMDB genre tags |
| --- | --- | --- | --- | --- |
| **Video archive** | plain text | yes | — | yes |
| **Content archive** | formatted HTML | yes | **downloaded** to `data/raw/` | no |
| **Links archive** | formatted HTML | yes | listed, **never downloaded** | no |

**Links archive** is for creators whose files are far too large to mirror — battlemap
packs, VTT bundles, 4K asset archives. It keeps the post exactly as written (headings,
lists, emphasis) plus every URL the post points at, and records attachments as metadata
only: you see the filename and size, and follow the original post to fetch them.

Links are captured from all four places Patreon hides them — `<a href>` in the body,
bare URLs in the text, hyperlink marks in Patreon's rich-text source, and the post
embed — then split on display, with off-Patreon links (Drive folders, storefronts,
mirrors) listed first and Patreon's own self-links dimmed below them.

Video posts with no links are hidden from listings; content and links posts are not,
since their value is the body rather than something watchable.

## API quick reference

- `POST /api/sync/{creator}`: download + import creator posts (Patreon)
- `POST /api/patreon/sync/{creator}?mode=video|content|links`: FlareSolverr scrape +
  import, setting the creator's archive mode (see **Archive modes** above)
- `POST /api/sync/cinebingers/{collection_name}`: scrape and import cinebingers.ca videos
- `GET /api/posts?q=...&creator=...&tag=...`
- `GET /api/posts/{id}`
- `GET /api/tags`
- `POST /api/login` / `POST /api/logout`: session login/logout (form-encoded)
- `GET /api/favourites`: the logged-in user's favourited posts
- `POST /api/favourites/{post_id}` / `DELETE /api/favourites/{post_id}`: star / unstar

## Cinebingers.ca Source

This service also supports archiving video collections from [cinebingers.ca](https://cinebingers.ca), a website that curates YouTube videos.

### How it works

1. The cinebingers scraper fetches video listings from cinebingers.ca
2. Extracts video titles, YouTube URLs, and metadata
3. Saves each video as a `post_info_*.json` file (similar to patreon-dl format)
4. Imports the data into the SQLite database

### Usage

To scrape and import a collection from cinebingers.ca:

```bash
curl -X POST http://localhost:8000/api/sync/cinebingers/anime
```

This will:
1. Scrape all videos from cinebingers.ca
2. Store them in `data/raw/anime/`
3. Import them into the database under creator "anime"

You can then search and browse them like any other source.

### Example response

```json
{
  "creator": "anime",
  "downloaded": false,
  "imported_posts": 42
}
```

### Note about cinebingers.ca access

The cinebingers.ca scraper works with publicly available data. However, the site may require an active session cookie in your browser to access videos. If you encounter authentication issues:

1. Visit https://cinebingers.ca in your browser and ensure you're logged in
2. The scraper will automatically include browser headers to attempt access
3. If the endpoint is session-protected, you may need to inspect network requests and add cookies to the scraper configuration



- This is intended for content you are authorized to access.
- Keep your Patreon cookie secret.
- The cinebingers scraper doesn't require authentication—it uses publicly available data.
- Current parser is intentionally tolerant and can be tightened once you inspect your downloaded `post_info` files.
