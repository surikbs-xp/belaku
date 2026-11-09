from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))

# Tests must never inherit the developer's real ``bot/.env``. Otherwise they
# pass or fail depending on whether that person configured a dashboard password,
# an alert webhook or a stop-loss - and the production docs tell operators to
# set exactly those. Set here, before any test module imports ``bot``, because
# ``load_dotenv`` does not override variables already present in the
# environment.
#
# ALLOWED_HOSTS is special: ``bot`` reads it at import time to configure the
# TrustedHost middleware, so a later fixture cannot undo it. TestClient sends
# ``Host: testserver``, hence its inclusion.
os.environ["ALLOWED_HOSTS"] = "127.0.0.1,localhost,[::1],testserver"

# Read at call time, so a fixture could clear these - but doing it up front is
# one less thing to remember.
for _name in (
    "DASHBOARD_PASSWORD",
    "DASHBOARD_SESSION_SECRET",
    "ALERT_WEBHOOK_URL",
    "ALLOWED_ORIGINS",
    "ENABLE_API_DOCS",
    "LOG_LEVEL",
    "UVICORN_ACCESS_LOG",
    "TRADING_ALLOW_OFF_HOURS",
    "STOP_LOSS_POINTS",
    "MAX_DAILY_LOSS",
    "EXECUTION_MODE",
):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch):
    """Keep call-time environment reads clean for every test.

    A test that needs a variable sets it itself with ``monkeypatch.setenv``, so
    this is only a floor, never an override.
    """
    for name in (
        "DASHBOARD_PASSWORD",
        "DASHBOARD_SESSION_SECRET",
        "ALERT_WEBHOOK_URL",
        "ENABLE_API_DOCS",
        "TRADING_ALLOW_OFF_HOURS",
        "STOP_LOSS_POINTS",
        "MAX_DAILY_LOSS",
        "EXECUTION_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
