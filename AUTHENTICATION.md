# Authentication Troubleshooting

## Issue: "cinebingers.ca is redirecting to Patreon login"

**Root Cause**: cinebingers.ca uses Patreon OAuth for authentication. You need session cookies to access it.

## Why Your Browser Works But The Script Doesn't

When you visit cinebingers.ca in your browser:
1. Your browser has valid session cookies stored
2. These cookies are sent automatically with each request
3. cinebingers.ca verifies you're logged in and shows content

When the script runs:
1. It makes HTTP requests without your browser's cookies
2. cinebingers.ca sees an unauthenticated request
3. It redirects you to Patreon's login page

## Solution: Extract and Configure Cookies

### Quick Fix (2 minutes)

1. Run the cookie setup script:
```bash
python setup_cinebingers_cookies.py
```

2. Follow the prompts to extract cookies from your browser

3. Try scraping again:
```bash
python test_cinebingers.py data/raw/cinema 1
```

### Manual Setup

If the script doesn't work, do it manually:

**Step 1: Get cookies from your browser**
1. Open https://cinebingers.ca (logged in)
2. Press F12 to open DevTools
3. Go to `Application` tab
4. Click `Cookies` on the left
5. Click `https://cinebingers.ca`
6. Look for these cookies:
   - `XSRF-TOKEN` - Copy the entire value
   - `Cinebinge_user_live` - Copy the entire value
   - Any other session cookies

**Step 2: Create the cookies file**

Create `data/cinebingers_cookies.json` (the easiest way is `python setup_cinebingers_cookies.py`, which writes it for you):

```json
{
  "XSRF-TOKEN": "paste-the-token-value-here",
  "Cinebinge_user_live": "paste-the-value-here"
}
```

The app loads this automatically. It lives under `data/`, so it's already
gitignored and rides the Docker volume mount onto the VPS.

**Step 3: Test it**
```bash
python test_cinebingers.py data/raw/cinema 1
```

### Debug Mode

To see exactly what's happening:

```bash
python test_cinebingers.py data/raw/cinema 1 --debug
```

This will:
- Show the HTTP response status
- Show the actual redirect URL
- Save the HTML response to `debug_response.html`
- Give you clues about what's wrong

## Understanding the Debug Output

When you run with `--debug`, you'll see:

```
[DEBUG] Status: 200
[DEBUG] URL: https://www.patreon.com/login?ru=https%3A%2F%2F...
```

This means:
- **Status: 200** - The request succeeded, but...
- **URL: patreon.com/login** - You were redirected to Patreon login

This is what happens when your cookies are missing or expired.

## Cookie Expiration

Session cookies expire, so if scraping was working before but suddenly fails:

1. Your cookies may have expired
2. Run the setup script again to get fresh cookies
3. Or manually extract new cookies from your browser

## Security Notes

⚠️ **IMPORTANT:**

- **Never** share your cookies with anyone
- **Never** commit `data/cinebingers_cookies.json` to Git
- Cookies contain your authentication tokens
- Treat them like passwords

It's covered by the existing `data/` entry in `.gitignore`.

## API Usage

Once cookies are configured, you can use the API:

```python
# Python
import json
from app.config import settings
from app.sync_service import scrape_cinebingers

cookies = json.loads(settings.cinebingers_cookies_file.read_text())
scrape_cinebingers("anime", cookies=cookies)

# Or via FastAPI endpoint (need to configure cookies in app first)
curl -X POST http://localhost:8000/api/sync/cinebingers/anime
```

## Alternatives

If you don't want to use cookies:

1. **Use the CLI manually** - Extract videos manually and add them
2. **Use a different approach** - Write a separate integration
3. **Check if there's a public API** - cinebingers.ca might have an API

## Still Having Issues?

1. Make sure you're actually logged in at https://cinebingers.ca
2. Check if the site has changed its authentication method
3. Try with the `--debug` flag to see what's happening
4. Check if your IP is blocked (some sites block scrapers)

## Technical Details

cinebingers.ca uses:
- Patreon OAuth 2.0 for authentication
- Client ID: `yGkPUdry3UUQjPQ3AOB8_BPVA4CcYVnlxL7iSYP0fw0_CFbimkkUjPFSYFu4RXiA`
- Redirect URI: `https://cinebingers.ca/oauth/callback`
- Scope: `identity identity[email]`

This is why cookies from Patreon are needed - the site delegates authentication to Patreon.
