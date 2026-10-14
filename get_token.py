"""Kite Connect OAuth callback and request-token exchange."""

from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse
import webbrowser

import requests


KITE_API_URL = "https://api.kite.trade"
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 9000
CALLBACK_PATH = "/zerodha/callback"


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> str:
    """Exchange Kite's one-time request token for a session access token."""
    checksum = sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()
    response = requests.post(
        f"{KITE_API_URL}/session/token",
        data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        headers={"X-Kite-Version": "3"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    access_token = payload.get("data", {}).get("access_token")
    if payload.get("status") != "success" or not access_token:
        raise RuntimeError("Kite did not return an access token")
    return access_token


def acquire_access_token(api_key: str, api_secret: str, timeout: int = 300) -> str:
    """Open Kite login, receive its redirect locally, and exchange the token."""
    callback_url = f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
    received: dict[str, Optional[str]] = {"request_token": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != CALLBACK_PATH:
                self.send_error(404)
                return
            if query.get("status", [""])[0] != "success":
                received["error"] = query.get("message", ["Kite login was not successful"])[0]
                body = b"Kite login was not successful. You may close this tab."
                self.send_response(400)
            else:
                received["request_token"] = query.get("request_token", [None])[0]
                body = b"Kite authorization received. You may close this tab and return to the terminal."
                self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), CallbackHandler)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot listen on {callback_url}; ensure the callback URL is configured "
            "in the Kite developer console and the port is available"
        ) from exc

    server.timeout = timeout
    login_url = "https://kite.zerodha.com/connect/login?" + urlencode(
        {"v": "3", "api_key": api_key}
    )
    print(f"Kite callback listener ready at {callback_url}")
    print("Approve the Kite login in the browser to continue; no token copying is needed.")
    webbrowser.open(login_url)
    try:
        server.handle_request()
    finally:
        server.server_close()

    if received["error"]:
        raise RuntimeError(str(received["error"]))
    request_token = received["request_token"]
    if not request_token:
        raise TimeoutError(f"No Kite callback received within {timeout} seconds")
    return exchange_request_token(api_key, api_secret, request_token)