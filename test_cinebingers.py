#!/usr/bin/env python
"""
Quick test script to scrape cinebingers.ca and save videos locally.
Usage: python test_cinebingers.py [output_directory] [num_pages] [--debug]

Note: cinebingers.ca uses Patreon OAuth for authentication.
You may need to pass session cookies from your browser.
See setup_cinebingers_cookies.py for instructions.
"""
import sys
import json
from pathlib import Path
from app.cinebingers_scraper import (
    scrape_all_cinebingers_videos,
    fetch_cinebingers_page,
    export_videos_to_json_files,
    _extract_cookies_from_env,
)

def load_cookies() -> dict | None:
    """Try to load cookies from .env.cinebingers or environment"""
    # First try .env.cinebingers file
    cookies_file = Path(".env.cinebingers")
    if cookies_file.exists():
        try:
            return json.loads(cookies_file.read_text())
        except Exception as e:
            print(f"Warning: Could not load cookies from {cookies_file}: {e}")
    
    # Fall back to environment
    return _extract_cookies_from_env()

def main():
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/raw/cinebingers_test")
    max_pages = int(sys.argv[2]) if len(sys.argv) > 2 else None
    debug = "--debug" in sys.argv
    
    # Try to load cookies
    cookies = load_cookies()
    
    if debug:
        print("DEBUG MODE ENABLED")
        print(f"Using cookies: {list(cookies.keys()) if cookies else 'None'}")
        print("Testing single page fetch with debug output...\n")
        videos = fetch_cinebingers_page(page=1, debug=True, cookies=cookies)
        print(f"\nDebug test complete. Got {len(videos)} videos.\n")
        return
    
    print(f"Scraping cinebingers.ca (max {max_pages} pages)...")
    if cookies:
        print(f"Using cookies: {list(cookies.keys())}")
    else:
        print("Warning: No cookies found. You may need to run: python setup_cinebingers_cookies.py")
        print("(cinebingers.ca uses Patreon OAuth authentication)")
    
    def on_page(page, count):
        print(f"  Page {page}: found {count} videos")
    
    videos = scrape_all_cinebingers_videos(
        max_pages=max_pages,
        on_page_complete=on_page,
        cookies=cookies,
    )
    
    print(f"\nTotal videos found: {len(videos)}")
    
    print(f"Exporting to {output_dir}...")
    files = export_videos_to_json_files(videos, output_dir)
    
    print(f"Exported {len(files)} video files")
    
    # Print first 3 videos as sample
    if videos:
        print("\nSample videos:")
        for video in videos[:3]:
            print(f"  - {video.title}")
            print(f"    YouTube: {video.youtube_url}")

if __name__ == "__main__":
    main()
