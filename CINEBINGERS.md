# Cinebingers.ca Integration

This document explains how the cinebingers.ca source works and how to use it.

## Overview

The cinebingers scraper integrates cinebingers.ca video archives into the main archive system. It:

- Fetches video listings from cinebingers.ca via HTTP
- Parses HTML to extract video titles, YouTube URLs, and metadata
- Saves data in the same JSON format as patreon-dl for compatibility
- Integrates with the existing SQLite database and search UI

## Architecture

### Scraper Module (`app/cinebingers_scraper.py`)

Key components:

- `CinebingersVideo`: Dataclass representing a single video
- `parse_cinebingers_html()`: Parses HTML response and extracts video cards
- `fetch_cinebingers_page()`: Fetches a single page from cinebingers.ca
- `scrape_all_cinebingers_videos()`: Iterates through all pages
- `export_videos_to_json_files()`: Saves videos as post_info JSON files

### Integration Points

- **sync_service.py**: `scrape_cinebingers()` function handles the scraping workflow
- **main.py**: `POST /api/sync/cinebingers/{collection_name}` endpoint
- **models.py**: Uses existing `Post`, `Creator`, `Link`, `Tag` models

## HTML Parsing

The scraper extracts data from this HTML structure:

```html
<div class="col-6 col-sm-6 col-md-4 col-lg-3">
    <div class="card">
        <a href="https://cinebingers.ca/{VIDEO_ID}"></a>
        <div class="card-body" style="position: relative;">
            <div class="row">
                <div class="col-9">
                    <h6 class="card-title mb-1">{TITLE}</h6>
                </div>
                <div class="col-3">
                    <span onClick=" return copy('{YOUTUBE_URL}')">
                        <i class="fa-solid fa-copy"></i>
                    </span>
                </div>
            </div>
        </div>
    </div>
</div>
```

Extracts:
- **Title**: From `<h6 class="card-title mb-1">`
- **Video ID**: From first cinebingers link (`/rQhcCXMa88`)
- **YouTube URL**: From `copy()` onclick handler
- **Cinebingers URL**: From the anchor href

## Data Format

Each video is saved as `post_info_{VIDEO_ID}.json`:

```json
{
  "id": "rQhcCXMa88",
  "title": "The Great Escape",
  "url": "https://www.youtube.com/watch?v=UIeFmp-KDIU",
  "postUrl": "https://cinebingers.ca/rQhcCXMa88",
  "source": "cinebingers.ca",
  "archived_at": "2025-05-30T12:34:56.789123+00:00"
}
```

This is then normalized by the existing `normalize_post()` function to:
- Extract YouTube links
- Set proper post metadata
- Store raw JSON

## API Usage

### Start a sync

```bash
curl -X POST http://localhost:8000/api/sync/cinebingers/anime
```

Response:
```json
{
  "creator": "anime",
  "downloaded": false,
  "imported_posts": 0,
  "status": "queued"
}
```

### Check sync status

```bash
curl http://localhost:8000/api/sync-status/anime
```

Response:
```json
{
  "creator": "anime",
  "status": "completed",
  "downloaded_posts": 42,
  "imported_posts": 42,
  "last_message": "Sync complete",
  "imported_posts_db": 42
}
```

### Search imported videos

```bash
curl "http://localhost:8000/api/posts?creator=anime&q=Escape"
```

## Python Usage

### Basic scraping

```python
from app.cinebingers_scraper import scrape_all_cinebingers_videos

videos = scrape_all_cinebingers_videos()
for video in videos:
    print(f"{video.title} -> {video.youtube_url}")
```

### With progress callback

```python
def on_page(page_num, videos_found):
    print(f"Page {page_num}: {videos_found} videos")

videos = scrape_all_cinebingers_videos(on_page_complete=on_page)
```

### Single page

```python
from app.cinebingers_scraper import fetch_cinebingers_page

videos = fetch_cinebingers_page(page=1, sort="NEWEST")
```

### Export to JSON

```python
from app.cinebingers_scraper import export_videos_to_json_files
from pathlib import Path

output_dir = Path("data/raw/my_collection")
exported = export_videos_to_json_files(videos, output_dir)
print(f"Exported {len(exported)} files")
```

## Limitations & Future Improvements

### Current Limitations

1. **No authentication**: The scraper doesn't handle session cookies or authentication. If cinebingers.ca requires login, you'll need to provide session cookies.

2. **Pagination**: Stops when an empty page is found. If there's a maximum page limit, adjust accordingly.

3. **Rate limiting**: No built-in delays between requests. Use responsibly.

4. **No filtering**: Always scrapes everything. Could add genre/category filtering.

### Potential Enhancements

1. **Session management**: Support for session cookies to handle authenticated endpoints
2. **Caching**: Cache video listings to avoid re-scraping known pages
3. **Incremental sync**: Track last sync time and only fetch new videos
4. **Category filtering**: Support genre, anime, series filtering parameters
5. **Custom headers**: Make headers configurable per-request
6. **Error handling**: Better distinction between auth failures, network errors, and empty results
7. **Duplicate detection**: Check if videos already exist before re-importing

## Troubleshooting

### Issue: "No videos found" on first page

**Possible causes:**
- cinebingers.ca endpoint requires authentication
- Page structure has changed
- Network request is being blocked

**Solutions:**
1. Check if https://cinebingers.ca works in your browser
2. Inspect the response HTML to see if structure matches expectations
3. Try fetching manually: `curl https://cinebingers.ca/homepagevideo?page=1&...`

### Issue: "Too many redirects"

**Cause:** Session expired or invalid, redirecting to login page

**Solution:**
1. Extract session cookies from browser DevTools
2. Modify the scraper to include cookies in requests
3. Or implement a login flow

### Issue: Some videos missing titles or URLs

**Cause:** HTML structure varies or data is in different elements

**Solution:**
1. Inspect the raw HTML to find the pattern
2. Update the parsing regex in `_extract_youtube_url_from_onclick()`
3. Add additional fallback selectors

## Testing

Run the test script:

```bash
python test_cinebingers.py data/raw/test_cinebingers 1
```

This will:
1. Scrape the first page
2. Export to `data/raw/test_cinebingers/`
3. Show sample videos

Or use the Python snippet directly:

```python
from app.cinebingers_scraper import fetch_cinebingers_page

videos = fetch_cinebingers_page(page=1)
for video in videos[:3]:
    print(f"✓ {video.title}")
```
