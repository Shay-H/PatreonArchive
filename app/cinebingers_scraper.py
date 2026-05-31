"""
Scraper for cinebingers.ca - a website that curates YouTube videos
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from app.errors import AuthError


def _extract_cookies_from_env() -> dict[str, str] | None:
    """
    Try to extract Patreon cookies from environment.
    This may not work as PATREON_COOKIE is typically session-based.
    Users should extract cookies from their browser.
    """
    try:
        import os
        from app.config import settings
        
        if hasattr(settings, 'patreon_cookie') and settings.patreon_cookie:
            # Note: PATREON_COOKIE is usually a session cookie, not the auth cookie we need
            # But we can try using it as a cookie value
            return {"Patreon-Cookie": settings.patreon_cookie}
    except Exception:
        pass
    return None


@dataclass
class CinebingersVideo:
    """Represents a video from cinebingers.ca"""
    title: str
    youtube_url: str
    video_id: str  # from cinebingers.ca
    cinebingers_url: str


def _extract_video_id_from_url(url: str) -> str:
    """Extract the video ID from cinebingers.ca URL"""
    match = re.search(r'cinebingers\.ca/([A-Za-z0-9_-]+)$', url)
    if match:
        return match.group(1)
    return ""


def _extract_youtube_url_from_onclick(onclick: str) -> str | None:
    """
    Extract YouTube URL from onClick event handler.
    Example: onclick=" return copy('https://www.youtube.com/watch?v=UIeFmp-KDIU')"
    """
    match = re.search(r"copy\('(https://www\.youtube\.com/watch\?v=[^']+)'\)", onclick)
    if match:
        return match.group(1)
    return None


def parse_cinebingers_html(html: str, base_url: str = "https://cinebingers.ca") -> list[CinebingersVideo]:
    """
    Parse the HTML response from cinebingers.ca and extract video information.
    
    Args:
        html: The HTML content from the homepagevideo endpoint
        base_url: The base URL for constructing full URLs
    
    Returns:
        List of CinebingersVideo objects
    """
    soup = BeautifulSoup(html, "html.parser")
    videos: list[CinebingersVideo] = []
    
    # Find all video cards - they're in divs with class "col-6 col-sm-6 col-md-4 col-lg-3"
    # containing a card with title and YouTube link
    for card_wrapper in soup.find_all("div", class_="col-6"):
        card = card_wrapper.find("div", class_="card")
        if not card:
            continue
        
        # Extract title from h6 with class "card-title mb-1"
        title_elem = card.find("h6", class_="card-title")
        if not title_elem or not title_elem.get_text(strip=True):
            continue
        title = title_elem.get_text(strip=True)
        
        # Extract cinebingers.ca link
        link_elem = card.find("a", href=re.compile(r"cinebingers\.ca/"))
        if not link_elem or not link_elem.get("href"):
            continue
        cinebingers_url = link_elem["href"]
        if not cinebingers_url.startswith("http"):
            cinebingers_url = f"{base_url}{cinebingers_url}"
        
        video_id = _extract_video_id_from_url(cinebingers_url)
        
        # Extract YouTube URL from onClick handler
        # The span with copy button has onClick=" return copy('...')"
        youtube_url = None
        for span in card.find_all("span"):
            onclick = span.get("onclick") or ""
            if "copy" in onclick:
                youtube_url = _extract_youtube_url_from_onclick(onclick)
                if youtube_url:
                    break
        
        if youtube_url and title and video_id:
            videos.append(CinebingersVideo(
                title=title,
                youtube_url=youtube_url,
                video_id=video_id,
                cinebingers_url=cinebingers_url,
            ))
    
    return videos


def fetch_cinebingers_page(
    page: int = 1,
    sort: str = "NEWEST",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    debug: bool = False,
) -> list[CinebingersVideo]:
    """
    Fetch a page of videos from cinebingers.ca.
    
    Args:
        page: Page number (1-indexed)
        sort: Sort order (default: NEWEST)
        headers: Custom headers to use for the request
        cookies: Optional cookies dict (may include session or Patreon auth)
        debug: If True, save response to file and print debug info
    
    Returns:
        List of CinebingersVideo objects from this page
    
    Note:
        If cinebingers.ca is redirecting to Patreon login, you need to pass
        session cookies. Extract them from your browser's DevTools (Application > Cookies).
        Look for: XSRF-TOKEN, Cinebinge_user_live, PHPSESSID, or other session cookies.
    """
    if headers is None:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
            "Accept": "*/*",
            "Accept-Language": "en-GB,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-GPC": "1",
            "Connection": "keep-alive",
            "Referer": f"https://cinebingers.ca/?sort={sort}&version=Sync+with+your+copy&thumbnail=no",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=0",
        }
    
    url = (
        f"https://cinebingers.ca/homepagevideo"
        f"?page={page}"
        f"&genre_id=&series_id=&anime_id=&category_id="
        f"&sort={sort}"
        f"&search=&version=Sync%20with%20your%20copy&thumbnail=no"
    )
    
    response = requests.get(url, headers=headers, cookies=cookies, timeout=10)
    if response.status_code in (401, 403):
        raise AuthError(
            f"cinebingers.ca returned HTTP {response.status_code} — session cookies "
            "are missing or expired. Refresh data/cinebingers_cookies.json."
        )
    response.raise_for_status()
    
    if debug:
        print(f"[DEBUG] Status: {response.status_code}")
        print(f"[DEBUG] URL: {response.url}")
        print(f"[DEBUG] Content length: {len(response.text)}")
        print(f"[DEBUG] First 500 chars: {response.text[:500]}")
        # Save response for inspection (with proper encoding)
        debug_file = Path("debug_response.html")
        try:
            debug_file.write_text(response.text, encoding='utf-8')
            print(f"[DEBUG] Full response saved to: {debug_file}")
        except Exception as e:
            print(f"[DEBUG] Could not save response: {e}")
    
    # Check if we got redirected to login (indicating the session is invalid)
    if "patreon.com/login" in response.text.lower() or "authenticate" in response.text.lower():
        error_msg = (
            "cinebingers.ca is redirecting to Patreon login. Your session may have expired.\n"
            "\n"
            "To fix this:\n"
            "1. Open https://cinebingers.ca in your browser\n"
            "2. Make sure you're logged in\n"
            "3. Open DevTools (F12) > Application tab > Cookies\n"
            "4. Copy the cookie values for: XSRF-TOKEN, Cinebinge_user_live, PHPSESSID, etc.\n"
            "5. Pass them when scraping:\n"
            "   cookies = {'XSRF-TOKEN': 'value...', 'Cinebinge_user_live': 'value...', ...}\n"
            "   scrape_all_cinebingers_videos(cookies=cookies)\n"
            "\n"
            "Or enable debug mode to see the full response:\n"
            "   fetch_cinebingers_page(page=1, debug=True)\n"
        )
        raise AuthError(error_msg)
    
    return parse_cinebingers_html(response.text)


def scrape_all_cinebingers_videos(
    sort: str = "NEWEST",
    max_pages: int | None = None,
    on_page_complete: callable | None = None,
    cookies: dict[str, str] | None = None,
    request_delay: float = 1.0,
    known_ids: set[str] | None = None,
) -> list[CinebingersVideo]:
    """
    Scrape all videos from cinebingers.ca by iterating through pages.

    Args:
        sort: Sort order
        max_pages: Maximum number of pages to scrape (None = all)
        on_page_complete: Optional callback called after each page with (page_num, videos_found)
        cookies: Optional cookies dict (may include Patreon auth if needed)
        request_delay: Delay in seconds between requests (default: 1.0)
        known_ids: Optional set of already-imported video IDs. With sort=NEWEST,
            once an entire page consists only of known videos there's nothing
            newer left to find, so pagination stops early.

    Returns:
        List of all CinebingersVideo objects found
    """
    all_videos: list[CinebingersVideo] = []
    page = 1

    while True:
        if max_pages is not None and page > max_pages:
            break

        try:
            videos = fetch_cinebingers_page(page=page, sort=sort, cookies=cookies)
            if not videos:
                # Empty page means we've reached the end
                break

            all_videos.extend(videos)
            if on_page_complete:
                on_page_complete(page, len(videos))

            # Early stop: if every video on this page is already imported, there
            # are no newer videos to fetch on later pages.
            if known_ids is not None and all(v.video_id in known_ids for v in videos):
                break

            # Add delay before next request (rate limiting)
            if max_pages is None or page < max_pages:
                time.sleep(request_delay)

            page += 1
        except AuthError:
            raise  # preserve auth failures for the scheduler / UI
        except Exception as e:
            raise RuntimeError(f"Failed to fetch page {page}: {e}") from e

    return all_videos


def export_videos_to_json_files(
    videos: list[CinebingersVideo],
    output_dir: Path,
) -> dict[str, Path]:
    """
    Export videos to JSON files in the output directory.
    Creates one JSON file per video (similar to patreon-dl post_info format).
    
    Args:
        videos: List of videos to export
        output_dir: Directory to write the JSON files
    
    Returns:
        Dictionary mapping video IDs to their file paths
    """
    import json
    
    output_dir.mkdir(parents=True, exist_ok=True)
    file_paths: dict[str, Path] = {}
    
    for i, video in enumerate(videos):
        # Create a post_info file for each video (similar to patreon-dl format)
        filename = f"post_info_{video.video_id}.json"
        filepath = output_dir / filename
        
        post_data = {
            "id": video.video_id,
            "title": video.title,
            "url": video.youtube_url,
            "postUrl": video.cinebingers_url,
            "source": "cinebingers.ca",
            "archived_at": datetime.now(timezone.utc).isoformat(),
        }
        
        filepath.write_text(json.dumps(post_data, indent=2, ensure_ascii=False))
        file_paths[video.video_id] = filepath
    
    return file_paths
