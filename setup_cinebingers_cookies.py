#!/usr/bin/env python
"""
Helper script to set up cinebingers.ca cookies for scraping.

cinebingers.ca uses Patreon OAuth for authentication, so you need session cookies
to scrape it. This script helps you extract and configure them.

IMPORTANT: Never share your cookies with anyone!
"""
import json
from pathlib import Path

def main():
    print("=" * 70)
    print("Cinebingers.ca Cookie Setup")
    print("=" * 70)
    
    print("\ncinebingers.ca uses Patreon OAuth for authentication.")
    print("You need to extract session cookies from your browser to scrape it.\n")
    
    print("STEP 1: Get cookies from your browser")
    print("-" * 70)
    print("""
1. Open https://cinebingers.ca in your browser (make sure you're logged in)
2. Open DevTools (F12 or Ctrl+Shift+I)
3. Go to Application tab > Cookies > https://cinebingers.ca
4. Look for these cookies and copy their VALUES:
   
   Important cookies to copy:
   - XSRF-TOKEN (usually a JWT)
   - Cinebinge_user_live (usually a JWT)
   - PHPSESSID (if present)
   - Any other session cookies
   
   You can copy all cookies by right-clicking and "Copy all as cURL"
   or manually copy each value.
""")
    
    print("\nSTEP 2: Enter your cookies")
    print("-" * 70)
    print("\nEnter cookie values (press Enter twice to finish):\n")
    
    cookies = {}
    while True:
        cookie_name = input("Cookie name (or press Enter to finish): ").strip()
        if not cookie_name:
            break
        
        cookie_value = input(f"Value for {cookie_name}: ").strip()
        if cookie_value:
            cookies[cookie_name] = cookie_value
            print(f"  Added: {cookie_name}")
    
    if not cookies:
        print("\nNo cookies entered. Exiting.")
        return
    
    print("\n" * 1)
    print("STEP 3: Save cookies")
    print("-" * 70)
    
    # Save as JSON to the configured path (data/cinebingers_cookies.json),
    # which is mounted into the container and gitignored.
    from app.config import settings
    cookies_file = settings.cinebingers_cookies_file
    cookies_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cookies_file, 'w') as f:
        json.dump(cookies, f, indent=2)

    print(f"\nCookies saved to: {cookies_file}")
    
    # Also show Python usage
    print("\nSTEP 4: Use the cookies")
    print("-" * 70)
    print("""
You can now use the cookies in two ways:

Option A: Trigger a sync from the web UI
    The app loads data/cinebingers_cookies.json automatically.

Option B: Use in Python code
    import json
    from app.config import settings
    from app.cinebingers_scraper import scrape_all_cinebingers_videos

    cookies = json.loads(settings.cinebingers_cookies_file.read_text())
    videos = scrape_all_cinebingers_videos(cookies=cookies)

Option C: Use in sync API
    import json
    from app.config import settings
    from app.sync_service import scrape_cinebingers

    cookies = json.loads(settings.cinebingers_cookies_file.read_text())
    scrape_cinebingers("anime", cookies=cookies)
""")

    print("\nWARNING:")
    print("  - Never commit your cookies file to git!")
    print("  - Session cookies expire, you may need to refresh them")
    print("  - Keep your cookies private!")
    
    print("\n" + "=" * 70)
    print("Setup complete!")
    print("=" * 70)

if __name__ == "__main__":
    main()
