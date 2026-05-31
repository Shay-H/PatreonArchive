"""Shared exception types."""
from __future__ import annotations


class AuthError(RuntimeError):
    """Raised when a sync fails because credentials are missing/expired.

    Distinguishes auth failures (bad/expired Patreon cookie, cinebingers session
    redirecting to login) from other errors so the scheduler can pause and the UI
    can prompt for a credential refresh.
    """
