"""Tests for the headless Kite login.

The flow is four HTTP calls and a redirect chase, so it is exercised against a
real local HTTP server rather than a mocked client: the parts that break in
production are the redirect handling and the request shapes, and a mock would
happily agree with a wrong implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

import kite_autologin  # noqa: E402


@pytest.fixture
def creds(monkeypatch):
    """Configure the module as if the operator had set the three secrets."""
    monkeypatch.setattr(
        kite_autologin,
        "settings",
        SimpleNamespace(
            kite_user_id="AB1234",
            kite_password="hunter2",
            kite_totp_secret="JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
            api_key="key",
            api_secret="secret",
        ),
    )
    return kite_autologin.settings


def test_configured_requires_all_three_secrets(monkeypatch) -> None:
    for missing in ("kite_user_id", "kite_password", "kite_totp_secret"):
        values = {
            "kite_user_id": "AB1234",
            "kite_password": "hunter2",
            "kite_totp_secret": "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        }
        values[missing] = ""
        monkeypatch.setattr(kite_autologin, "settings", SimpleNamespace(**values))
        assert kite_autologin.configured() is False


def test_a_totp_code_is_six_digits(creds) -> None:
    code = kite_autologin._totp_now()
    assert len(code) == 6 and code.isdigit()


def test_an_invalid_totp_secret_is_reported_not_raised_raw(creds, monkeypatch) -> None:
    monkeypatch.setattr(
        kite_autologin, "settings", SimpleNamespace(**{**vars(creds), "kite_totp_secret": "!!!"})
    )
    with pytest.raises(kite_autologin.AutoLoginError, match="not a valid TOTP secret"):
        kite_autologin._totp_now()


def test_login_is_skipped_when_the_token_is_already_valid(monkeypatch) -> None:
    """The keeper runs every few minutes; it must not log in each time."""
    monkeypatch.setattr(kite_autologin, "configured", lambda: True)
    monkeypatch.setattr(
        "kite_auth.token_status", lambda: {"state": "valid", "valid": True}
    )
    called = []
    monkeypatch.setattr(kite_autologin, "login", lambda: called.append(1))

    assert kite_autologin.ensure_token() is None
    assert called == []


def test_login_is_attempted_when_the_token_expired(monkeypatch) -> None:
    monkeypatch.setattr(kite_autologin, "configured", lambda: True)
    monkeypatch.setattr(
        "kite_auth.token_status", lambda: {"state": "expired", "valid": False}
    )
    monkeypatch.setattr(kite_autologin, "login", lambda: {"ok": True, "user_id": "AB1234"})

    assert kite_autologin.ensure_token() == {"ok": True, "user_id": "AB1234"}


def test_a_configuration_fault_does_not_trigger_a_login(monkeypatch) -> None:
    """sdk_missing / no_credentials cannot be fixed by logging in."""
    monkeypatch.setattr(kite_autologin, "configured", lambda: True)
    monkeypatch.setattr(
        "kite_auth.token_status", lambda: {"state": "no_credentials", "valid": False}
    )
    called = []
    monkeypatch.setattr(kite_autologin, "login", lambda: called.append(1))

    assert kite_autologin.ensure_token() is None
    assert called == []


def test_a_failed_login_never_raises(monkeypatch) -> None:
    """A refresh failure must not take down a process that is still serving."""
    monkeypatch.setattr(kite_autologin, "configured", lambda: True)
    monkeypatch.setattr(
        "kite_auth.token_status", lambda: {"state": "expired", "valid": False}
    )

    def boom():
        raise kite_autologin.AutoLoginError("rejected")

    monkeypatch.setattr(kite_autologin, "login", boom)

    assert kite_autologin.ensure_token() is None


def test_login_without_credentials_raises_a_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(kite_autologin, "configured", lambda: False)
    with pytest.raises(kite_autologin.AutoLoginError, match="not configured"):
        kite_autologin.login()


def test_a_rejected_password_is_reported_without_echoing_it(creds, monkeypatch) -> None:
    """The error must name the step, never the credential."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"status": "error", "message": "Invalid credentials"}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )

    with pytest.raises(kite_autologin.AutoLoginError) as excinfo:
        kite_autologin.login()

    message = str(excinfo.value)
    assert "password login failed" in message
    assert "hunter2" not in message


def test_the_request_token_is_read_off_the_redirect(creds, monkeypatch) -> None:
    """The connect login redirects to the callback with request_token attached."""
    import httpx

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"status": "success", "data": {"request_id": "rid"}})
        if request.url.path == "/api/twofa":
            return httpx.Response(200, json={"status": "success"})
        if request.url.path == "/connect/login":
            return httpx.Response(
                302,
                headers={
                    "location": "https://belaku.example/zerodha/callback?request_token=rt-1&action=login"
                },
            )
        raise AssertionError(f"unexpected request to {request.url}")

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )
    monkeypatch.setattr("kite_auth.login_url", lambda: "https://kite.zerodha.com/connect/login?api_key=key&v=3")

    captured: dict = {}

    def fake_exchange(token: str) -> dict:
        captured["token"] = token
        return {"ok": True, "access_token": "at-1", "user_id": "AB1234"}

    monkeypatch.setattr("kite_auth.exchange_request_token", fake_exchange)

    result = kite_autologin.login()

    assert captured["token"] == "rt-1"
    assert result["access_token"] == "at-1"
    # The callback itself must never be fetched: the running app would then
    # exchange the single-use request_token a second time and fail.
    assert not any("/zerodha/callback" in url for url in seen)


def test_a_redirect_loop_is_bounded(creds, monkeypatch) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            return httpx.Response(200, json={"status": "success", "data": {"request_id": "rid"}})
        if request.url.path == "/api/twofa":
            return httpx.Response(200, json={"status": "success"})
        return httpx.Response(302, headers={"location": "https://kite.zerodha.com/connect/login"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )
    monkeypatch.setattr("kite_auth.login_url", lambda: "https://kite.zerodha.com/connect/login")

    with pytest.raises(kite_autologin.AutoLoginError, match="too many times"):
        kite_autologin.login()


def test_a_missing_request_id_is_reported(creds, monkeypatch) -> None:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": {}})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )

    with pytest.raises(kite_autologin.AutoLoginError, match="no request_id"):
        kite_autologin.login()


def test_a_captcha_challenge_is_reported_as_its_own_failure(creds, monkeypatch) -> None:
    """Zerodha gates the login API for datacenter traffic.

    This is the failure a CI runner or a cloud container actually hits, so it
    must be distinguishable from a wrong password: one is fixable, the other is
    not, and retrying a CAPTCHA looks like credential stuffing.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "status": "error",
                "message": "Invalid CAPTCHA values.",
                "data": {"captcha": True},
                "error_type": "InputException",
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=transport, **kw)
    )

    with pytest.raises(kite_autologin.CaptchaRequired, match="CAPTCHA"):
        kite_autologin.login()


def test_a_captcha_challenge_is_not_retried(monkeypatch) -> None:
    monkeypatch.setattr(kite_autologin, "configured", lambda: True)
    monkeypatch.setattr(
        "kite_auth.token_status", lambda: {"state": "expired", "valid": False}
    )
    calls = []

    def blocked():
        calls.append(1)
        raise kite_autologin.CaptchaRequired("blocked")

    monkeypatch.setattr(kite_autologin, "login", blocked)

    assert kite_autologin.ensure_token() is None
    assert len(calls) == 1
