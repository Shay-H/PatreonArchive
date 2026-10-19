import secrets
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    app_name: str = "Patreon Archive"
    database_url: str = "sqlite:///./data/patreon_archive.db"
    data_root: Path = Path("data")
    raw_root: Path = Path("data/raw")

    # Cookie copied from your logged-in Patreon session.
    # Keep it private. Never commit to source control.
    patreon_cookie: str = ""

    # Command used to invoke patreon-dl CLI.
    # Examples: "patreon-dl" or "npx patreon-dl"
    patreon_dl_command: str = "patreon-dl"

    # Optional path to a patreon-dl config file.
    patreon_dl_config_file: str | None = None

    # When enabled, use a generated metadata-only config file so the app imports
    # post/campaign info without downloading media files.
    use_metadata_only_config: bool = True

    # Optional TMDB API key for genre tagging.
    # Get a free key at https://www.themoviedb.org/settings/api
    tmdb_api_key: str = ""

    # JSON file of cinebingers.ca session cookies ({"COOKIE": "value", ...}).
    # Lives under data/ so it rides the existing volume mount and is gitignored.
    cinebingers_cookies_file: Path = Path("data/cinebingers_cookies.json")

    # After a successful sync, delete downloaded image media under data/raw.
    # patreon-dl's status cache keeps syncs incremental regardless, so this only
    # reclaims space — it never causes re-downloads. See app/cleanup.py.
    prune_raw_media_after_sync: bool = True

    # Auto-refetch scheduler (app/scheduler.py). Re-syncs every known creator
    # and tags new posts. Pauses itself on an auth failure.
    auto_sync_enabled: bool = True
    auto_sync_hour: int = 3  # hour of day for daily mode (ignored when interval_hours > 0)
    # Set AUTO_SYNC_INTERVAL_HOURS=3 to run every 3 hours instead of once daily.
    auto_sync_interval_hours: int = 0  # 0 = daily-at-hour mode

    # Creator name routed to the cinebingers scraper instead of patreon-dl.
    cinebingers_creator_name: str = "cinebingers"

    # When true, skip patreon-dl fetch entirely (import-only). Set on the VPS
    # while Cloudflare blocks datacenter IPs; the FlareSolverr scraper feeds
    # posts in via its own path.
    patreon_fetch_disabled: bool = False

    # --- Login / sessions -------------------------------------------------
    # Signing key for the session cookie. Leave blank to auto-generate and
    # persist one under data/ (see get_session_secret) so it survives restarts.
    session_secret: str = ""
    # Set SESSION_COOKIE_SECURE=true in production (behind nginx TLS) so the
    # cookie is only sent over HTTPS. Left False so local HTTP dev works.
    session_cookie_secure: bool = False
    # How long a login lasts, in seconds (default 30 days).
    session_max_age: int = 60 * 60 * 24 * 30


settings = Settings()
settings.data_root.mkdir(parents=True, exist_ok=True)
settings.raw_root.mkdir(parents=True, exist_ok=True)


def get_session_secret() -> str:
    """Return the session signing key.

    Prefers an explicit SESSION_SECRET from the environment/.env. Otherwise
    generates a random key once and persists it to data/.session_secret so
    sessions stay valid across restarts and container rebuilds (the data dir is
    a mounted volume).
    """
    if settings.session_secret.strip():
        return settings.session_secret.strip()
    secret_file = settings.data_root / ".session_secret"
    try:
        existing = secret_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        secret_file.write_text(secret, encoding="utf-8")
        secret_file.chmod(0o600)
    except OSError:
        pass
    return secret

METADATA_ONLY_CONFIG_FILE = (settings.data_root / "patreon-dl-metadata-only.conf").resolve()


def ensure_metadata_only_config_file() -> str | None:
    if settings.use_metadata_only_config:
        METADATA_ONLY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        METADATA_ONLY_CONFIG_FILE.write_text(
            "\n".join(
                [
                    "[downloader]",
                    "dry.run = 0",
                    # Skip already-downloaded posts via the status cache so that
                    # pruned media (app/cleanup.py) is never re-fetched.
                    "use.status.cache = 1",
                    "",
                    "[request]",
                    "user.agent = Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
                    "",
                    "[output]",
                    "out.dir = data/raw",
                    "",
                    "[include]",
                    "locked.content = 1",
                    "posts.with.media.type = any",
                    "posts.in.tier = any",
                    "campaign.info = 1",
                    "content.info = 1",
                    "preview.media = 0",
                    "content.media = 0",
                    "protected.media = 0",
                    "all.media.variants = 0",
                    "media.thumbnails = 0",
                    "comments = 0",
                    ""
                ]
            ),
            encoding="utf-8",
        )
        return str(METADATA_ONLY_CONFIG_FILE)

    return None
