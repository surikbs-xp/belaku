"""Programmatic Zerodha Kite login.

Kite has no pure username/password API - the token flow is a browser redirect:

1. send the user to ``login_url()`` (or let the dashboard do it);
2. they authenticate; Zerodha redirects to the app's registered Redirect URL
   with ``?request_token=...`` in the query string;
3. exchange that ``request_token`` for an ``access_token``
   (:meth:`KiteConnect.generate_session`);
4. use the access token until it expires.

Two operational realities this module handles:

* **The token expires daily**, around 06:00 IST. A stale token is not an error
  state to crash on; it is the normal morning condition, and the bot must keep
  running on cached candles until it is refreshed.
* **The redirect lands on a page the bot is not serving** unless the Redirect
  URL is pointed at the bot itself. :data:`CALLBACK_PATH` exists so it can be:
  register ``http://127.0.0.1:9000/zerodha/callback`` in the Kite developer
  console and the bot captures the ``request_token`` automatically, no manual
  copy-paste.

The token is written back to ``.env`` so it survives a restart.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import BASE_DIR, settings

log = logging.getLogger("kite_auth")

ENV_PATH = BASE_DIR / ".env"
CALLBACK_PATH = "/zerodha/callback"
LEGACY_CALLBACK_PATH = "/kite/callback"
TOKEN_ENV_KEY = "KITE_ACCESS_TOKEN"

# Kite tokens are issued around 06:00 IST and are valid for the trading day.
TOKEN_REFRESH_HOUR_IST = 6

_lock = threading.RLock()


class KiteAuthError(RuntimeError):
    """Raised when the login exchange cannot be completed."""


# ---------------------------------------------------------------------------
# Login URL
# ---------------------------------------------------------------------------
def login_url() -> str:
    """The URL the user must open to authenticate.

    Uses ``KiteConnect.login_url()`` so the version parameter stays in step
    with the SDK.
    """
    from kite_client import KiteConnect, SDK_AVAILABLE

    if not SDK_AVAILABLE:  # pragma: no cover - env without the SDK
        raise KiteAuthError("kiteconnect SDK is not installed")
    if not settings.api_key:
        raise KiteAuthError("KITE_API_KEY is not set")
    return KiteConnect(api_key=settings.api_key).login_url()


def callback_url() -> str:
    """Where Kite should send the user back.

    Prefers the bot's own callback so the request_token is captured
    automatically; falls back to whatever is registered.
    """
    base = settings.kite_redirect_url or f"http://{settings.webhook_host}:{settings.webhook_port}"
    base = base.rstrip("/")
    if base.endswith((CALLBACK_PATH, LEGACY_CALLBACK_PATH)):
        return base
    return base + CALLBACK_PATH


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------
def exchange_request_token(request_token: str) -> dict[str, Any]:
    """Swap a ``request_token`` for an access token and persist it.

    Returns the session payload plus ``persisted`` / ``env_path`` so the caller
    can report whether the write succeeded.
    """
    token = (request_token or "").strip()
    if not token:
        raise KiteAuthError("request_token is empty")
    if not settings.api_key or not settings.api_secret:
        raise KiteAuthError("KITE_API_KEY and KITE_API_SECRET are both required")

    from kite_client import KiteConnect, SDK_AVAILABLE

    if not SDK_AVAILABLE:  # pragma: no cover
        raise KiteAuthError("kiteconnect SDK is not installed")

    kite = KiteConnect(api_key=settings.api_key)
    try:
        session = kite.generate_session(token, api_secret=settings.api_secret)
    except Exception as exc:
        raise KiteAuthError(f"token exchange failed: {exc}") from exc

    access_token = str(session.get("access_token") or "").strip()
    if not access_token:
        raise KiteAuthError("Kite returned no access_token")

    with _lock:
        persisted = write_token(access_token)
        apply_token(access_token)

    return {
        "ok": True,
        "access_token": access_token,
        "user_id": session.get("user_id"),
        "user_name": session.get("user_name"),
        "login_time": str(session.get("login_time") or ""),
        "persisted": persisted,
        "env_path": str(ENV_PATH) if persisted else None,
    }


def write_token(access_token: str) -> bool:
    """Persist ``KITE_ACCESS_TOKEN`` into .env, preserving other entries.

    Rewrites in place rather than appending so repeated logins do not pile up
    duplicate keys. Returns False when .env is not writable - the in-memory
    token still works for this process.
    """
    try:
        if ENV_PATH.exists():
            text = ENV_PATH.read_text(encoding="utf-8")
        else:
            example = BASE_DIR / ".env.example"
            text = example.read_text(encoding="utf-8") if example.exists() else ""
    except OSError as exc:
        log.warning("Cannot read .env (%s); token kept in memory only", exc)
        return False

    line = f"{TOKEN_ENV_KEY}={access_token}"
    pattern = re.compile(rf"^{TOKEN_ENV_KEY}=.*$", re.MULTILINE)
    text = pattern.sub(line, text) if pattern.search(text) else text.rstrip("\n") + f"\n{line}\n"

    try:
        ENV_PATH.write_text(text, encoding="utf-8")
        try:
            ENV_PATH.chmod(0o600)
        except OSError:
            pass
        log.info("Access token written to %s", ENV_PATH)
        return True
    except OSError as exc:
        log.warning("Cannot write .env (%s); token kept in memory only", exc)
        return False


def apply_token(access_token: str) -> None:
    """Push a fresh token into the running client and reconnect the feed.

    The GIFT Nifty websocket is built from the token at connect time, so a new
    token requires the ticker to be rebuilt, not just the REST client.
    """
    import os

    os.environ["KITE_ACCESS_TOKEN"] = access_token

    from kite_client import get_client

    client = get_client()
    client.access_token = access_token
    client._init_sdk()  # rebuild KiteConnect with the new token

    # The tick feed cannot be re-authenticated in place: tear it down and let
    # the engine pick it up again.
    try:
        from gift_nifty import get_feed, reset_feed

        reset_feed()
        log.info("GIFT Nifty feed reset for the new token")
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not reset the GIFT feed: %s", exc)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
def token_status() -> dict[str, Any]:
    """Whether the current token works, without raising."""
    from kite_client import SDK_AVAILABLE, get_client

    client = get_client()
    if not SDK_AVAILABLE:
        return {"state": "sdk_missing", "valid": False, "detail": "kiteconnect not installed"}
    if not settings.api_key or not settings.api_secret:
        return {"state": "no_credentials", "valid": False,
                "detail": "KITE_API_KEY / KITE_API_SECRET not set"}
    if not client.access_token:
        return {"state": "no_token", "valid": False,
                "detail": "No access token. Run the Kite login flow.",
                "login_url": _safe_login_url()}

    profile = client.profile()
    if profile.get("ok"):
        return {
            "state": "valid",
            "valid": True,
            "user_id": profile.get("user_id"),
            "user_name": profile.get("user_name"),
            "detail": f"Authenticated as {profile.get('user_id')}",
        }

    detail = profile.get("error") or "token rejected"
    expired = "expired" in detail.lower() or "invalid" in detail.lower()
    return {
        "state": "expired" if expired else "error",
        "valid": False,
        "detail": detail,
        "login_url": _safe_login_url(),
    }


def _safe_login_url() -> Optional[str]:
    try:
        return login_url()
    except KiteAuthError:
        return None


def token_age_note() -> str:
    """Human hint about the daily expiry, for the dashboard."""
    now = datetime.now()
    if now.hour < TOKEN_REFRESH_HOUR_IST:
        return "Tokens expire around 06:00 IST - refresh after that."
    return "Kite tokens expire around 06:00 IST tomorrow."


__all__ = [
    "CALLBACK_PATH",
    "KiteAuthError",
    "apply_token",
    "callback_url",
    "exchange_request_token",
    "login_url",
    "token_age_note",
    "token_status",
    "write_token",
]
