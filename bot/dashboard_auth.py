"""Authentication for the dashboard and its control endpoints.

The bot places real orders, so ``/api/panic``, ``/api/toggle-mode`` and friends
must not be reachable by anything that can open a socket to the port. Binding to
loopback is not enough: any local process, any SSH user, and any browser page
that can reach ``127.0.0.1`` would otherwise be able to arm LIVE mode.

Design, and why:

* **A shared password, not user accounts.** There is one operator. Accounts,
  password resets and roles would be ceremony without a second person to
  distinguish.
* **A signed cookie, not HTTP Basic.** Basic sends the password on every
  request, so it lands in logs and shell history, and it cannot be logged out of.
  The cookie carries an HMAC-signed expiry instead, and the password itself is
  never stored client-side.
* **``SameSite=Strict`` plus the existing Origin check.** The cookie is not
  attached to cross-site requests at all, which closes CSRF without a token
  round-trip.
* **Constant-time comparison.** ``hmac.compare_digest`` so a wrong password
  cannot be recovered byte by byte from response timing.
* **A per-process signing key by default.** Restarting the bot invalidates
  sessions, which is the safe default. Set ``DASHBOARD_SESSION_SECRET`` to keep
  sessions across restarts.

When ``DASHBOARD_PASSWORD`` is unset the dashboard is *open*, which preserves
the previous behaviour for local development. :func:`auth_required` reports that
so the UI can warn instead of silently pretending to be protected.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from typing import Optional

log = logging.getLogger("dashboard_auth")

COOKIE_NAME = "belaku_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # one trading day

# Failed logins are rate limited per client address. Without this the password
# is brute-forceable at whatever rate the network allows.
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW_SECONDS = 300.0

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()

# A random key per process unless the operator pins one. Sessions then do not
# survive a restart, which is the safer default for a bot holding broker
# credentials.
_SESSION_SECRET = os.getenv("DASHBOARD_SESSION_SECRET", "").strip() or secrets.token_hex(32)


def password() -> str:
    """The configured dashboard password, or "" when auth is disabled."""
    return os.getenv("DASHBOARD_PASSWORD", "").strip()


def auth_required() -> bool:
    """Whether a password is configured. False means the dashboard is open."""
    return bool(password())


def _sign(payload: str) -> str:
    return hmac.new(_SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(now: Optional[float] = None) -> str:
    """A signed ``<expiry>.<nonce>.<signature>`` session token."""
    expiry = int((now if now is not None else time.time()) + SESSION_TTL_SECONDS)
    payload = f"{expiry}.{secrets.token_hex(8)}"
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str, now: Optional[float] = None) -> bool:
    """Whether ``token`` is authentic and unexpired."""
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    expiry_raw, nonce, signature = parts
    payload = f"{expiry_raw}.{nonce}"
    if not hmac.compare_digest(signature, _sign(payload)):
        return False
    try:
        expiry = int(expiry_raw)
    except ValueError:
        return False
    return expiry > int(now if now is not None else time.time())


def check_password(candidate: str) -> bool:
    """Constant-time password check. False when no password is configured."""
    expected = password()
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def _prune(now: float) -> None:
    for key in list(_attempts):
        kept = [t for t in _attempts[key] if now - t < ATTEMPT_WINDOW_SECONDS]
        if kept:
            _attempts[key] = kept
        else:
            del _attempts[key]


def rate_limited(client: str, now: Optional[float] = None) -> bool:
    """Whether ``client`` has exhausted its login attempts."""
    moment = now if now is not None else time.time()
    with _attempts_lock:
        _prune(moment)
        return len(_attempts.get(client, ())) >= MAX_ATTEMPTS


def record_failure(client: str, now: Optional[float] = None) -> None:
    moment = now if now is not None else time.time()
    with _attempts_lock:
        _attempts.setdefault(client, []).append(moment)


def clear_failures(client: str) -> None:
    with _attempts_lock:
        _attempts.pop(client, None)


def reset_state() -> None:
    """Drop rate-limit state. For tests."""
    with _attempts_lock:
        _attempts.clear()
