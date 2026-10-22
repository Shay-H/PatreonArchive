"""Login/session helpers.

Passwords are hashed with PBKDF2-HMAC-SHA256 (stdlib only — no external crypto
dependency). Sessions are signed cookies carrying just the user id, managed by
Starlette's SessionMiddleware (wired up in app/main.py).
"""
import hashlib
import hmac
import re
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import User

# PBKDF2 parameters. 600k iterations follows current OWASP guidance for
# PBKDF2-HMAC-SHA256.
_ALGO = "pbkdf2_sha256"
_ITERATIONS = 600_000
_SALT_BYTES = 16

# Paths reachable without a session. Everything else is gated by the auth
# middleware in app/main.py. `/api/health` stays public for the Docker
# healthcheck; `/static` and `/login` are needed to render the login page.
PUBLIC_PATHS = ("/login", "/api/login", "/api/logout", "/api/health", "/favicon.ico")
PUBLIC_PREFIXES = ("/static/",)


def hash_password(password: str) -> str:
    """Return a self-describing hash string: algo$iterations$salt_hex$hash_hex."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify of a password against a stored hash string."""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# Paths a FEED_TOKEN bearer may read without a session.
FEED_PATH_RE = re.compile(r"^/api/posts/\d+/links$")


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES)


def is_feed_request(request: Request) -> bool:
    """True for a GET to a feed path carrying the configured FEED_TOKEN."""
    token = settings.feed_token.strip()
    if not token or request.method != "GET" or not FEED_PATH_RE.match(request.url.path):
        return False
    header = request.headers.get("authorization", "")
    return hmac.compare_digest(header.encode(), f"Bearer {token}".encode())


def authenticate(db: Session, username: str, password: str) -> User | None:
    """Return the user if the credentials are valid, else None."""
    user = db.scalar(select(User).where(User.username == username))
    if user and verify_password(password, user.password_hash):
        return user
    return None


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """FastAPI dependency: resolve the logged-in user or raise 401.

    The auth middleware already blocks unauthenticated requests, but endpoints
    that need the actual User object depend on this to load it.
    """
    user_id = request.session.get("user_id")
    if user_id is not None:
        user = db.get(User, user_id)
        if user is not None:
            return user
    raise HTTPException(status_code=401, detail="Not authenticated")
