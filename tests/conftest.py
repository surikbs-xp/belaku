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
def offline_market_calendar(monkeypatch, tmp_path):
    """Give every test a calendar without touching the network.

    ``market_status()`` refuses to report the session open unless a holiday
    calendar has loaded, because an unverified session would let a LIVE order
    fire on a trading holiday. The calendar is fetched from NSE and cached under
    ``bot/data/``, which is gitignored - so a developer machine has a cache and a
    CI runner does not.

    That asymmetry made ~20 engine tests pass locally and fail in CI on the same
    commit: with no cache and no outbound access, ``is_market_open()`` answered
    False and every order was rejected with "Outside market hours (Session
    unverified)". It also made the suite depend on NSE being reachable, so the
    same SHA passed one run and failed the next.

    The cache is written into ``tmp_path`` and the network is stubbed, so this is
    deterministic and offline. A test that needs a specific calendar still builds
    its own ``HolidayCalendar`` and patches ``kite_client.get_calendar``; that
    patch wins over this one because it is applied later.
    """
    import json

    import market_calendar

    # A realistic 2026 F&O calendar. Only "a calendar exists" matters to the
    # session gate; the holiday-specific tests supply their own payload.
    holidays = {
        "2026-01-26": "Republic Day",
        "2026-03-03": "Holi",
        "2026-04-03": "Good Friday",
        "2026-05-01": "Maharashtra Day",
        "2026-08-15": "Independence Day",
        "2026-10-02": "Mahatma Gandhi Jayanti",
        "2026-10-20": "Dussehra",
        "2026-11-10": "Diwali-Balipratipada",
        "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
        "2026-12-25": "Christmas",
    }
    # A name of its own: the calendar tests point their own instances at
    # ``tmp_path / "nse_holidays.json"`` to model "no cache on disk", and sharing
    # the name made those cases find this baseline instead of an empty path.
    cache = tmp_path / "conftest_nse_holidays.json"
    cache.write_text(json.dumps({"source": "test", "holidays": holidays}))

    def unreachable(*_args, **_kwargs):
        raise OSError("network access is not available in the test suite")

    monkeypatch.setattr(market_calendar.urllib.request, "urlopen", unreachable)
    # The fetcher is asked for the calendar at import, so a stale singleton from
    # an earlier test would otherwise win over this cache.
    market_calendar.reset_calendar()
    monkeypatch.setattr(
        market_calendar,
        "default_cache_path",
        lambda: cache,
    )
    yield
    market_calendar.reset_calendar()


@pytest.fixture(autouse=True)
def reset_engine_singleton():
    """Drop the process-wide engine between tests.

    ``engine.get_engine`` caches one instance, and a per-strategy mode switch
    writes to it. Without this, a test that arms a book leaves the next test
    reading that book as live - a leak that only shows up in a full run, never
    when the test is run alone.
    """
    import engine

    engine._engine = None
    yield
    engine._engine = None


@pytest.fixture(autouse=True)
def reset_order_manager_singleton():
    """Drop the process-wide order manager between tests.

    ``order_manager.get_order_manager`` caches one instance and holds the
    per-book pause map on it. A test that pauses a book left the next test
    reading it as paused, so a manual entry was refused for the wrong reason -
    a leak that only shows up in a full run, never when the test is run alone.
    """
    import order_manager

    order_manager._manager = None
    yield
    order_manager._manager = None


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
