#!/usr/bin/env python3
"""Fetch a fresh Kite access token and (optionally) publish it to GitHub.

Kite has no refresh token: the access token comes from a browser redirect and
dies around 06:00 IST every day. This script runs the three steps that are easy
to get wrong by hand:

1. print the login URL (built from the app's ``api_key``, so it cannot drift from
   the registered app);
2. take the ``request_token`` from the redirect, or from the URL you paste back;
3. exchange it for an access token, write it into ``bot/.env``, and verify that
   Kite accepts it before claiming success.

``--github`` then pushes it to the ``KITE_ACCESS_TOKEN`` secret, which is what the
deploy workflow reads. That is the supported path for a cloud deployment: the
headless auto-login cannot work from a datacenter IP because Zerodha CAPTCHAs
``/api/login`` there (see ``bot/kite_autologin.py`` and the Lightsail docs).

Usage:
    # Start here: opens the login, then paste the redirect URL back.
    scripts/kite-token.py

    # Publish the current (valid) token to GitHub without logging in again.
    scripts/kite-token.py --push-secret

    # You already have a request_token (e.g. from the browser's address bar).
    scripts/kite-token.py --request-token abcd1234 --github

Only ``--push-secret`` and ``--request-token`` touch the token; nothing here logs
it. The secret is sent to ``gh`` on stdin so it never appears in a process list.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / "bot" / ".env"
PROFILE_URL = "https://api.kite.trade/user/profile"
SECRET_NAME = "KITE_ACCESS_TOKEN"
CALLBACK_PATH = "/zerodha/callback"
LOCAL_CALLBACK = f"http://127.0.0.1:9000{CALLBACK_PATH}"


def read_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` pairs, stopping each value at an inline ``#``."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.split("#")[0].strip()
    return values


def write_env_value(path: Path, key: str, value: str) -> bool:
    """Replace ``key`` in place, or append it. True when the file changed."""
    text = path.read_text()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        updated = pattern.sub(f"{key}={value}", text, count=1)
    else:
        updated = text.rstrip("\n") + f"\n{key}={value}\n"
    if updated != text:
        path.write_text(updated)
        return True
    return False


def login_url(api_key: str) -> str:
    return f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"


def exchange(api_key: str, api_secret: str, request_token: str) -> str:
    """Swap a request_token for an access_token via the Kite session endpoint."""
    # The checksum is sha256(api_key + request_token + api_secret). Computed here
    # rather than with the SDK so the script works before `pip install` has run.
    import hashlib

    checksum = hashlib.sha256(
        f"{api_key}{request_token}{api_secret}".encode()
    ).hexdigest()
    body = json.dumps(
        {"api_key": api_key, "request_token": request_token, "checksum": checksum}
    ).encode()
    request = urllib.request.Request(
        "https://api.kite.trade/session/token",
        data=body,
        headers={"Content-Type": "application/json", "X-Kite-Version": "3"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("message") or ""
        except Exception:
            pass
        raise SystemExit(
            f"Kite rejected the request_token (HTTP {exc.code}): {detail or exc.reason}\n"
            "A request_token is single-use and expires within a minute, so open the\n"
            "login URL again and paste the fresh redirect."
        )
    token = (data.get("data") or {}).get("access_token")
    if not token:
        raise SystemExit(f"Kite returned no access_token: {json.dumps(data)[:200]}")
    return token


def verify(api_key: str, access_token: str) -> Optional[str]:
    """Return the Zerodha user id when the token works, else None."""
    request = urllib.request.Request(
        PROFILE_URL,
        headers={"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return (json.load(response).get("data") or {}).get("user_id")
    except Exception:
        return None


def extract_request_token(text: str) -> Optional[str]:
    """Pull ``request_token`` out of a full redirect URL, a query string, or the
    bare value. Kite only ever puts it in the query string."""
    text = (text or "").strip()
    if not text:
        return None
    if "request_token=" in text:
        parsed = urllib.parse.urlparse(text)
        query = urllib.parse.parse_qs(parsed.query or text)
        token = (query.get("request_token") or [""])[0]
        return token or None
    # A bare token: Kite's are alphanumeric.
    return text if re.fullmatch(r"[A-Za-z0-9]{8,64}", text) else None


def push_secret(token: str) -> bool:
    """Set the GitHub secret. The value goes in on stdin, never on argv."""
    proc = subprocess.run(
        ["gh", "secret", "set", SECRET_NAME], input=token.encode(), capture_output=True
    )
    if proc.returncode != 0:
        print(f"gh secret set failed: {proc.stderr.decode()[:300]}", file=sys.stderr)
        return False
    return True


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--request-token", help="request_token or the full redirect URL")
    parser.add_argument("--github", action="store_true", help="also set the GitHub secret")
    parser.add_argument(
        "--push-secret",
        action="store_true",
        help="skip login; push the token already in bot/.env to GitHub",
    )
    parser.add_argument("--env", type=Path, default=ENV_PATH)
    args = parser.parse_args(argv)

    env = read_env(args.env)
    api_key = env.get("KITE_API_KEY", "")
    api_secret = env.get("KITE_API_SECRET", "")
    if not api_key or not api_secret:
        print(f"KITE_API_KEY / KITE_API_SECRET are empty in {args.env}", file=sys.stderr)
        return 2

    if args.push_secret:
        token = env.get(SECRET_NAME, "")
        if not token:
            print(f"{SECRET_NAME} is empty in {args.env}", file=sys.stderr)
            return 2
        user = verify(api_key, token)
        if not user:
            print(
                "The token in .env is no longer accepted by Kite; fetch a new one "
                "first (run without --push-secret).",
                file=sys.stderr,
            )
            return 2
        print(f"Token is valid for {user}.")
        if not push_secret(token):
            return 1
        print(f"GitHub secret {SECRET_NAME} updated.")
        return 0

    request_token = extract_request_token(args.request_token or "")
    if not request_token:
        print("Open this URL, complete the Zerodha login, then paste the address")
        print("Kite redirects you to (it contains request_token=...):\n")
        print(f"  {login_url(api_key)}\n")
        registered = env.get("KITE_REDIRECT_URL") or LOCAL_CALLBACK
        print(f"Registered Redirect URL: {registered}")
        if registered.rstrip("/") != LOCAL_CALLBACK:
            print(
                "  This must match the Kite developer console exactly.\n"
                f"  With `docker compose up` on this machine, {LOCAL_CALLBACK}\n"
                "  is the value that lets the bot capture the token itself."
            )
        print()
        pasted = input("Redirect URL or request_token: ")
        request_token = extract_request_token(pasted)
        if not request_token:
            print("That did not contain a request_token.", file=sys.stderr)
            return 2

    token = exchange(api_key, api_secret, request_token)
    user = verify(api_key, token)
    if not user:
        print("Kite issued a token but rejected it immediately.", file=sys.stderr)
        return 2
    write_env_value(args.env, SECRET_NAME, token)
    print(f"New token verified for {user}; written to {args.env}.")

    if args.github:
        if push_secret(token):
            print(f"GitHub secret {SECRET_NAME} updated; redeploy to use it.")
        else:
            return 1
    else:
        print("Run with --github to publish it, or use the dashboard's Connect button.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
