"""Headless Zerodha Kite login, so the daily token needs no human.

Kite has no token API: the access token comes from a browser redirect. That is
fine on a laptop and fatal on a container that must run unattended, because the
token dies around 06:00 IST every day.

This module performs the same flow a browser would, using the credentials the
operator already has:

1. ``POST /api/login`` with the user id and password -> ``request_id``
2. ``POST /api/twofa`` with a TOTP code generated from the stored secret
3. ``GET /connect/login?api_key=...`` and read ``request_token`` off the
   redirect
4. exchange it for an ``access_token`` (:func:`kite_auth.exchange_request_token`)

Because the bot can mint a token at any time, the token does not need to
survive a restart or a redeploy: a fresh one is obtained on startup. That is
what makes a stateless container viable.

Security notes:

* The password and TOTP secret are read from the environment and never logged.
  Errors are reported by *state*, not by echoing the request.
* TOTP is a shared secret equivalent to the second factor. Treat
  ``KITE_TOTP_SECRET`` with the same care as the password, and prefer a
  dedicated Kite account over a primary one.
* Zerodha requires TOTP-based 2FA for this to work. An account using the Kite
  mobile app's push approval cannot be logged in headlessly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from config import settings

log = logging.getLogger("kite_autologin")

LOGIN_URL = "https://kite.zerodha.com/api/login"
TWOFA_URL = "https://kite.zerodha.com/api/twofa"
CONNECT_LOGIN_URL = "https://kite.zerodha.com/connect/login"

# The connect login redirects through a couple of hops before landing on the
# registered Redirect URL. Enough to reach it, few enough that a redirect loop
# cannot hang the startup path.
MAX_REDIRECTS = 6
TIMEOUT_SECONDS = 20.0


class AutoLoginError(RuntimeError):
    """Raised when the headless login cannot be completed."""


class CaptchaRequired(AutoLoginError):
    """Zerodha demanded a CAPTCHA, which cannot be solved headlessly.

    This is not a bug to work around. Zerodha gates ``/api/login`` behind a
    CAPTCHA for traffic it considers automated - notably datacenter IPs, which
    is what a CI runner or a cloud container is. When this is raised the account
    cannot be logged in without a human, and the caller should say so plainly
    rather than retrying.
    """


def configured() -> bool:
    """Whether enough credentials are present to attempt a headless login."""
    return bool(
        settings.kite_user_id and settings.kite_password and settings.kite_totp_secret
    )


def _totp_now() -> str:
    """The current 6-digit code for the stored secret."""
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise AutoLoginError("pyotp is not installed; cannot generate a TOTP code") from exc

    secret = settings.kite_totp_secret.strip().replace(" ", "")
    try:
        return pyotp.TOTP(secret).now()
    except Exception as exc:
        raise AutoLoginError(f"KITE_TOTP_SECRET is not a valid TOTP secret: {exc}") from exc


def _json_or_error(response: Any, step: str) -> dict:
    """Parse a Zerodha JSON body, turning a rejection into a clear error.

    The body is inspected for ``status``/``message`` rather than dumped, so a
    failure cannot put a credential into the log.
    """
    try:
        payload = response.json()
    except ValueError as exc:
        raise AutoLoginError(
            f"{step} returned a non-JSON response (HTTP {response.status_code})"
        ) from exc

    if not isinstance(payload, dict):
        raise AutoLoginError(f"{step} returned an unexpected payload")
    if payload.get("status") == "error":
        message = payload.get("message") or payload.get("error_type") or "rejected"
        # Zerodha answers a bot-flagged login with a CAPTCHA challenge instead of
        # a credential error. Retrying cannot help: the challenge is the point.
        if (payload.get("data") or {}).get("captcha") or "captcha" in message.lower():
            raise CaptchaRequired(
                f"{step} requires a CAPTCHA ({message}). Zerodha gates the login "
                "API for traffic it treats as automated, which includes datacenter "
                "IPs. Headless login cannot proceed from this host."
            )
        raise AutoLoginError(f"{step} failed: {message}")
    return payload


def _request_token_from(client: Any, url: str) -> str:
    """Follow the connect-login redirects until ``request_token`` appears.

    Redirects are followed by hand rather than with ``follow_redirects=True``:
    the final hop is the bot's own ``/zerodha/callback``, and letting the client
    fetch it would make the running app exchange the token a second time.
    """
    current = url
    for _ in range(MAX_REDIRECTS):
        response = client.get(current, follow_redirects=False)
        location = response.headers.get("location") or ""
        if not location:
            raise AutoLoginError(
                f"connect login stopped at HTTP {response.status_code} without a redirect"
            )
        token = parse_qs(urlparse(location).query).get("request_token")
        if token:
            return token[0]
        current = location
    raise AutoLoginError("connect login redirected too many times without a request_token")


def login() -> dict[str, Any]:
    """Log in headlessly and install the resulting access token.

    Returns the same payload as :func:`kite_auth.exchange_request_token`, so a
    caller does not need to know which path produced the token.
    """
    if not configured():
        raise AutoLoginError(
            "headless login is not configured: set KITE_USER_ID, KITE_PASSWORD "
            "and KITE_TOTP_SECRET"
        )
    if not settings.api_key or not settings.api_secret:
        raise AutoLoginError("KITE_API_KEY and KITE_API_SECRET are both required")

    import httpx

    from kite_auth import exchange_request_token, login_url

    user_id = settings.kite_user_id.strip()

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        # 1. Password. Zerodha answers with a request_id that the 2FA step needs.
        response = client.post(
            LOGIN_URL, data={"user_id": user_id, "password": settings.kite_password}
        )
        payload = _json_or_error(response, "password login")
        request_id = str((payload.get("data") or {}).get("request_id") or "")
        if not request_id:
            raise AutoLoginError("password login returned no request_id")

        # 2. TOTP. The session cookie set here is what authorises step 3.
        response = client.post(
            TWOFA_URL,
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": _totp_now(),
            },
        )
        _json_or_error(response, "TOTP verification")

        # 3. The connect login, which redirects to the registered Redirect URL
        #    with the request_token attached.
        request_token = _request_token_from(client, login_url())

    # 4. Exchange and install. exchange_request_token also writes .env and
    #    rebuilds the ticker, so the feed picks the new token up immediately.
    result = exchange_request_token(request_token)
    log.info("Headless Kite login succeeded for %s", result.get("user_id") or user_id)
    return result


def ensure_token() -> Optional[dict[str, Any]]:
    """Log in only if the current token is missing or rejected.

    Returns the login payload when a login happened, ``None`` when the existing
    token was already good or headless login is not configured. Never raises:
    a failed refresh must not take down a process that is otherwise serving.
    """
    if not configured():
        return None

    from kite_auth import token_status

    status = token_status()
    if status.get("valid"):
        return None
    if status.get("state") not in {"no_token", "expired", "error"}:
        # sdk_missing / no_credentials are configuration faults a login cannot
        # fix, so retrying would only add noise.
        log.warning("Skipping headless login: %s", status.get("detail"))
        return None

    try:
        return login()
    except CaptchaRequired as exc:
        # Retrying is pointless and would look like a credential-stuffing
        # attempt, so this is logged once and then left alone.
        log.error("Headless Kite login blocked: %s", exc)
        return None
    except AutoLoginError as exc:
        log.error("Headless Kite login failed: %s", exc)
        return None
    except Exception as exc:  # pragma: no cover - network
        log.error("Headless Kite login failed unexpectedly: %s", exc)
        return None


__all__ = ["AutoLoginError", "CaptchaRequired", "configured", "ensure_token", "login"]
