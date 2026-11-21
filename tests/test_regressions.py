from __future__ import annotations

import contextlib
import inspect
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from types import MethodType, SimpleNamespace

import config
import bot as bot_app
import database as db
import engine
import fill
import gift_nifty
import kite_auth
import kite_client
import order_manager
import pytest
import risk
import strategy
from config import BASE_DIR
from config import LIVE, PAPER, Settings
from gift_nifty import GiftNiftyFeed
from kite_client import IST, KiteClient, NetworkException
from order_manager import OrderManager


def use_test_database(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        db,
        "settings",
        SimpleNamespace(db_path=str(tmp_path / "test.db"), virtual_balance=1_000_000.0),
    )
    db.init_db()


def _isolate_gift_book(monkeypatch, tmp_path) -> None:
    """Point the GIFT index book at a throwaway file.

    Its own module-level singleton defaults to ``GIFT_BOOK_DB_PATH``, which on a
    developer machine is the *real* ledger. A test that lets the engine settle the
    index book would then append to it - and because the rows record the test's
    own name, the damage is invisible until someone reads the dashboard.
    """
    import gift_book

    book = gift_book.GiftBook(path=str(tmp_path / "gift_book.db"), enabled=True, units=65)
    monkeypatch.setattr(gift_book, "_book", book, raising=False)
    # Every strategy id resolves to this one throwaway book, so a test cannot
    # reach the real ledger through a per-strategy lookup.
    monkeypatch.setattr(gift_book, "get_gift_book", lambda strategy_id=None: book)


def use_risk_limits(
    monkeypatch,
    *,
    max_daily_loss=0.0,
) -> None:
    """Override the risk limits.

    ``Settings`` is a frozen dataclass, so the module's reference is swapped for
    a stand-in rather than mutated - the same approach ``use_test_database``
    takes for ``db.settings``.

    The daily loss cap is the only limit left: the strategy's own signal is the
    exit, so there is no per-trade stop to configure.
    """
    import risk

    monkeypatch.setattr(
        risk,
        "settings",
        SimpleNamespace(
            max_daily_loss=max_daily_loss,
            max_open_lots=4,
        ),
    )


def test_bot_configures_a_rotating_file_handler(monkeypatch, tmp_path) -> None:
    root = logging.getLogger()
    existing_handlers = list(root.handlers)
    original_level = root.level
    log_file = tmp_path / "bot.log"
    monkeypatch.setattr(bot_app, "LOG_FILE", log_file)
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    try:
        bot_app._configure_logging()
        logging.getLogger("bot.test").info("indicator snapshot to rotating file")
        file_handler = next(
            handler
            for handler in root.handlers
            if isinstance(handler, bot_app.RotatingFileHandler)
            and handler.baseFilename == str(log_file)
        )
        file_handler.flush()

        assert "indicator snapshot to rotating file" in log_file.read_text()
    finally:
        for handler in list(root.handlers):
            if handler not in existing_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(original_level)


def test_log_lines_are_stamped_in_ist_not_the_container_clock(monkeypatch, tmp_path) -> None:
    """The container runs in UTC; the dashboard reads every other time as IST.

    A UTC log line sits 5h30m behind the trade it explains, so the Logs tab
    looks stale even when the bot just acted.
    """
    root = logging.getLogger()
    existing_handlers = list(root.handlers)
    original_level = root.level
    log_file = tmp_path / "bot.log"
    monkeypatch.setattr(bot_app, "LOG_FILE", log_file)
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    try:
        bot_app._configure_logging()
        logging.getLogger("bot.test").info("stamped in IST")
        file_handler = next(
            handler
            for handler in root.handlers
            if isinstance(handler, bot_app.RotatingFileHandler)
            and handler.baseFilename == str(log_file)
        )
        file_handler.flush()

        stamp = log_file.read_text().split(" ", 2)
        written = datetime.strptime(f"{stamp[0]} {stamp[1]}", "%Y-%m-%d %H:%M:%S,%f")
        drift = abs((datetime.now(bot_app.db.IST).replace(tzinfo=None) - written).total_seconds())
        assert drift < 120, f"{written} is {drift:.0f}s from IST now"
    finally:
        for handler in list(root.handlers):
            if handler not in existing_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(original_level)


def test_log_tail_filters_by_level_and_keeps_tracebacks(monkeypatch, tmp_path) -> None:
    log_file = tmp_path / "bot.log"
    log_file.write_text(
        "\n".join(
            [
                "2026-11-07 09:15:00,001 INFO bot: candle closed at 25010.5",
                "2026-11-07 09:15:01,002 DEBUG bot.engine: vote 3/3 bull",
                "2026-11-07 09:15:02,003 WARNING bot.kite_client: Quote failed for NIFTY26KFUT: boom",
                "2026-11-07 09:15:03,004 ERROR bot.order_manager: Execution failure",
                "Traceback (most recent call last):",
                '  File "order_manager.py", line 1, in place',
                "ValueError: nope",
                "2026-11-07 09:15:04,005 INFO bot: recovered",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_app, "LOG_FILE", log_file)

    everything = bot_app.read_log_tail()
    assert everything["available"] is True
    assert everything["count"] == 8
    assert everything["lines"][0].startswith("2026-11-07 09:15:00")

    warnings = bot_app.read_log_tail(level="WARNING")
    # WARNING is a minimum severity, and the traceback rides along with the
    # ERROR it explains rather than being filtered out as unparsed noise.
    assert warnings["lines"] == [
        "2026-11-07 09:15:02,003 WARNING bot.kite_client: Quote failed for NIFTY26KFUT: boom",
        "2026-11-07 09:15:03,004 ERROR bot.order_manager: Execution failure",
        "Traceback (most recent call last):",
        '  File "order_manager.py", line 1, in place',
        "ValueError: nope",
    ]

    searched = bot_app.read_log_tail(search="recovered")
    assert searched["lines"] == ["2026-11-07 09:15:04,005 INFO bot: recovered"]

    # Oldest line first, capped to the newest `limit` entries.
    assert bot_app.read_log_tail(limit=2)["lines"] == [
        "ValueError: nope",
        "2026-11-07 09:15:04,005 INFO bot: recovered",
    ]


def test_log_tail_redacts_credentials(monkeypatch, tmp_path) -> None:
    log_file = tmp_path / "bot.log"
    log_file.write_text(
        "2026-11-07 09:15:00,001 INFO bot: KITE_ACCESS_TOKEN=abc123secret\n"
        "2026-11-07 09:15:01,002 INFO bot: request_token: 'def456'\n"
        '2026-11-07 09:15:02,003 INFO bot: password = "hunter2"\n'
        "2026-11-07 09:15:03,004 INFO bot: api_secret=zzz\n"
        "2026-11-07 09:15:04,005 INFO bot: subscribed instrument token 256265\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_app, "LOG_FILE", log_file)

    blob = "\n".join(bot_app.read_log_tail()["lines"])
    for secret in ("abc123secret", "def456", "hunter2", "zzz"):
        assert secret not in blob
    assert blob.count("***") == 4
    # A bare numeric instrument token is not a credential and must survive.
    assert "subscribed instrument token 256265" in blob


def test_log_tail_reports_a_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "absent.log")
    payload = bot_app.read_log_tail()
    assert payload["available"] is False
    assert payload["lines"] == []


def test_logs_endpoint_validates_the_level(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    log_file = tmp_path / "bot.log"
    log_file.write_text(
        "2026-11-07 09:15:00,001 INFO bot: warmed up\n"
        "2026-11-07 09:15:01,002 ERROR bot: feed dropped\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_app, "LOG_FILE", log_file)

    client = TestClient(bot_app.app)
    assert client.get("/api/logs", params={"level": "NOPE"}).status_code == 400

    ok = client.get("/api/logs", params={"limit": 5})
    assert ok.status_code == 200
    body = ok.json()
    assert body["level"] is None
    assert body["levels"] == list(bot_app.LOG_LEVELS)
    assert body["count"] == 2

    only_errors = client.get("/api/logs", params={"level": "warning"}).json()
    assert only_errors["level"] == "WARNING"
    assert only_errors["lines"] == ["2026-11-07 09:15:01,002 ERROR bot: feed dropped"]


def test_log_search_cannot_confirm_a_secret_that_was_redacted(monkeypatch, tmp_path) -> None:
    """Redaction must happen before the search filter, not after.

    Otherwise ?search= becomes a byte-by-byte oracle: the secret is masked in
    the response, but its presence is still confirmable from the match count.
    """
    log_file = tmp_path / "bot.log"
    log_file.write_text(
        "2026-11-07 09:15:00,001 INFO bot: Authorization: token ABCKEY:SUPERSECRETVALUE\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bot_app, "LOG_FILE", log_file)

    hit = bot_app.read_log_tail(search="SUPERSECRET")
    miss = bot_app.read_log_tail(search="WRONGVALUE")

    # The oracle is closed: neither the real prefix nor a wrong guess matches.
    assert hit["count"] == 0
    assert miss["count"] == 0

    # And the secret never appears in the unfiltered tail either.
    blob = "\n".join(bot_app.read_log_tail()["lines"])
    assert "SUPERSECRETVALUE" not in blob
    assert "ABCKEY" not in blob


def test_log_redaction_covers_sdk_shapes() -> None:
    """The Kite SDK emits these forms; the key/value pattern alone misses them."""
    cases = {
        "Authorization: token vwajai1234:62VGx3secret": ["vwajai1234", "62VGx3secret"],
        "kite.set_access_token(62VGx3secret)": ["62VGx3secret"],
        "TokenException: Invalid token: 62VGx3secret": ["62VGx3secret"],
        "api_secret xs1s3hsecret": ["xs1s3hsecret"],
        "request_token=abc123&status=success": ["abc123"],
    }
    for line, secrets in cases.items():
        redacted = bot_app._redact_secrets(line)
        for secret in secrets:
            assert secret not in redacted, f"{secret!r} survived in {redacted!r}"
        assert "***" in redacted, f"nothing masked in {redacted!r}"


def test_sdk_loggers_are_capped_so_a_debug_session_cannot_leak_the_token() -> None:
    """LOG_LEVEL=DEBUG must not put the Authorization header into the log."""
    root = logging.getLogger()
    existing = list(root.handlers)
    original = root.level
    try:
        bot_app._configure_logging()
        for name in ("kiteconnect", "urllib3", "httpx", "httpcore"):
            assert logging.getLogger(name).level >= logging.WARNING
    finally:
        for handler in list(root.handlers):
            if handler not in existing:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(original)


def test_cross_site_post_is_blocked_but_same_origin_is_allowed(monkeypatch, tmp_path) -> None:
    """A page on another site must not be able to drive the control endpoints."""
    use_test_database(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient

    client = TestClient(bot_app.app)

    # A malicious page: rejected before the handler can act.
    blocked = client.post(
        "/api/pause",
        json={"paused": True, "reason": "csrf"},
        headers={"Origin": "https://evil.com", "Host": "127.0.0.1:9000"},
    )
    assert blocked.status_code == 403
    assert "Cross-site" in blocked.json()["detail"]

    # The same call from the dashboard itself still works.
    allowed = client.post(
        "/api/pause",
        json={"paused": True, "reason": "manual"},
        headers={"Origin": "http://127.0.0.1:9000", "Host": "127.0.0.1:9000"},
    )
    assert allowed.status_code == 200

    # A non-browser caller (no Origin) is unaffected: curl, healthcheck, tests.
    no_origin = client.post(
        "/api/pause",
        json={"paused": False, "reason": "manual"},
        headers={"Host": "127.0.0.1:9000"},
    )
    assert no_origin.status_code == 200


def test_health_stays_reachable_without_an_origin_header() -> None:
    """The container healthcheck must keep working after the CSRF guard."""
    from fastapi.testclient import TestClient

    response = TestClient(bot_app.app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_spoofed_host_is_rejected() -> None:
    """DNS rebinding: a browser treats http://evil.com as same-origin when it
    resolves to the bot, so a Host allow-list is the only thing that stops it."""
    from fastapi.testclient import TestClient

    client = TestClient(bot_app.app)
    assert client.get("/api/status", headers={"Host": "evil.com"}).status_code == 400
    assert client.get("/api/status", headers={"Host": "127.0.0.1:9000"}).status_code == 200


def test_security_headers_are_present() -> None:
    from fastapi.testclient import TestClient

    headers = TestClient(bot_app.app).get("/api/health").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    csp = headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


def test_callback_page_escapes_the_status_parameter() -> None:
    """`status` comes straight off the query string into an HTML page that runs
    same-origin with the dashboard, so it must be escaped, not interpolated."""
    from fastapi.testclient import TestClient

    payload = "<script>alert(document.domain)</script>"
    response = TestClient(bot_app.app).get(
        "/zerodha/callback", params={"status": payload}
    )

    assert response.status_code == 200
    assert "<script>alert(document.domain)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_callback_page_escapes_broker_error_text(monkeypatch) -> None:
    """Broker error text is attacker-influenceable too (it echoes the request)."""
    from fastapi.testclient import TestClient

    def boom(_token: str) -> dict:
        raise kite_auth.KiteAuthError("<img src=x onerror=alert(1)>bad token")

    monkeypatch.setattr(kite_auth, "exchange_request_token", boom)
    response = TestClient(bot_app.app).get(
        "/zerodha/callback", params={"request_token": "x"}
    )

    assert response.status_code == 200
    assert "<img src=x" not in response.text
    assert "&lt;img src=x" in response.text


def test_panic_and_pause_reject_a_reason_that_could_forge_a_log_line() -> None:
    """`reason` is logged verbatim, so a newline would inject a fake record."""
    from fastapi.testclient import TestClient

    client = TestClient(bot_app.app)
    forged = "x\n2026-11-07 10:00:00,000 CRITICAL bot: FAKE LINE INJECTED"

    assert client.post("/api/pause", json={"paused": True, "reason": forged}).status_code == 422
    assert client.post("/api/panic", json={"confirm": True, "reason": forged}).status_code == 422
    # A normal label still passes.
    assert client.post("/api/pause", json={"paused": True, "reason": "MANUAL"}).status_code == 200
    client.post("/api/pause", json={"paused": False, "reason": "MANUAL"})


def test_api_docs_are_disabled_by_default() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(bot_app.app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_the_adx_filter_suppresses_an_entry_it_does_not_allow() -> None:
    """A configured filter must actually gate the signal.

    ``filter_adx`` and ``filter_ma`` were parsed from ``STRATEGY_<n>_*`` and
    reported by the API, but ``strategy_params()`` never passed them to
    ``evaluate`` - so a strategy configured as "ADX>15" traded completely
    unfiltered. The backtest that justified the filter was measuring something
    the live bot never did.
    """
    candles = [
        {
            "time": f"2026-11-16T09:{15 + index:02d}:00+05:30",
            "open": 25_000.0 + index,
            "high": 25_010.0 + index,
            "low": 24_990.0 + index,
            "close": 25_000.0 + index,
        }
        for index in range(40)
    ]

    unfiltered = strategy.evaluate(candles, {"filter_adx": 0})
    # A threshold above any achievable ADX must suppress whatever the rules said.
    filtered = strategy.evaluate(candles, {"filter_adx": 1000})

    assert filtered.signal == strategy.NO_SIGNAL
    assert filtered.values["filter_passed"] is False
    assert "ADX" in filtered.values["filter_reason"]
    assert "filtered:" in filtered.detail
    # The gate may only remove a signal, never invent one.
    if unfiltered.signal == strategy.NO_SIGNAL:
        assert filtered.signal == strategy.NO_SIGNAL


def test_the_ma_filter_needs_its_own_warm_up() -> None:
    """A 100-bar average cannot be tested on 15 bars.

    Without this the first bars of a series would pass a gate that had no data
    behind it, which is the one way a filter can be worse than no filter.
    """
    assert strategy.required_bars({"filter_ma": 100}) == 101
    assert strategy.required_bars({"filter_ma": 0}) == 15
    assert strategy.required_bars() == 15


def test_strategy_params_carries_the_filters_to_evaluate() -> None:
    """The config's filters must reach ``evaluate``, not just the API payload."""
    import config

    cfg = config.StrategyConfig(
        index=2, id="nifty-adx", source="nifty", interval="60minute",
        exit="flip", lots=1, enabled=True, name="", filter_adx=15.0, filter_ma=100,
    )
    params = cfg.strategy_params()

    assert params["filter_adx"] == 15.0
    assert params["filter_ma"] == 100


def test_pandas_ta_indicators_warm_up_from_real_candles() -> None:
    candles = [
        {
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
        }
        for index in range(15)
    ]

    # The single 14x3 SuperTrend needs 14 ATR bars plus one to compare against.
    assert engine.required_bars() == 15
    with pytest.raises(strategy.InsufficientData, match="need 15 closed candles"):
        strategy.evaluate(candles[:14])

    verdict = strategy.evaluate(candles)
    assert set(verdict.conditions) == {
        "buy_supertrend",
        "buy_psar",
        "sell_supertrend",
        "sell_psar",
    }
    assert set(verdict.values) == {
        "psar",
        "supertrend",
        "supertrend_bullish",
        "supertrend_bearish",
        "supertrend_config",
        "st_14_3",
        "buy_confirmations",
        "sell_confirmations",
        "confirmations_total",
        "previous_signal",
        "candle_range",
        # The optional entry gates report their own state, so a filtered bar can
        # say which gate refused instead of just showing no signal.
        "filter_passed",
        "filter_reason",
        "filter_adx",
        "filter_ma",
    }
    assert isinstance(verdict.values["psar"], float)
    assert isinstance(verdict.values["st_14_3"], float)
    assert verdict.values["supertrend_bullish"] is True
    assert verdict.values["supertrend_bearish"] is False


def test_supertrend_is_fixed_at_14x3_and_not_configurable(monkeypatch) -> None:
    """The strategy has exactly one SuperTrend, so there is nothing to parse.

    A ``STRATEGY_SUPERTRENDS`` value left in an old ``.env`` must not resurrect a
    second instance: the vote it implies is the thing this strategy dropped.
    """
    monkeypatch.setenv("STRATEGY_SUPERTRENDS", "9:1.5, 21:4")

    assert strategy.SUPERTREND_ATR_LENGTH == 14
    assert strategy.SUPERTREND_MULTIPLIER == 3.0
    assert strategy.required_bars() == 15
    assert config.strategy_params() == {
        "psar_start": 0.02,
        "psar_increment": 0.02,
        "psar_maximum": 0.2,
        "exit": "flip",
    }
    assert config.strategy_summary()["supertrend_label"] == "14×3"
    assert config.strategy_summary()["supertrend"] == {"length": 14, "multiplier": 3.0}


def test_psar_initializes_direction_from_first_close_change() -> None:
    assert strategy.psar([10.0, 9.0, 8.0], [8.0, 7.0, 6.0], [9.0, 8.0, 7.0])[1] == 10.0


def test_psar_follows_wilder_reversal_and_two_bar_clamp() -> None:
    highs = [10.0, 11.0, 12.0, 11.0, 10.0, 12.0]
    lows = [8.0, 9.0, 10.0, 9.0, 8.0, 10.0]
    closes = [9.0, 10.0, 11.0, 10.0, 9.0, 11.0]

    assert strategy.psar(highs, lows, closes) == [None, 8.0, 8.0, 8.16, 12.0, 8.0]


def test_psar_uses_configured_acceleration_steps() -> None:
    highs = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    lows = [value - 2.0 for value in highs]
    closes = [value - 1.0 for value in highs]

    default = strategy.psar(highs, lows, closes, increment=0.02)
    faster = strategy.psar(highs, lows, closes, increment=0.05)

    assert default[-1] != faster[-1]


def _reference_series(bars: int = 300) -> tuple[list[float], list[float], list[float]]:
    """Deterministic OHLC series used to pin the indicators to their references."""
    import random

    rng = random.Random(7)
    closes = [24_600.0]
    for _ in range(bars - 1):
        closes.append(closes[-1] + rng.gauss(0, 12))
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) + rng.uniform(0, 8) for o, c in zip(opens, closes)]
    lows = [min(o, c) - rng.uniform(0, 8) for o, c in zip(opens, closes)]
    return highs, lows, closes


def _reference_psar(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.2,
) -> list[float | None]:
    """Published Pine ``ta.sar`` reference, transcribed independently of the bot."""
    out: list[float | None] = [None] * len(highs)
    if len(highs) < 2:
        return out
    is_below = closes[1] > closes[0]
    result = lows[0] if is_below else highs[0]
    extreme = highs[1] if is_below else lows[1]
    acceleration = start
    out[1] = result
    for i in range(2, len(highs)):
        result += acceleration * (extreme - result)
        if is_below:
            if result > lows[i]:
                is_below = False
                result = max(highs[i], extreme)
                extreme = lows[i]
                acceleration = start
            elif highs[i] > extreme:
                extreme = highs[i]
                acceleration = min(acceleration + increment, maximum)
        else:
            if result < highs[i]:
                is_below = True
                result = min(lows[i], extreme)
                extreme = highs[i]
                acceleration = start
            elif lows[i] < extreme:
                extreme = lows[i]
                acceleration = min(acceleration + increment, maximum)
        if is_below:
            result = min(result, lows[i - 1], lows[i - 2])
        else:
            result = max(result, highs[i - 1], highs[i - 2])
        out[i] = result
    return out


def _reference_supertrend(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    length: int,
    factor: float,
) -> tuple[list[float | None], list[int]]:
    """TradingView ``ta.supertrend`` reference: RMA ATR, ratcheted bands.

    Returns ``(line, direction)`` with ``direction == 1`` meaning bullish, which
    is the opposite sign of ``strategy.supertrend``'s convention.
    """
    count = len(highs)
    true_range: list[float] = [float("nan")] * count
    for i in range(1, count):
        true_range[i] = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )

    atr: list[float] = [float("nan")] * count
    seed: list[float] = []
    running = float("nan")
    for i, value in enumerate(true_range):
        if value != value:  # NaN
            continue
        if running != running:
            seed.append(value)
            if len(seed) == length:
                running = sum(seed) / length
                atr[i] = running
            continue
        running = (running * (length - 1) + value) / length
        atr[i] = running

    final_upper: list[float] = [float("nan")] * count
    final_lower: list[float] = [float("nan")] * count
    line: list[float | None] = [None] * count
    direction: list[int] = [0] * count
    for i in range(1, count):
        if atr[i] != atr[i]:
            continue
        middle = (highs[i] + lows[i]) / 2.0
        upper = middle + factor * atr[i]
        lower = middle - factor * atr[i]
        if final_upper[i - 1] != final_upper[i - 1] or final_lower[i - 1] != final_lower[i - 1]:
            final_upper[i], final_lower[i], trend = upper, lower, 1
        else:
            final_upper[i] = (
                upper
                if upper < final_upper[i - 1] or closes[i - 1] > final_upper[i - 1]
                else final_upper[i - 1]
            )
            final_lower[i] = (
                lower
                if lower > final_lower[i - 1] or closes[i - 1] < final_lower[i - 1]
                else final_lower[i - 1]
            )
            if closes[i] > final_upper[i - 1]:
                trend = 1
            elif closes[i] < final_lower[i - 1]:
                trend = -1
            else:
                trend = direction[i - 1]
        direction[i] = trend
        line[i] = final_lower[i] if trend > 0 else final_upper[i]
    return line, direction


def test_psar_matches_the_pine_reference_on_a_long_series() -> None:
    highs, lows, closes = _reference_series()

    values = strategy.psar(highs, lows, closes)
    expected = _reference_psar(highs, lows, closes)

    assert values == pytest.approx(expected, abs=1e-9)


def test_supertrend_matches_the_pine_reference_on_a_long_series() -> None:
    highs, lows, closes = _reference_series()

    line, directions = strategy.supertrend(
        highs, lows, closes,
        strategy.SUPERTREND_MULTIPLIER, strategy.SUPERTREND_ATR_LENGTH,
    )
    expected_line, expected_direction = _reference_supertrend(
        highs, lows, closes,
        strategy.SUPERTREND_ATR_LENGTH, strategy.SUPERTREND_MULTIPLIER,
    )

    assert line == pytest.approx(expected_line, abs=1e-9)
    # strategy.supertrend negates the library direction, so bullish is < 0.
    assert directions == [-value for value in expected_direction]


def _gift_ticks(feed: GiftNiftyFeed, *ticks: tuple[float, datetime]) -> None:
    feed._builder.on_close = feed._on_bar_close
    for price, moment in ticks:
        feed._builder.add_tick(price, moment)


def _at(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 11, 6, 10, minute, second, tzinfo=IST)


def test_bar_joined_late_is_excluded_from_indicator_history(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    feed = GiftNiftyFeed("5minute")

    _gift_ticks(
        feed,
        (100.0, _at(1, 30)),  # feed joined 90s into the 10:00 bar
        (101.0, _at(5, 1)),
        (102.0, _at(10, 1)),
    )

    assert [candle["time"] for candle in feed.candles()] == ["2026-11-06T10:05:00+05:30"]
    stored = {row["start"][11:16]: row["partial"] for row in db.get_gift_candles()}
    assert stored == {"10:00": gift_nifty.PARTIAL_GAP, "10:05": gift_nifty.PARTIAL_NONE}


def test_feed_outage_inside_a_bar_excludes_that_bar_only(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    feed = GiftNiftyFeed("5minute")
    feed._builder.on_close = feed._on_bar_close
    feed._builder.add_tick(100.0, _at(0, 1))
    feed._builder.add_tick(101.0, _at(5, 1))
    feed._builder.mark_outage()
    feed._builder.add_tick(102.0, _at(5, 2))  # reconnected, same bar
    feed._builder.add_tick(103.0, _at(10, 1))
    feed._builder.add_tick(104.0, _at(15, 1))

    assert [candle["time"][11:16] for candle in feed.candles()] == ["10:00", "10:10"]


def test_shutdown_cut_bar_is_dropped_unless_restarted_in_same_bucket(
    monkeypatch, tmp_path
) -> None:
    use_test_database(monkeypatch, tmp_path)

    class FixedDateTime(datetime):
        current = datetime(2026, 11, 6, 10, 2, 0, tzinfo=IST)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(gift_nifty, "datetime", FixedDateTime)
    FixedDateTime.current = datetime(2026, 11, 6, 10, 0, 3, tzinfo=IST)
    first = GiftNiftyFeed("5minute")
    first._handle_ticks(None, [{"last_price": 100.0}])
    FixedDateTime.current = datetime(2026, 11, 6, 10, 2, 0, tzinfo=IST)
    first.stop()
    assert [row["partial"] for row in db.get_gift_candles()] == [gift_nifty.PARTIAL_CUT]

    FixedDateTime.current = datetime(2026, 11, 6, 10, 20, 0, tzinfo=IST)
    later = GiftNiftyFeed("5minute")
    assert later.load_persisted() == 0
    later._handle_ticks(None, [{"last_price": 101.0}])
    assert later.candles() == []

    FixedDateTime.current = datetime(2026, 11, 6, 10, 2, 30, tzinfo=IST)
    same_bucket = GiftNiftyFeed("5minute")
    assert same_bucket.load_persisted() == 1
    same_bucket._handle_ticks(None, [{"last_price": 102.0}])
    FixedDateTime.current = datetime(2026, 11, 6, 10, 5, 1, tzinfo=IST)
    same_bucket._handle_ticks(None, [{"last_price": 103.0}])
    assert [candle["time"][11:16] for candle in same_bucket.candles()] == ["10:00"]


def test_init_db_adds_partial_column_to_existing_candle_table(tmp_path) -> None:
    import sqlite3

    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE gift_candles (start TEXT PRIMARY KEY, open REAL NOT NULL, "
            "high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, "
            "volume REAL NOT NULL DEFAULT 0, ticks INTEGER NOT NULL DEFAULT 0, "
            "created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO gift_candles VALUES ('2026-11-06T10:00:00+05:30', 1, 2, 0, 1, 0, 3, 'x')"
        )

    db.init_db(path)

    assert db.get_gift_candles(path=path)[0]["partial"] == 0


def test_ltp_ticks_contribute_to_explicit_volume_proxy() -> None:
    feed = GiftNiftyFeed("1minute")
    feed._handle_ticks(
        None,
        [
            {"last_price": 100.0, "volume_traded": 10_000},
            {"last_price": 101.0, "volume_traded": 10_001},
        ],
    )

    assert feed.in_progress["volume"] == 2.0
    assert feed.status()["volume_basis"] == "tick_count_proxy"


def test_candle_snapshot_includes_forming_candle() -> None:
    feed = GiftNiftyFeed("1minute")
    feed._handle_ticks(None, [{"last_price": 100.0}])

    candles, in_progress = feed.candle_snapshot()

    assert in_progress
    assert len(candles) == 1
    assert candles[0]["close"] == 100.0
    assert candles[0]["ticks"] == 1


def test_feed_resumes_persisted_partial_candle_without_duplicate(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    class FixedDateTime(datetime):
        current = datetime(2026, 11, 6, 6, 4, tzinfo=IST)

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    start = gift_nifty.bucket_start(FixedDateTime.current, 300)
    db.record_gift_candle(
        start=start.isoformat(timespec="seconds"),
        open_=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=5.0,
        ticks=5,
    )
    monkeypatch.setattr(gift_nifty, "datetime", FixedDateTime)

    feed = GiftNiftyFeed("5minute")
    assert feed.load_persisted() == 1
    assert feed.candles() == []
    assert feed.candle_snapshot() == ([], False)

    feed._handle_ticks(None, [{"last_price": 103.0}])
    snapshot, in_progress = feed.candle_snapshot()
    assert in_progress
    assert len(snapshot) == 1
    assert snapshot[0] == {
        "time": start.isoformat(timespec="seconds"),
        "open": 100.0,
        "high": 103.0,
        "low": 99.0,
        "close": 103.0,
        "volume": 6.0,
        "ticks": 6,
    }

    FixedDateTime.current = datetime(2026, 11, 6, 6, 5, tzinfo=IST)
    feed._handle_ticks(None, [{"last_price": 104.0}])
    closed = feed.candles()
    snapshot, in_progress = feed.candle_snapshot()
    assert len(closed) == 1
    assert closed[0]["time"] == start.isoformat(timespec="seconds")
    assert len(snapshot) == 2
    assert snapshot[0]["time"] != snapshot[1]["time"]
    assert in_progress


def test_feed_status_exposes_last_candle_as_source_fallback() -> None:
    feed = GiftNiftyFeed("5minute")
    feed._history = [{
        "time": "2026-11-05T21:40:00+05:30",
        "open": 100.0,
        "high": 105.0,
        "low": 99.0,
        "close": 104.0,
        "volume": 5.0,
    }]

    status = feed.status()

    assert status["last_price"] is None
    assert status["last_candle_close"] == 104.0
    assert status["last_candle_at"] == "2026-11-05T21:40:00+05:30"


def test_engine_status_evaluates_the_closed_bar_the_engine_actually_trades(monkeypatch) -> None:
    """The dashboard must not advertise a signal the engine will never act on.

    The rule checks used to be computed from the *forming* bar while the engine
    traded the last closed one. A wide forming candle therefore flipped the
    dashboard to BULL while the engine still saw NO_SIGNAL on the bar it acts on
    - the operator saw a signal that could not fire. Both verdicts now come from
    closed bars; the forming bar is reported separately.
    """
    feed = GiftNiftyFeed("1minute")
    feed._history = [
        {
            "time": f"2026-11-05T09:{index:02d}:00+05:30",
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 1.0,
        }
        # 17 closed bars, so the "previous" verdict still has the 15 it needs
        # after the newest closed bar is dropped.
        for index in range(17)
    ]
    feed._handle_ticks(None, [{"last_price": 118.0}])
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": True})
    monkeypatch.setattr(
        engine,
        "get_order_manager",
        lambda: SimpleNamespace(
            current_symbol=lambda: "NIFTY", paused=False, paused_reason=""
        ),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)

    status = engine.StrategyEngine().status()
    snapshot, _ = feed.candle_snapshot()

    assert status["current_bar_in_progress"]
    # The forming bar is still reported, but only as a timestamp - it never
    # decides a signal.
    assert status["forming_bar_at"] == snapshot[-1]["time"]
    # Both verdicts are computed from closed bars.
    assert status["current_values"] == strategy.evaluate(feed.candles()).values
    assert status["current_bar_at"] == feed.candles()[-1]["time"]
    assert status["previous_values"] == strategy.evaluate(feed.candles()[:-1]).values
    assert status["previous_bar_at"] == feed.candles()[-2]["time"]
    # `current_bar_at` is the bar the verdict came from, not the forming one.
    assert status["current_bar_at"] != status["forming_bar_at"]
    assert status["previous_values"]["psar"] != status["current_values"]["psar"]
    assert status["last_signal"] == strategy.NO_SIGNAL


def test_engine_after_hours_paper_order_does_not_price_from_the_gift_close(
    monkeypatch,
) -> None:
    """The GIFT close gates the fill but must never *price* it.

    Booking an entry at the index level wrote the index/future basis into the
    position, so the row opened showing a P&L no trade had produced. The fill has
    to come from the future's own quote, which the order manager resolves.
    """
    captured = {}

    class FakeResult:
        ok = True
        reason = "simulated"

        def to_dict(self):
            return {"ok": True, "action": "BUY"}

    class FakeManager:
        paused = False

        def current_symbol(self):
            return "NIFTY26NOVFUT"

        def process_signal(self, payload, source, strategy_id=None, mode=None):
            captured.update(payload=payload, source=source)
            return FakeResult()

    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": False, "reason": "Post-market"})
    monkeypatch.setattr(engine, "get_order_manager", lambda: FakeManager())
    signal_time = datetime.now(IST)

    result = engine.StrategyEngine().execute(
        "BUY",
        "closed-candle signal",
        paper_reference_price=24_418.5,
        signal_timestamp=signal_time,
    )

    assert result["ok"]
    assert captured["source"] == "ENGINE"
    # The reference price still marks the signal as after-hours, but it is not
    # carried as a fill price - 24,418.5 must not appear in the payload at all.
    assert captured["payload"]["paper_after_hours"] is True
    assert "price" not in captured["payload"]
    assert 24_418.5 not in captured["payload"].values()
    assert captured["payload"]["timestamp"] == signal_time.isoformat()


def test_engine_primes_on_startup_then_acts_on_the_next_bar(monkeypatch, tmp_path) -> None:
    """The first evaluated bar only primes; the next *transition* acts.

    A fresh process cannot tell a genuine signal from a stale one, so booting into
    a directional verdict does not open a position. It does on the bar where the
    pair *becomes* directional - entries track a transition, not the state, which
    is what stops a stop-out from being re-entered on the signal that caused it.

    ``market_status`` is pinned open because the decision is deliberately held
    until the first bar that closes in-session; without it the test would pass or
    fail depending on the wall clock when it ran.
    """
    # Isolate the ledgers. Without this the engine's GIFT index book writes its
    # signal trail into the developer's real gift_book.db, which is how that file
    # came to hold hundreds of rows explaining that a test failed.
    use_test_database(monkeypatch, tmp_path)
    _isolate_gift_book(monkeypatch, tmp_path)

    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )

    class FakeFeed:
        seconds = 300

        def __init__(self):
            self.rows = [{"time": "2026-11-05T22:10:00+05:30"}]

        def candles(self):
            return self.rows

    feed = FakeFeed()
    verdicts = iter([
        # Boots into an existing BULL: not a transition, so nothing opens.
        strategy.StrategyVerdict(strategy.BULL, 100.0, previous_signal=strategy.BULL),
        # Still BULL: the state persists, so still nothing.
        strategy.StrategyVerdict(strategy.BULL, 101.0, previous_signal=strategy.BULL),
        # BULL -> BEAR is a transition, so this bar trades.
        strategy.StrategyVerdict(strategy.BEAR, 99.0, previous_signal=strategy.BULL),
    ])
    actions = []
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine.strategy, "evaluate", lambda *_args: next(verdicts))
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(
        engine,
        "get_order_manager",
        lambda: SimpleNamespace(current_symbol=lambda: "NIFTY"),
    )
    monkeypatch.setattr(db, "get_open_positions", lambda mode=None, strategy_id=None: [])
    # Pin the strategy list to the single default book, so this test measures the
    # engine rather than whatever the developer's .env configures.
    monkeypatch.setattr(
        engine, "strategy_configs",
        lambda: [config.StrategyConfig(
            index=1, id="Surikbs", source="gift", interval="60minute",
            exit="flip", lots=1, enabled=True, name="",
        )],
    )
    runner = engine.StrategyEngine()
    monkeypatch.setattr(
        runner,
        "execute",
        lambda action, detail, **kwargs: actions.append(action) or {"ok": True},
    )

    assert runner.evaluate_now()["action"] is None
    feed.rows.append({"time": "2026-11-05T22:15:00+05:30"})
    assert runner.evaluate_now()["action"] is None
    feed.rows.append({"time": "2026-11-05T22:20:00+05:30"})
    assert runner.evaluate_now()["action"] == "SELL"
    assert actions == ["SELL"]


def test_engine_logs_closed_gift_candle_and_indicator_values(monkeypatch, caplog, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    _isolate_gift_book(monkeypatch, tmp_path)

    class FakeFeed:
        seconds = 300

        def candles(self):
            return [{
                "time": "2026-11-05T22:10:00+05:30",
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
            }]

    monkeypatch.setattr(engine, "get_feed", FakeFeed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(
        engine.strategy,
        "evaluate",
        lambda *_args: strategy.StrategyVerdict(
            strategy.NO_SIGNAL,
            100.5,
            values={"psar": 98.75, "st_14_3": 99.25},
        ),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    # Pin the strategy list to the single default book, so this test measures the
    # engine rather than whatever the developer's .env configures.
    monkeypatch.setattr(
        engine, "strategy_configs",
        lambda: [config.StrategyConfig(
            index=1, id="Surikbs", source="gift", interval="60minute",
            exit="flip", lots=1, enabled=True, name="",
        )],
    )
    caplog.set_level("INFO", logger="engine")

    engine.StrategyEngine().evaluate_now()

    message = next(
        record.message
        for record in caplog.records
        if record.name == "engine" and "Indicator values" in record.message
    )
    assert "source=NSEIX:GIFT NIFTY" in message
    assert "interval_seconds=300" in message
    assert "candle_at=2026-11-05T22:10:00+05:30" in message
    assert '"close": 100.5' in message
    assert '"psar": 98.75' in message
    assert '"st_14_3": 99.25' in message


def test_engine_exits_an_opposing_paper_position_on_startup(monkeypatch, tmp_path) -> None:
    """A flip against an open leg is the exit, not a reversal.

    The bar that turns the pair against the position closes it; the opposite side
    is a *separate* decision and needs its own fresh signal on a later bar, so the
    two never share a candle.
    """
    use_test_database(monkeypatch, tmp_path)
    _isolate_gift_book(monkeypatch, tmp_path)

    # Pinned open: the decision is held until the first in-session bar, so an
    # unstubbed market_status makes the test depend on the wall clock.
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )

    class FakeFeed:
        seconds = 300

        def candles(self):
            return [{"time": "2026-11-05T22:50:00+05:30"}]

    feed = FakeFeed()
    # A transition into BEAR, so the bar is a fresh signal and the opposing long
    # is corrected on it.
    verdict = strategy.StrategyVerdict(
        strategy.BEAR, 95.0, previous_signal=strategy.BULL
    )
    captured = []
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine.strategy, "evaluate", lambda *_args: verdict)
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(
        engine,
        "get_order_manager",
        lambda: SimpleNamespace(current_symbol=lambda: "NIFTY"),
    )
    # The stub mirrors the real signature: the engine now scopes the lookup to
    # the book, so a stub that only accepts `mode` would hide the very argument
    # this test is about.
    monkeypatch.setattr(
        db,
        "get_open_positions",
        lambda path=None, mode=None, strategy_id=None: [{
            "symbol": "NIFTY",
            "position_key": 1,
            "quantity": 65,
        }],
    )
    runner = engine.StrategyEngine()
    monkeypatch.setattr(
        runner,
        "execute",
        lambda action, detail, **kwargs: captured.append(
            (action, kwargs["paper_reference_price"])
        ) or {"ok": True},
    )

    result = runner.evaluate_now()

    # The exit ran and the broker call is recorded against it, not an entry.
    assert result["action"] is None
    assert result["result"]["kind"] == "SIGNAL_FLIP"
    assert captured == [], "the opposite side must not open on the exit bar"


def test_after_hours_paper_engine_pnl_marks_the_future_not_gift(monkeypatch, tmp_path) -> None:
    """The futures book must mark at the future's own price once NSE closes.

    It used to substitute the GIFT Nifty level after hours, which made a closed
    position keep moving on an index that still ticks - the mark disagreed with
    the LTP the dashboard showed for the very same contract. GIFT at 95 is
    deliberately far from the future's 110 so the two rules cannot be confused.
    """
    use_test_database(monkeypatch, tmp_path)
    db.open_position(
        "NIFTY26NOVFUT", 65, 100.0, mode=PAPER, source="ENGINE"
    )
    db.open_position(
        "NIFTY26NOVFUT", 65, 200.0, mode=LIVE, source="ENGINE"
    )

    class FakeClient:
        mode = PAPER

        def get_ltp(self, _symbol, max_age=None):
            return 110.0

        def instrument_for_symbol(self, _symbol):
            return {"lot_size": 65}

        def data_source(self):
            return "CACHE"

        def auth_status(self):
            return {"authenticated": True}

    class FakeFeed:
        def status(self):
            return {"last_price": 95.0, "last_candle_close": 96.0}

    client = FakeClient()
    manager = OrderManager(client)
    monkeypatch.setattr(order_manager, "is_market_open", lambda: False)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(gift_nifty, "get_feed", lambda: FakeFeed())
    monkeypatch.setattr(bot_app, "get_order_manager", lambda: manager)
    monkeypatch.setattr(bot_app, "get_client", lambda: client)

    portfolio = bot_app.api_portfolio()
    metrics = manager.portfolio()
    position = bot_app._enrich_positions(db.get_open_positions())[0]
    active_positions = bot_app.api_positions(status="open")

    # Marked at the future's 110, not the GIFT index's 95.
    assert position["mark_price"] == 110.0
    assert metrics["prices"]["NIFTY26NOVFUT"] == 110.0
    assert position["unrealized_pnl"] == 650.0
    assert portfolio["unrealized_pnl"] == 650.0
    assert portfolio["total_pnl"] == 650.0
    assert portfolio["equity"] == 1_000_650.0
    assert len(metrics["open_positions"]) == 1
    assert active_positions["count"] == 1
    assert active_positions["positions"][0]["mode"] == PAPER


def test_paper_engine_can_fill_after_hours_but_webhook_cannot(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(order_manager, "is_market_open", lambda: False)
    monkeypatch.setattr(
        order_manager,
        "market_status",
        lambda: {"reason": "Post-market"},
    )

    class FakeClient:
        mode = PAPER

        def get_ltp(self, _symbol, max_age=None):
            return 25_000.0

        def place_order(self, **kwargs):
            return {
                "order_id": "PAPER-TEST",
                "status": "COMPLETE",
                "filled_quantity": kwargs["quantity"],
                "average_price": kwargs["price"],
            }

        def data_source(self):
            return "CACHE"

    manager = OrderManager(FakeClient())
    payload = {
        "action": "BUY",
        "symbol": "NIFTY26NOVFUT",
        "qty_lots": 1,
        "price": 24_418.5,
        "paper_after_hours": True,
        "timestamp": db.iso(),
        "signal_id": "after-hours-paper-test",
    }

    result = manager.process_signal(payload, source="ENGINE")
    sell_payload = {
        **payload,
        "action": "SELL",
        "price": 24_500.0,
        "signal_id": "after-hours-paper-sell-test",
    }
    flipped = manager.process_signal(sell_payload, source="ENGINE")
    rejected = manager.process_signal({**payload, "signal_id": "webhook-after-hours-test"}, source="WEBHOOK")
    open_positions = db.get_open_positions(mode=PAPER)

    assert result.ok
    assert result.quantity == 65
    # Filled at the *future's* quote (25,000), not the payload's 24,418.5. The
    # payload price is a GIFT/index level and booking it wrote the index/future
    # basis into the row as P&L that no trade had produced.
    assert result.price == 25_000.05
    assert flipped.ok
    assert flipped.quantity == 65
    assert len(open_positions) == 1
    assert open_positions[0]["position_key"] == -1
    assert open_positions[0]["quantity"] == 65
    assert not rejected.ok
    assert "outside market hours" in rejected.reason.lower()


def test_feed_connect_is_only_true_after_subscription_succeeds() -> None:
    class FakeSocket:
        MODE_LTP = "ltp"

        def subscribe(self, tokens):
            assert tokens == [123]

        def set_mode(self, mode, tokens):
            raise RuntimeError("subscription rejected")

    feed = GiftNiftyFeed("1minute")
    feed.token = 123
    feed.connected = True
    feed._handle_connect(FakeSocket(), {})

    status = feed.status()
    assert not status["connected"]
    assert "subscription rejected" in status["error"]


def test_feed_uses_runtime_client_token(monkeypatch) -> None:
    received: list[tuple[str, str]] = []

    class FakeTicker:
        def __init__(self, api_key, access_token, reconnect):
            received.append((api_key, access_token))

        def connect(self, threaded=False):
            pass

        def close(self):
            pass

    client = SimpleNamespace(sdk_ready=True, access_token="fresh-token", api_key="key")
    monkeypatch.setattr(gift_nifty, "get_client", lambda: client)
    monkeypatch.setitem(sys.modules, "kiteconnect", SimpleNamespace(KiteTicker=FakeTicker))

    feed = GiftNiftyFeed("1minute")
    feed.token = 123
    assert feed.start()
    assert feed._thread is not None
    feed._thread.join(timeout=1)
    assert received == [("key", "fresh-token")]


def test_feed_reports_running_when_reactor_is_reused(monkeypatch) -> None:
    """A restart reuses the running reactor and creates no websocket_thread.

    The SDK only starts its own thread when ``reactor.running`` is False, so on
    the second connect() a fresh KiteTicker has no ``websocket_thread`` at all
    and the wrapper thread has already exited. Liveness has to come from the
    reactor, or the dashboard reports a healthy feed as stopped.
    """
    monkeypatch.setattr(gift_nifty, "_reactor_running", lambda: True)

    feed = GiftNiftyFeed("1minute")
    feed._ticker = SimpleNamespace()  # no websocket_thread, as after a restart
    feed._thread = None
    feed.connected = True

    status = feed.status()
    assert status["running"]
    assert not status["starting"]


def test_feed_reports_stopped_after_explicit_stop(monkeypatch) -> None:
    """The reactor outlives stop(), so it alone must not imply a live feed."""
    monkeypatch.setattr(gift_nifty, "_reactor_running", lambda: True)

    feed = GiftNiftyFeed("1minute")
    feed._ticker = SimpleNamespace()
    feed.stop()

    status = feed.status()
    assert not status["running"]
    assert not status["starting"]


def test_feed_reports_stopped_when_reactor_is_down(monkeypatch) -> None:
    monkeypatch.setattr(gift_nifty, "_reactor_running", lambda: False)

    feed = GiftNiftyFeed("1minute")
    feed._ticker = SimpleNamespace()
    feed._thread = None

    assert not feed.status()["running"]


def test_reactor_running_is_false_without_twisted(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "twisted.internet", None)

    assert gift_nifty._reactor_running() is False


def test_runtime_token_arms_live_configuration(monkeypatch) -> None:
    settings = Settings(api_key="key", api_secret="secret", access_token="")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "fresh-token")

    assert settings.is_live_configured
    assert KiteClient(api_key="").access_token == "fresh-token"


def test_startup_uses_valid_token_to_start_feed(monkeypatch) -> None:
    called: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(bot_app.kite_auth, "token_status", lambda: {"state": "valid", "valid": True})
    monkeypatch.setattr(
        bot_app,
        "_start_gift_feed",
        lambda: called.append("start") or {"start_requested": True},
    )
    monkeypatch.setattr(bot_app.webbrowser, "open", lambda url, new=0: opened.append(url) or True)

    bot_app._connect_kite_on_startup()

    assert called == ["start"]
    assert opened == []


def test_startup_opens_zerodha_login_when_token_is_missing(monkeypatch) -> None:
    opened: list[tuple[str, int]] = []
    monkeypatch.setattr(
        bot_app.kite_auth,
        "token_status",
        lambda: {"state": "no_token", "valid": False, "login_url": "https://kite.example/login"},
    )
    monkeypatch.setattr(
        bot_app.webbrowser,
        "open",
        lambda url, new=0: opened.append((url, new)) or True,
    )
    monkeypatch.setattr(
        bot_app,
        "_start_gift_feed",
        lambda: (_ for _ in ()).throw(AssertionError("feed cannot start without a token")),
    )

    bot_app._connect_kite_on_startup()

    assert opened == [("https://kite.example/login", 2)]


def test_zerodha_redirect_url_has_a_served_callback_route(monkeypatch) -> None:
    redirect_url = "http://127.0.0.1:9000/zerodha/callback"
    monkeypatch.setattr(
        kite_auth,
        "settings",
        SimpleNamespace(
            kite_redirect_url=redirect_url,
            webhook_host="127.0.0.1",
            webhook_port=9000,
        ),
    )

    assert kite_auth.callback_url() == redirect_url
    paths = {route.path for route in bot_app.app.routes}
    assert "/zerodha/callback" in paths
    assert "/kite/callback" in paths


def test_relative_database_path_resolves_inside_bot_directory(monkeypatch) -> None:
    monkeypatch.setenv("DB_PATH", "paper_trading.db")

    assert config._env_path("DB_PATH", "") == str(BASE_DIR / "paper_trading.db")


def test_front_month_contract_comes_from_exchange_instruments(monkeypatch) -> None:
    client = KiteClient.__new__(KiteClient)
    instruments = [
        {
            "instrument_type": "FUT",
            "tradingsymbol": "NIFTY26DEC_FUT",
            "expiry": date(2026, 12, 29),
            "lot_size": 65,
        },
        {
            "instrument_type": "FUT",
            "tradingsymbol": "NIFTY26NOV_FUT",
            "expiry": date(2026, 11, 24),
            "lot_size": 65,
        },
    ]
    monkeypatch.setattr(client, "_load_instruments", lambda: instruments)

    contract = client.front_month_contract(datetime(2026, 11, 5, 12, 0, tzinfo=IST))

    assert contract is not None
    assert contract["tradingsymbol"] == "NIFTY26NOV_FUT"
    assert contract["lot_size"] == 65


def test_unresolved_live_order_is_cancelled_or_marked_pending() -> None:
    class FakeKite:
        VARIETY_REGULAR = "regular"

        def __init__(self):
            self.status = "OPEN"
            self.cancelled = False

        def order_history(self, order_id):
            return [{"status": self.status, "filled_quantity": 0}]

        def cancel_order(self, variety, order_id):
            self.cancelled = True
            self.status = "CANCELLED"

    client = KiteClient.__new__(KiteClient)
    client._kite = FakeKite()
    result = client._wait_for_fill("order-1", timeout=0)

    assert client._kite.cancelled
    assert result["status"] == "CANCELLED"
    assert result["filled_quantity"] == 0


def test_order_submission_network_error_is_not_reported_as_rejected() -> None:
    class FakeKite:
        VARIETY_REGULAR = "regular"
        EXCHANGE_NFO = "NFO"
        TRANSACTION_TYPE_BUY = "BUY"
        PRODUCT_NRML = "NRML"
        ORDER_TYPE_MARKET = "MARKET"
        VALIDITY_DAY = "DAY"

        def place_order(self, **kwargs):
            raise NetworkException("connection timed out")

    client = KiteClient.__new__(KiteClient)
    client._kite = FakeKite()
    client.mode = LIVE

    result = client.place_order("NIFTY", "BUY", 65)

    assert result["status"] == "PENDING"
    assert result["order_id"] is None
    assert "uncertain" in result["message"].lower()


def test_open_ack_does_not_create_position(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)

    class FakeClient:
        mode = PAPER

        def get_ltp(self, symbol, max_age=None):
            return 100.0

        def place_order(self, **kwargs):
            return {
                "order_id": "pending-1",
                "status": "OPEN",
                "filled_quantity": 0,
                "message": "still open",
            }

    manager = OrderManager(FakeClient())
    result = manager._execute_directional({}, "BUY", "TEST", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "not filled" in result.reason.lower()
    assert "open" in result.reason.lower()
    assert db.get_open_positions(mode=PAPER) == []
    assert db.get_pending_orders(PAPER, "TEST")[0]["exchange_order_id"] == "pending-1"


def test_existing_target_lot_is_not_reopened(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    db.open_position("TEST", 65, 100.0, mode=PAPER)

    class FakeClient:
        mode = PAPER

        def place_order(self, **kwargs):
            raise AssertionError("an unchanged target position must not place an order")

    result = OrderManager(FakeClient())._execute_directional({}, "BUY", "TEST", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "already positioned" in result.reason.lower()


def test_rejected_square_off_reports_failure_and_keeps_position(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    db.open_position("TEST", 65, 100.0, mode=PAPER)

    class FakeClient:
        mode = PAPER

        def get_ltp(self, symbol, max_age=None):
            return 100.0

        def place_order(self, **kwargs):
            return {"status": "REJECTED", "message": "market closed"}

    result = OrderManager(FakeClient()).square_off()

    assert not result.ok
    assert "incomplete" in result.reason.lower()
    assert len(db.get_open_positions(mode=PAPER)) == 1


def test_partial_close_reduces_open_quantity(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    position_id = db.open_position("TEST", 75, 100.0, mode=PAPER)

    trade = db.close_position(position_id, 110.0, quantity=25)

    position = db.get_open_positions(mode=PAPER)[0]
    assert position["quantity"] == 50
    assert trade["quantity"] == 25
    assert db.get_trades()[0]["quantity"] == 25


def test_live_position_mismatch_blocks_reconciliation(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: LIVE)
    db.open_position("NIFTY26NOV_FUT", 65, 100.0, mode=LIVE)

    class FakeClient:
        mode = LIVE

        def positions(self, force=False):
            return []

        def nifty_future_instruments(self):
            return [{"tradingsymbol": "NIFTY26NOV_FUT"}]

    message = OrderManager(FakeClient())._live_position_sync_error()

    assert message is not None
    assert "Position mismatch" in message


# ---------------------------------------------------------------------------
# NSE holiday calendar
# ---------------------------------------------------------------------------

def _calendar(monkeypatch, tmp_path, payload=None, *, fail=False):
    """Build a HolidayCalendar whose network call is stubbed."""
    import market_calendar

    if fail:
        def boom(*_args, **_kwargs):
            raise OSError("network down")

        monkeypatch.setattr(market_calendar.urllib.request, "urlopen", boom)
    else:
        class Response:
            def __init__(self, body: bytes) -> None:
                self._body = body

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(
            market_calendar.urllib.request,
            "urlopen",
            lambda *_a, **_k: Response(json.dumps(payload).encode()),
        )
    return market_calendar.HolidayCalendar(cache_path=tmp_path / "nse_holidays.json")


def test_holiday_calendar_parses_the_nse_fo_segment(monkeypatch, tmp_path) -> None:
    calendar = _calendar(
        monkeypatch,
        tmp_path,
        {
            "CM": [{"tradingDate": "01-Jan-2026", "description": "New Year"}],
            "FO": [
                {"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"},
                {"tradingDate": "24-Nov-2026", "description": "Guru Nanak Gurpurb"},
                {"tradingDate": "not-a-date", "description": "junk"},
            ],
        },
    )

    assert calendar.available
    assert calendar.status()["source"] == "nse"
    assert calendar.holiday_name(date(2026, 11, 10)) == "Diwali-Balipratipada"
    assert calendar.holiday_name(date(2026, 11, 24)) == "Guru Nanak Gurpurb"
    # The CM-only date must not leak into the F&O calendar.
    assert calendar.holiday_name(date(2026, 1, 1)) is None
    assert calendar.status()["count"] == 2


def test_holiday_calendar_falls_back_to_cache_when_nse_is_unreachable(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "nse_holidays.json"
    cache.write_text(json.dumps({"holidays": {"2026-11-10": "Diwali-Balipratipada"}}))

    import market_calendar

    def boom(*_args, **_kwargs):
        raise OSError("network down")

    monkeypatch.setattr(market_calendar.urllib.request, "urlopen", boom)
    calendar = market_calendar.HolidayCalendar(cache_path=cache)

    assert calendar.available
    assert calendar.status()["source"] == "cache"
    assert calendar.holiday_name(date(2026, 11, 10)) == "Diwali-Balipratipada"
    assert calendar.status()["error"] is not None


def test_holiday_calendar_reports_unavailable_without_network_or_cache(monkeypatch, tmp_path) -> None:
    calendar = _calendar(monkeypatch, tmp_path, fail=True)

    assert not calendar.available
    assert calendar.status()["source"] == "none"
    assert calendar.holiday_name(date(2026, 11, 10)) is None


def test_market_status_closes_on_a_trading_holiday(monkeypatch, tmp_path) -> None:
    calendar = _calendar(
        monkeypatch,
        tmp_path,
        {"FO": [{"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"}]},
    )
    monkeypatch.setattr(kite_client, "get_calendar", lambda: calendar)

    # 10 Nov 2026 is a Tuesday inside the 09:15-15:30 window.
    status = kite_client.market_status(datetime(2026, 11, 10, 11, 0, tzinfo=IST))

    assert status["is_open"] is False
    assert status["holiday"] == "Diwali-Balipratipada"
    assert "Holiday" in status["reason"]


def test_market_status_is_unverified_without_a_calendar(monkeypatch, tmp_path) -> None:
    calendar = _calendar(monkeypatch, tmp_path, fail=True)
    monkeypatch.setattr(kite_client, "get_calendar", lambda: calendar)

    status = kite_client.market_status(datetime(2026, 11, 10, 11, 0, tzinfo=IST))

    assert status["is_open"] is False
    assert status["calendar_known"] is False
    assert "unverified" in status["reason"]


def test_market_status_opens_on_a_normal_session(monkeypatch, tmp_path) -> None:
    calendar = _calendar(
        monkeypatch,
        tmp_path,
        {"FO": [{"tradingDate": "10-Nov-2026", "description": "Diwali-Balipratipada"}]},
    )
    monkeypatch.setattr(kite_client, "get_calendar", lambda: calendar)

    status = kite_client.market_status(datetime(2026, 11, 11, 11, 0, tzinfo=IST))

    assert status["is_open"] is True
    assert status["calendar_known"] is True


def test_the_suite_has_a_calendar_without_network_access() -> None:
    """Every test must see a trading calendar without reaching NSE.

    ``market_status`` reports the session unverified when no calendar has loaded,
    and an unverified session rejects every order. The cache lives under
    ``bot/data/``, which is gitignored, so a developer machine had one and a CI
    runner did not: ~20 engine tests passed locally and failed in CI on the same
    commit with "Outside market hours (Session unverified)". The suite also
    depended on NSE being reachable, so the same SHA passed one run and failed
    the next.

    The conftest fixture supplies the calendar offline. This pins that it stays
    that way - if it is ever removed, this fails by name instead of as a dozen
    unrelated order rejections.
    """
    calendar = kite_client.get_calendar()

    assert calendar.available, "the test suite must not depend on a live NSE fetch"
    assert calendar.status()["source"] == "cache"
    # A mid-week, non-holiday date is open; that is the state the engine tests
    # assume when they act on a bar.
    status = kite_client.market_status(datetime(2026, 11, 11, 11, 0, tzinfo=IST))
    assert status["calendar_known"] is True
    assert status["is_open"] is True


def _stub_client(calls: dict[str, int]) -> KiteClient:
    """A KiteClient whose SDK calls are counted instead of hitting the network."""
    client = KiteClient(api_key="key", access_token="token")
    client._kite = SimpleNamespace(
        profile=lambda: calls.__setitem__("profile", calls.get("profile", 0) + 1)
        or {"user_id": "AB1234"},
        margins=lambda: calls.__setitem__("margins", calls.get("margins", 0) + 1)
        or {"equity": {"available": {"live_balance": 1000.0}}},
        quote=lambda keys: calls.__setitem__("quote", calls.get("quote", 0) + 1)
        or {keys[0]: {"last_price": 25_000.0, "ohlc": {"close": 24_900.0}}},
    )
    return client


def test_profile_is_cached_between_dashboard_polls() -> None:
    calls: dict[str, int] = {}
    client = _stub_client(calls)

    for _ in range(5):
        assert client.auth_status()["authenticated"] is True

    assert calls["profile"] == 1
    # max_age=0 must always reach the broker (used after a token change).
    client.profile(max_age=0)
    assert calls["profile"] == 2


def test_margins_are_cached_between_dashboard_polls() -> None:
    calls: dict[str, int] = {}
    client = _stub_client(calls)

    for _ in range(5):
        assert client.available_cash() == 1000.0

    assert calls["margins"] == 1


def test_quote_cache_is_shared_but_orders_can_bypass_it() -> None:
    calls: dict[str, int] = {}
    client = _stub_client(calls)

    for _ in range(5):
        assert client.get_ltp("NIFTY26KFUT") == 25_000.0
    assert calls["quote"] == 1

    # An order needs the freshest price, so it must not read the cache.
    client.get_ltp("NIFTY26KFUT", max_age=0)
    assert calls["quote"] == 2


def test_engine_status_reuses_the_verdict_until_a_new_bar(monkeypatch) -> None:
    evaluations: list[int] = []

    class FakeFeed:
        seconds = 300

        def refresh_history(self, force: bool = False):
            return 0

        def candle_snapshot(self):
            return (
                [
                    {"time": f"2026-11-06T22:{minute:02d}:00+05:30", "open": 100.0,
                     "high": 101.0, "low": 99.0, "close": 100.5}
                    for minute in range(20)
                ],
                False,
            )

        def status(self):
            return {}

    def fake_evaluate(candles, _params):
        evaluations.append(len(candles))
        return strategy.StrategyVerdict(strategy.NO_SIGNAL, 100.5, values={"psar": 99.0})

    monkeypatch.setattr(engine, "get_feed", FakeFeed)
    monkeypatch.setattr(engine.strategy, "evaluate", fake_evaluate)
    monkeypatch.setattr(engine, "required_bars", lambda: 2)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": False})
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)

    subject = engine.StrategyEngine()
    for _ in range(5):
        subject.status()

    # Two slots (current + previous), each evaluated once for the same bar.
    assert len(evaluations) == 2


def test_history_fetch_backs_off_after_a_failure(monkeypatch) -> None:
    attempts: list[int] = []

    class FakeClient:
        def historical_data(self, **_kwargs):
            attempts.append(1)
            return []

    monkeypatch.setattr(gift_nifty, "get_client", lambda: FakeClient())
    feed = GiftNiftyFeed("5minute")
    feed.token = 291849

    feed.refresh_history(force=True)
    assert len(attempts) == 1
    assert feed._history_failures == 1

    # A non-forced refresh inside the backoff window must not hit the API.
    feed.refresh_history()
    assert len(attempts) == 1

    # force=True is an explicit operator action and always retries.
    feed.refresh_history(force=True)
    assert len(attempts) == 2
    assert feed._history_failures == 2


def test_failed_quote_is_cached_so_a_dead_token_is_not_retried() -> None:
    calls: list[int] = []

    def boom(_keys):
        calls.append(1)
        raise kite_client.TokenException("token expired")

    client = KiteClient(api_key="key", access_token="token")
    client._kite = SimpleNamespace(quote=boom)

    for _ in range(5):
        assert client.get_ltp("NIFTY26KFUT") is None

    # One attempt, then the failure is served from cache.
    assert len(calls) == 1
    # An order still forces a fresh attempt.
    assert client.get_ltp("NIFTY26KFUT", max_age=0) is None
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Dashboard authentication
# ---------------------------------------------------------------------------
def test_control_endpoints_require_a_session(monkeypatch) -> None:
    """The control API can arm live trading and square off positions, so an
    unauthenticated caller must not reach it even from loopback."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    client = TestClient(bot_app.app)

    for path, body in (
        ("/api/panic", {"confirm": True}),
        ("/api/toggle-mode", {"mode": "LIVE", "confirm": True}),
        ("/api/pause", {"paused": True}),
        ("/api/kite/feed/reconnect", {}),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 401, path
        assert response.json()["detail"] == "Authentication required"


def test_read_endpoints_require_a_session(monkeypatch) -> None:
    """Positions, orders and the log tail are account data, not public."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    client = TestClient(bot_app.app)

    for path in ("/api/status", "/api/positions", "/api/orders", "/api/logs"):
        assert client.get(path).status_code == 401, path


def test_health_and_login_stay_reachable(monkeypatch) -> None:
    """The container healthcheck has no cookie, and the login page cannot
    require a session or nobody could ever sign in."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    client = TestClient(bot_app.app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/api/auth/status").status_code == 200
    assert client.get("/static/styles.css").status_code == 200


def test_dashboard_redirects_to_login_instead_of_401(monkeypatch) -> None:
    """A browser navigating to / must land on the login page, not a bare 401."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    response = TestClient(bot_app.app).get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_grants_access_and_logout_revokes_it(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    client = TestClient(bot_app.app)

    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    assert client.get("/api/status").status_code == 401

    ok = client.post("/api/auth/login", json={"password": "hunter2"})
    assert ok.status_code == 200
    assert client.get("/api/status").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/status").status_code == 401


def test_session_cookie_is_httponly_and_samesite_strict(monkeypatch) -> None:
    """HttpOnly keeps the token away from script; SameSite=Strict is what stops
    a cross-site page from riding the session."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    response = TestClient(bot_app.app).post(
        "/api/auth/login", json={"password": "hunter2"}
    )

    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_forged_session_cookie_is_rejected(monkeypatch) -> None:
    """The cookie is HMAC-signed, so a guessed or edited value must not work."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    client = TestClient(bot_app.app)

    for forged in ("", "1.abc.deadbeef", "9999999999.abc.deadbeef"):
        client.cookies.set("belaku_session", forged)
        assert client.get("/api/status").status_code == 401, forged


def test_login_is_rate_limited(monkeypatch) -> None:
    """Without a limit the password is brute-forceable at network speed."""
    from fastapi.testclient import TestClient

    import dashboard_auth

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    dashboard_auth.reset_state()
    client = TestClient(bot_app.app)

    for _ in range(dashboard_auth.MAX_ATTEMPTS):
        assert client.post("/api/auth/login", json={"password": "no"}).status_code == 401

    blocked = client.post("/api/auth/login", json={"password": "hunter2"})
    assert blocked.status_code == 429
    dashboard_auth.reset_state()


def test_dashboard_is_open_when_no_password_is_set(monkeypatch) -> None:
    """Preserves local development: no password means no gate."""
    from fastapi.testclient import TestClient

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    client = TestClient(bot_app.app)

    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "auth_required": False,
        "authenticated": True,
    }


def test_session_token_expires(monkeypatch) -> None:
    import dashboard_auth

    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    token = dashboard_auth.issue_token(now=1000.0)

    assert dashboard_auth.verify_token(token, now=1000.0)
    assert not dashboard_auth.verify_token(
        token, now=1000.0 + dashboard_auth.SESSION_TTL_SECONDS + 1
    )


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
def _watchdog(**kwargs):
    import watchdog

    sent: list[tuple[str, dict]] = []
    wd = watchdog.Watchdog(
        webhook_url="https://example.invalid/hook",
        sender=lambda url, payload: sent.append((url, payload)) or True,
        **kwargs,
    )
    return wd, sent


def test_watchdog_alerts_on_a_stale_feed() -> None:
    """A connected feed with no ticks is the failure a liveness probe misses."""
    wd, _ = _watchdog(stale_tick_seconds=180.0)

    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 600.0},
        engine={},
    )

    assert [a.kind for a in alerts] == ["STALE_FEED"]
    assert alerts[0].severity == "critical"


def test_watchdog_ignores_a_stale_feed_when_the_market_is_closed() -> None:
    """Overnight and on holidays there are no ticks, and that is normal."""
    wd, _ = _watchdog(stale_tick_seconds=180.0)

    alerts = wd.check(
        market_open=False,
        feed={"connected": True, "tick_age_seconds": 40000.0},
        engine={},
    )

    assert alerts == []


def test_watchdog_alerts_on_a_disconnected_feed() -> None:
    wd, _ = _watchdog()

    alerts = wd.check(
        market_open=True,
        feed={"connected": False, "starting": False, "error": "token expired"},
        engine={},
    )

    assert [a.kind for a in alerts] == ["FEED_DOWN"]
    assert "token expired" in alerts[0].detail


def test_watchdog_does_not_alert_while_the_feed_is_starting() -> None:
    """A feed that is still connecting is not a failure."""
    wd, _ = _watchdog()

    alerts = wd.check(
        market_open=True,
        feed={"connected": False, "starting": True},
        engine={},
    )

    assert alerts == []


def test_watchdog_alerts_on_a_stop_out_and_a_pause() -> None:
    """A stop-out is reported, but as a warning - the strategy is still running."""
    wd, _ = _watchdog()

    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1.0},
        engine={
            "risk_breach": {"detail": "stop-loss hit"},
            "paused": True,
            "paused_reason": "MANUAL",
        },
    )

    assert {a.kind for a in alerts} == {"STOP_OUT", "PAUSED"}
    stop_out = next(a for a in alerts if a.kind == "STOP_OUT")
    assert stop_out.severity == "warning"


def test_watchdog_requires_sustained_failure_before_alerting() -> None:
    """One slow poll must not page anyone, or the alerts get muted."""
    import watchdog

    wd, sent = _watchdog()
    unhealthy = dict(
        market_open=True,
        feed={"connected": False, "starting": False, "error": "down"},
        engine={},
    )

    for _ in range(watchdog.FAILURES_BEFORE_ALERT - 1):
        wd.run_once(**unhealthy)
    assert sent == []

    wd.run_once(**unhealthy)
    assert len(sent) == 1


def test_watchdog_rate_limits_repeated_alerts() -> None:
    """A persistent fault must not send an alert every cycle."""
    import watchdog

    wd, sent = _watchdog()
    unhealthy = dict(
        market_open=True,
        feed={"connected": False, "starting": False, "error": "down"},
        engine={},
    )

    for _ in range(watchdog.FAILURES_BEFORE_ALERT + 5):
        wd.run_once(**unhealthy)

    assert len(sent) == 1


def test_watchdog_reports_healthy_when_nothing_is_wrong() -> None:
    wd, sent = _watchdog()

    alerts = wd.run_once(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 2.0},
        engine={},
    )

    assert alerts == []
    assert wd.state.healthy is True
    assert sent == []


def test_watchdog_survives_a_failing_webhook() -> None:
    """A broken alert channel must not take the bot down with it."""
    import watchdog

    def explode(url, payload):
        raise RuntimeError("network down")

    wd = watchdog.Watchdog(webhook_url="https://example.invalid/hook", sender=explode)
    unhealthy = dict(
        market_open=True,
        feed={"connected": False, "starting": False, "error": "down"},
        engine={},
    )

    for _ in range(watchdog.FAILURES_BEFORE_ALERT):
        wd.run_once(**unhealthy)

    assert wd.state.alerts_sent == 1


# ---------------------------------------------------------------------------
# Execution future ticks (shown beside GIFT Nifty on the dashboard)
# ---------------------------------------------------------------------------
def test_execution_ticks_do_not_reach_the_gift_candle_builder() -> None:
    """The two prices differ by the basis, so routing an execution tick into
    the builder would corrupt the signal series rather than fail loudly."""
    feed = GiftNiftyFeed("1minute")
    feed.token = 111
    feed.set_exec_token("NIFTY26KFUT", 222)

    feed._handle_ticks(None, [
        {"instrument_token": 222, "last_price": 25060.0},
    ])

    assert feed.last_price is None
    assert feed.tick_count == 0
    assert feed._builder.current is None
    assert feed.exec_last_price == 25060.0
    assert feed.exec_ticks == 1


def test_gift_and_execution_ticks_are_kept_apart() -> None:
    feed = GiftNiftyFeed("1minute")
    feed.token = 111
    feed.set_exec_token("NIFTY26KFUT", 222)

    feed._handle_ticks(None, [
        {"instrument_token": 111, "last_price": 25000.0},
        {"instrument_token": 222, "last_price": 25060.0},
        {"instrument_token": 111, "last_price": 25001.0},
    ])

    assert feed.last_price == 25001.0
    assert feed.tick_count == 2
    assert feed.exec_last_price == 25060.0
    assert feed.exec_ticks == 1


def test_a_tick_without_a_token_counts_as_gift() -> None:
    """Every existing caller and test sends a bare tick, so the default must
    stay GIFT - the series that must never be disturbed."""
    feed = GiftNiftyFeed("1minute")
    feed.token = 111
    feed.set_exec_token("NIFTY26KFUT", 222)

    feed._handle_ticks(None, [{"last_price": 25000.0}])

    assert feed.last_price == 25000.0
    assert feed.exec_last_price is None


def test_exec_quote_reports_provenance_and_the_basis_inputs() -> None:
    feed = GiftNiftyFeed("1minute")
    feed.token = 111
    feed.set_exec_token("NIFTY26KFUT", 222)
    feed._handle_ticks(None, [
        {"instrument_token": 111, "last_price": 25000.0},
        {"instrument_token": 222, "last_price": 25060.0},
    ])

    exec_quote = feed.status()["exec"]

    assert exec_quote["symbol"] == "NIFTY26KFUT"
    assert exec_quote["price"] == 25060.0
    assert exec_quote["source"] == "tick"
    assert exec_quote["subscribed"] is True
    assert exec_quote["ticks"] == 1
    assert exec_quote["age_seconds"] is not None


def test_exec_quote_is_safe_before_any_tick() -> None:
    feed = GiftNiftyFeed("1minute")

    exec_quote = feed.status()["exec"]

    assert exec_quote["price"] is None
    assert exec_quote["age_seconds"] is None
    assert exec_quote["source"] == "none"
    assert exec_quote["subscribed"] is False


def test_unresolved_execution_token_falls_back_to_polling(monkeypatch) -> None:
    """A future that cannot be resolved must not leave the card blank: the
    mark loop polls a quote instead of relying on a subscription."""
    feed = GiftNiftyFeed("1minute")
    monkeypatch.setattr(feed, "current_exec_symbol", lambda: "NIFTY26KFUT")

    feed.set_exec_token("NIFTY26KFUT", None)
    assert feed.exec_source == "poll"

    monkeypatch.setattr(
        gift_nifty, "get_client", lambda: SimpleNamespace(get_ltp=lambda s: 25060.0)
    )
    feed.refresh_exec_quote()

    assert feed.exec_last_price == 25060.0
    assert feed.status()["exec"]["source"] == "poll"


def test_tick_source_wins_over_a_later_poll(monkeypatch) -> None:
    """Once real ticks arrive, a polled quote should not relabel the source."""
    feed = GiftNiftyFeed("1minute")
    feed.token = 111
    feed.set_exec_token("NIFTY26KFUT", 222)
    feed._handle_ticks(None, [{"instrument_token": 222, "last_price": 25060.0}])

    monkeypatch.setattr(
        gift_nifty, "get_client", lambda: SimpleNamespace(get_ltp=lambda s: 25061.0)
    )
    feed.refresh_exec_quote()

    assert feed.exec_last_price == 25061.0
    assert feed.status()["exec"]["source"] == "tick"


def test_future_symbol_matches_kites_instrument_dump() -> None:
    """Kite's NFO dump uses three-letter months (NIFTY26NOVFUT). The older
    single-letter form (NIFTY26KFUT) matched nothing, so the token could not be
    resolved and PAPER mode reported a contract that does not exist."""
    from kite_client import future_symbol

    assert future_symbol(datetime(2026, 11, 9, tzinfo=IST)) == "NIFTY26NOVFUT"
    assert future_symbol(datetime(2026, 12, 9, tzinfo=IST)) == "NIFTY26DECFUT"
    assert future_symbol(datetime(2026, 1, 9, tzinfo=IST)) == "NIFTY26JANFUT"
    # No single-letter month code may survive anywhere in the symbol.
    symbol = future_symbol(datetime(2026, 5, 9, tzinfo=IST))
    assert symbol == "NIFTY26MAYFUT"


def test_future_symbol_rolls_to_the_next_contract(monkeypatch) -> None:
    """After the front-month expiry the next contract becomes current, and the
    months_ahead form must agree with it."""
    from kite_client import future_symbol

    now = datetime(2026, 11, 9, tzinfo=IST)
    assert future_symbol(now, months_ahead=0) == "NIFTY26NOVFUT"
    assert future_symbol(now, months_ahead=1) == "NIFTY26DECFUT"
    assert future_symbol(now, months_ahead=2) == "NIFTY27JANFUT"


# ---------------------------------------------------------------------------
# Candle interval isolation
# ---------------------------------------------------------------------------
def test_candles_of_different_intervals_do_not_mix(monkeypatch, tmp_path) -> None:
    """A 5-minute and a 15-minute bucket can both start at 10:00 but cover
    different windows, so `start` alone cannot separate them. Without the
    interval column a timeframe change would splice two series together and the
    indicators would run on garbage."""
    use_test_database(monkeypatch, tmp_path)
    start = "2026-11-06T10:00:00+05:30"

    db.record_gift_candle(
        start=start, open_=100.0, high=102.0, low=99.0, close=101.0, interval="5minute"
    )
    db.record_gift_candle(
        start=start, open_=200.0, high=205.0, low=198.0, close=204.0, interval="15minute"
    )

    assert [r["close"] for r in db.get_gift_candles(interval="5minute")] == [101.0]
    assert [r["close"] for r in db.get_gift_candles(interval="15minute")] == [204.0]
    # Unfiltered still returns everything, which is what the count uses.
    assert len(db.get_gift_candles()) == 2


def test_feed_only_loads_its_own_interval(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    db.record_gift_candle(
        start="2026-11-06T09:00:00+05:30",
        open_=100.0, high=101.0, low=99.0, close=100.5, interval="5minute",
    )
    db.record_gift_candle(
        start="2026-11-06T10:00:00+05:30",
        open_=200.0, high=201.0, low=199.0, close=200.5, interval="15minute",
    )

    feed = GiftNiftyFeed("15minute")
    feed.load_persisted()

    times = [c["time"] for c in feed.candles()]
    assert times == ["2026-11-06T10:00:00+05:30"]


def test_default_candle_interval_is_sixty_minutes() -> None:
    """The strategy timeframe is configuration, but the shipped default must
    match the .env.example value so a fresh install is self-consistent.

    Sixty minutes is the shipped timeframe: the bars are the ones the strategy
    was measured on, and the finer timeframes lost more to charges per trade
    without gaining accuracy (see the Backtesting page).
    """
    from config import Settings

    assert Settings().gift_candle_interval == "60minute"


def test_interval_to_seconds_maps_fifteen_minutes() -> None:
    from gift_nifty import interval_to_seconds

    assert interval_to_seconds("15minute") == 900
    assert interval_to_seconds("5minute") == 300
    assert interval_to_seconds("1hour") == 3600



def test_settings_exposes_bar_length_in_seconds() -> None:
    """The staleness limit is derived from the bar length, so the length has to
    be reachable as a number without repeating the interval table."""
    assert Settings(gift_candle_interval="15minute").gift_candle_interval_seconds == 900
    assert Settings(gift_candle_interval="5minute").gift_candle_interval_seconds == 300
    assert Settings(gift_candle_interval="1hour").gift_candle_interval_seconds == 3600


def test_signal_max_age_outlives_the_bar(monkeypatch) -> None:
    """A signal is stamped with the *close* of its bar, so it is already one bar
    old on arrival. A fixed 300s limit rejected the engine's own fresh signals
    once the timeframe moved to 15 minutes (they arrived ~717s old)."""
    eng = engine.StrategyEngine.__new__(engine.StrategyEngine)

    def with_bar(seconds: int) -> int:
        monkeypatch.setattr(
            engine, "settings", SimpleNamespace(gift_candle_interval_seconds=seconds)
        )
        return eng._signal_max_age()

    assert with_bar(300) == 450      # 5-minute bars
    assert with_bar(900) == 1350     # 15-minute bars, the shipped default
    assert with_bar(60) == 300       # floor: the limit only ever loosens


def test_engine_payload_carries_the_bar_scaled_max_age(monkeypatch) -> None:
    """The broker falls back to its own 300s default when the payload omits the
    limit, so the engine must send the scaled one."""
    captured = {}

    class FakeResult:
        ok = True
        reason = "simulated"

        def to_dict(self):
            return {"ok": True, "action": "BUY"}

    class FakeManager:
        paused = False

        def current_symbol(self):
            return "NIFTY26NOVFUT"

        def process_signal(self, payload, source, strategy_id=None, mode=None):
            captured.update(payload=payload)
            return FakeResult()

    monkeypatch.setattr(
        engine, "settings",
        SimpleNamespace(gift_candle_interval_seconds=900, default_lots=1),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": False, "reason": "Post-market"})
    monkeypatch.setattr(engine, "get_order_manager", lambda: FakeManager())

    engine.StrategyEngine().execute(
        "BUY", "15m bar closed",
        paper_reference_price=25_100.0,
        signal_timestamp=datetime.now(IST),
    )
    assert captured["payload"]["max_age_seconds"] == 1350


# ---------------------------------------------------------------------------
# Shadow paper book
#
# The mirror is graded against real broker fills, so these tests pin the
# behaviour that makes the comparison mean something: it copies the live
# quantity rather than reconstructing it, it prices from the actual fill when
# one is fresh (and says so when it is not), and it cannot leak into the live
# ledger or stop trading.
# ---------------------------------------------------------------------------
def _mirror(monkeypatch, tmp_path, *, enabled=True, lot_size=65):
    """A mirror on its own database, with a pinned size and no broker access."""
    import order_manager
    import paper_mirror as pm

    use_test_database(monkeypatch, tmp_path)
    # Settings is a frozen dataclass, so the modules' references are swapped for
    # stand-ins rather than mutated - the same approach use_test_database takes.
    stub = SimpleNamespace(
        product="NRML",
        order_type="MARKET",
        tick_size=0.05,
        slippage_ticks=1.0,
        lot_size=lot_size,
        paper_mirror_balance=200_000.0,
        virtual_balance=1_000_000.0,
    )
    monkeypatch.setattr(pm, "settings", stub)
    monkeypatch.setattr(order_manager, "settings", stub)
    return pm.PaperMirror(
        path=str(tmp_path / "mirror.db"),
        mirror=enabled,
        lot_size=lot_size,
    )


def _live_position(monkeypatch, symbol="NIFTY26NOVFUT", quantity=65):
    """Put a position in the live ledger, which is what the mirror copies."""
    db.open_position(symbol, quantity, 25_000.0, mode=LIVE, source="ENGINE")
    monkeypatch.setenv("EXECUTION_MODE", LIVE)


def test_mirror_does_nothing_unless_armed(monkeypatch, tmp_path) -> None:
    """Off by default, and inert outside LIVE - it must never fire on a paper run."""
    mirror = _mirror(monkeypatch, tmp_path, enabled=False)
    monkeypatch.setenv("EXECUTION_MODE", LIVE)

    assert mirror.reconcile() is None
    assert mirror.status()["enabled"] is False
    assert not (tmp_path / "mirror.db").exists()

    # Configured but not live is the other half: a paper session is already the
    # paper book, so a shadow copy of it would be meaningless.
    import paper_mirror as pm

    mirror = pm.PaperMirror(path=str(tmp_path / "mirror.db"), mirror=True, lot_size=65)
    monkeypatch.setenv("EXECUTION_MODE", PAPER)
    assert mirror.reconcile() is None
    assert "LIVE" in mirror.status()["reason"]


def test_mirror_copies_the_live_quantity_and_direction(monkeypatch, tmp_path) -> None:
    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=-65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0

    mirror.reconcile()

    positions = db.get_open_positions(str(tmp_path / "mirror.db"), mode=PAPER)
    assert len(positions) == 1
    assert positions[0]["symbol"] == "NIFTY26NOVFUT"
    assert positions[0]["quantity"] == 65
    assert positions[0]["position_key"] == -1
    assert positions[0]["source"] == "MIRROR"


def test_mirror_matches_a_flip_without_a_broker_round_trip(monkeypatch, tmp_path) -> None:
    """A flip is what the live book does; the mirror must follow it out and back."""
    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0
    mirror.reconcile()

    live = db.get_open_positions(mode=LIVE)[0]
    db.close_position(live["id"], 25_100.0, exit_reason="FLIP")
    db.open_position("NIFTY26NOVFUT", -65, 25_100.0, mode=LIVE, source="ENGINE")
    mirror._prices["NIFTY26NOVFUT"] = 25_100.0
    mirror.reconcile()

    net = mirror.status()["mirror_net"]
    assert net == {"NIFTY26NOVFUT": -65}
    assert mirror.status()["in_sync"] is True
    trades = db.get_trades(path=str(tmp_path / "mirror.db"))
    assert len(trades) == 1 and trades[0]["exit_reason"] == "MIRROR"


def test_mirror_prices_from_the_real_fill_not_a_later_quote(monkeypatch, tmp_path) -> None:
    """The whole point: the gap measured is the slippage model's error.

    With a fresh broker fill at 25_010 the paper entry must be that price plus
    the model's own one-tick slippage, and must record the fill lag. A quote of
    25_000 is deliberately supplied to prove it is not what was used.
    """
    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0
    mirror.note_fill(
        symbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=65,
        price=25_010.0,
    )

    mirror.reconcile()

    position = db.get_open_positions(str(tmp_path / "mirror.db"), mode=PAPER)[0]
    assert position["entry_price"] == 25_010.05  # fill price + 1 tick slippage
    notes = json.loads(position["notes"])
    assert notes["basis"] == "live_fill"
    assert notes["fill_lag_seconds"] is not None
    assert notes["live_entry_price"] == 25_000.0


def test_mirror_labels_a_fill_it_could_not_match(monkeypatch, tmp_path) -> None:
    """A stale fill is booked from the quote but never passed off as a fill match.

    Without the label the dataset would silently mix "measured against a real
    fill" with "measured against a quote", which is the one distinction the
    comparison exists to make.
    """
    import paper_mirror as pm

    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0
    mirror.note_fill(
        symbol="NIFTY26NOVFUT",
        transaction_type="BUY",
        quantity=65,
        price=25_010.0,
    )
    # Age the fill past the tolerance rather than sleeping for it.
    for fill in mirror._recent_fills:
        fill["at"] -= pm.FILL_TOLERANCE_SECONDS + 1

    mirror.reconcile()

    position = db.get_open_positions(str(tmp_path / "mirror.db"), mode=PAPER)[0]
    assert json.loads(position["notes"])["basis"] == "live_fill_stale"
    assert position["entry_price"] == 25_000.05  # the quote, not the old fill


def test_mirror_writes_only_to_its_own_database(monkeypatch, tmp_path) -> None:
    """The shadow book must never be able to touch the live ledger."""
    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0

    mirror.reconcile()

    live = db.get_open_positions(mode=LIVE)
    assert len(live) == 1 and live[0]["source"] == "ENGINE"
    assert db.get_trades() == []
    assert db.get_trades(path=str(tmp_path / "mirror.db")) == []


def test_mirror_records_a_failure_instead_of_raising(monkeypatch, tmp_path) -> None:
    """A broken shadow book reports itself; it cannot take the bot down with it."""
    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0
    monkeypatch.setattr(
        db, "open_position", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    )

    status = mirror.reconcile()

    assert status is not None
    assert status["consecutive_failures"] >= 1
    assert "disk full" in status["last_error"]
    assert status["in_sync"] is False


def test_mirror_skips_a_live_quantity_below_one_lot(monkeypatch, tmp_path) -> None:
    """A partial fill under a lot cannot be mirrored, and must be reported."""
    mirror = _mirror(monkeypatch, tmp_path, lot_size=65)
    _live_position(monkeypatch, quantity=30)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0

    status = mirror.reconcile()

    assert db.get_open_positions(str(tmp_path / "mirror.db"), mode=PAPER) == []
    assert "under one lot" in status["last_error"]


def test_mirror_marks_itself_to_market(monkeypatch, tmp_path) -> None:
    """Unrealised PnL has to be real, or the comparison is not like for like."""
    mirror = _mirror(monkeypatch, tmp_path)
    _live_position(monkeypatch, quantity=65)
    mirror._prices["NIFTY26NOVFUT"] = 25_000.0
    mirror.reconcile()

    mirror.mark({"NIFTY26NOVFUT": 25_100.0})

    position = db.get_open_positions(str(tmp_path / "mirror.db"), mode=PAPER)[0]
    assert position["last_price"] == 25_100.0
    # 65 units x 100 points, less the round-trip cost the shadow book booked.
    unrealized = mirror.status()["comparison"]["mirror"]["unrealized_pnl"]
    assert 6_400.0 < unrealized < 6_500.0, unrealized


def test_mark_open_positions_refreshes_last_price(monkeypatch, tmp_path) -> None:
    """The stop-loss reads `last_price`, so a mark that never lands blinds it."""
    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    touched = db.mark_open_positions(PAPER, {"NIFTY26NOVFUT": 24_900.0})

    assert touched == 1
    assert db.get_open_positions(mode=PAPER)[0]["last_price"] == 24_900.0


def test_mirror_endpoint_reports_a_disabled_mirror(monkeypatch, tmp_path) -> None:
    """Off is a normal answer, not an error - the dashboard has to render it."""
    from fastapi.testclient import TestClient

    import bot as bot_app
    import paper_mirror as pm

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app,
        "get_mirror",
        lambda: pm.PaperMirror(path=str(tmp_path / "mirror.db"), mirror=False),
    )

    response = TestClient(bot_app.app).get("/api/mirror")

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["reason"] == "PAPER_MIRROR is off"


def test_status_carries_the_mirror_block_and_survives_a_broken_mirror(
    monkeypatch, tmp_path
) -> None:
    """The mirror is a side-car: its failure must not take /api/status with it."""
    from fastapi.testclient import TestClient

    import bot as bot_app

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")

    client = TestClient(bot_app.app)
    payload = client.get("/api/status").json()
    assert payload["mirror"]["enabled"] is False

    class Exploding:
        def status(self):
            raise RuntimeError("shadow book on fire")

    monkeypatch.setattr(bot_app, "get_mirror", lambda: Exploding())
    payload = client.get("/api/status").json()

    assert payload["mirror"]["enabled"] is False
    assert "shadow book on fire" in payload["mirror"]["reason"]


def test_status_reports_the_build_that_is_serving(monkeypatch, tmp_path) -> None:
    """The dashboard must be able to answer "is the latest commit live?".

    Provenance is baked into the image at build time, so it is read from the
    environment rather than from git - the container has no repository. An image
    built without it reports empty strings, which the dashboard renders as
    "Build unknown" instead of inventing a version.
    """
    from fastapi.testclient import TestClient

    import dataclasses

    import bot as bot_app
    from config import settings

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")

    client = TestClient(bot_app.app)

    monkeypatch.setattr(
        bot_app, "settings",
        dataclasses.replace(
            settings,
            build_sha="4ba5f00af39f1234567890abcdef1234567890ab",
            build_time="2026-11-21T16:23:00Z",
            build_ref="main",
        ),
    )
    build = client.get("/api/status").json()["build"]
    assert build["sha"] == "4ba5f00af39f1234567890abcdef1234567890ab"
    assert build["short_sha"] == "4ba5f00af39f"
    assert build["time"] == "2026-11-21T16:23:00Z"
    assert build["ref"] == "main"

    monkeypatch.setattr(
        bot_app, "settings",
        dataclasses.replace(settings, build_sha="", build_time="", build_ref=""),
    )
    build = client.get("/api/status").json()["build"]
    assert build["sha"] == ""
    assert build["short_sha"] == "", "no provenance must not become a fake version"


# ---------------------------------------------------------------------------
# GIFT Nifty index book
#
# The index book exists to answer one question - would trading the signal source
# have beaten trading the future? - so these tests pin the properties that make
# the comparison valid: both books act on the same bar, the index is paper-only
# because it cannot be traded, and the two ledgers stay in separate files.
# ---------------------------------------------------------------------------
def _gift_book(monkeypatch, tmp_path, *, enabled=True, units=65):
    import gift_book
    import order_manager

    use_test_database(monkeypatch, tmp_path)
    stub = SimpleNamespace(
        tick_size=0.05,
        slippage_ticks=1.0,
        lot_size=65,
        gift_book_balance=200_000.0,
        virtual_balance=1_000_000.0,
        gift_book_units=units,
    )
    monkeypatch.setattr(gift_book, "settings", stub)
    monkeypatch.setattr(order_manager, "settings", stub)
    return gift_book.GiftBook(
        path=str(tmp_path / "gift_book.db"),
        enabled=enabled,
        units=units,
    )


def test_gift_book_is_off_by_default(monkeypatch, tmp_path) -> None:
    """It changes what the UI shows, so it must never switch itself on."""
    book = _gift_book(monkeypatch, tmp_path, enabled=False)

    assert book.execute("BUY", 25_000.0) is None
    assert book.positions() == []
    assert not (tmp_path / "gift_book.db").exists()
    assert book.status() == {"enabled": False, "configured": False, "symbol": "NSEIX:GIFT NIFTY"}


def test_gift_book_trades_the_index_level_not_a_contract(monkeypatch, tmp_path) -> None:
    book = _gift_book(monkeypatch, tmp_path)

    book.execute("BUY", 25_000.0, detail="2 of 3 ST agreed")

    positions = book.positions()
    assert len(positions) == 1
    assert positions[0]["symbol"] == "NSEIX:GIFT NIFTY"
    assert positions[0]["source"] == "GIFT_BOOK"
    # One tick of slippage on the signal's close, in the adverse direction.
    assert positions[0]["entry_price"] == 25_000.05
    assert positions[0]["quantity"] == 65


def test_gift_book_flips_like_the_pine_entries(monkeypatch, tmp_path) -> None:
    """BUY/SELL are target positions, so an opposite signal flips in one step."""
    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)

    book.execute("SELL", 25_100.0)

    assert book.net_units() == -65
    trades = book.trades()
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "FLIP"
    assert trades[0]["direction"] == "LONG"


def test_gift_book_settles_at_the_signal_close_with_no_quote(monkeypatch, tmp_path) -> None:
    """The bar's own close prices the fill, so the index book cannot be blocked.

    This is the mechanism that lets it act at 02:00, when the futures book has to
    defer - the single most important difference between the two books.
    """
    book = _gift_book(monkeypatch, tmp_path)

    book.execute("SELL", 24_950.0, bar_at="2026-11-11T02:00:00+05:30")

    assert book.net_units() == -65
    assert book.trades() == []  # still open


def test_gift_book_skips_a_bar_with_no_price(monkeypatch, tmp_path) -> None:
    """An unpriced signal is recorded, never filled at a guess."""
    book = _gift_book(monkeypatch, tmp_path)

    assert book.execute("BUY", None) is None
    assert book.execute("BUY", 0.0) is None
    assert book.positions() == []
    status = book.status()
    assert status["skipped"] == 2
    assert "no GIFT Nifty price" in status["last_error"]


def test_gift_book_repeats_nothing_when_already_positioned(monkeypatch, tmp_path) -> None:
    """A repeat of the same signal must not stack a second position."""
    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)

    assert book.execute("BUY", 25_050.0) is None
    assert book.net_units() == 65
    assert book.status()["fills"] == 1


def test_gift_book_flattens_on_a_flat_signal(monkeypatch, tmp_path) -> None:
    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)

    book.execute("FLAT", 25_120.0)

    assert book.net_units() == 0
    assert book.trades()[0]["exit_reason"] == "FLAT"
    # 65 units x ~119 points net of two ticks of slippage, less the round-trip
    # cost stack. At 0.05% sell-side STT the stack is ~970 on this turnover, so
    # the net lands near 6,823 - below 7,000, which is the statutory cost biting.
    assert 6_700 < book.trades()[0]["net_pnl"] < 7_000


def test_gift_book_keeps_its_own_ledger(monkeypatch, tmp_path) -> None:
    """The two books must not be able to see or corrupt each other."""
    book = _gift_book(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_400.0, mode=PAPER, source="ENGINE")

    book.execute("BUY", 25_000.0)

    futures = db.get_open_positions(mode=PAPER)
    assert [p["symbol"] for p in futures] == ["NIFTY26NOVFUT"]
    assert [p["symbol"] for p in book.positions()] == ["NSEIX:GIFT NIFTY"]
    assert db.get_trades() == []


def test_gift_book_marks_to_the_index_level(monkeypatch, tmp_path) -> None:
    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)

    book.mark(25_100.0)

    position = book.positions()[0]
    assert position["last_price"] == 25_100.0
    assert book.summary()["metrics"]["unrealized_pnl"] > 6_000


def test_gift_book_records_a_failure_instead_of_raising(monkeypatch, tmp_path) -> None:
    """It runs ahead of the futures order, so it can never be allowed to throw."""
    book = _gift_book(monkeypatch, tmp_path)
    monkeypatch.setattr(
        db, "open_position", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
    )

    result = book.execute("BUY", 25_000.0)

    assert result is None
    assert "disk full" in book.status()["last_error"]


def test_gift_book_honours_the_pause_switch(monkeypatch, tmp_path) -> None:
    """A paused bot must not open index trades either.

    The index book is a separate ledger, so it does not go through the order
    manager's entry guard. Without its own check, pausing the bot still opened
    index positions - which is what made a "clean" reset re-enter within seconds.
    """
    import order_manager

    book = _gift_book(monkeypatch, tmp_path)
    manager = order_manager.get_order_manager()
    manager.pause("RESET: books wiped")

    try:
        assert book.execute("BUY", 25_000.0, bar_at="2026-11-12T21:30:00+05:30") is None
        assert book.open_positions() == []
        # The refusal is recorded, so the trail explains why nothing happened.
        rows = book.signals()
        assert rows and rows[0]["accepted"] == 0
        assert "paused" in rows[0]["reason"]
    finally:
        manager.resume()

    # Unpaused, the same call opens the position.
    assert book.execute("BUY", 25_000.0, bar_at="2026-11-12T21:35:00+05:30") is not None
    assert len(book.open_positions()) == 1


def test_gift_book_still_exits_while_paused(monkeypatch, tmp_path) -> None:
    """A pause blocks entries, not exits - an open leg must still be closable."""
    import order_manager

    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0, bar_at="2026-11-12T21:30:00+05:30")

    manager = order_manager.get_order_manager()
    manager.pause("RESET: books wiped")
    try:
        assert book.execute("FLAT", 25_100.0, bar_at="2026-11-12T21:35:00+05:30") is not None
        assert book.open_positions() == []
    finally:
        manager.resume()


def test_gift_book_records_its_own_signal_trail(monkeypatch, tmp_path) -> None:
    """The index book's decisions must be readable from its own ledger.

    The Orders tab scopes its signal trail to the selected venue, so an index
    book that traded without recording a signal would render as "no signals"
    while its order book showed fills - the exact mismatch this fixes.
    """
    book = _gift_book(monkeypatch, tmp_path)

    book.execute("BUY", 25_000.0, bar_at="2026-11-12T21:30:00+05:30", detail="test")
    book.execute("FLAT", 25_100.0, bar_at="2026-11-12T21:35:00+05:30", detail="test")

    rows = book.signals()
    assert [row["action"] for row in rows] == ["FLAT", "BUY"]
    assert all(row["accepted"] == 1 for row in rows)
    # The futures ledger is a different database and must stay untouched.
    assert db.get_signals() == []


def test_gift_book_records_a_refused_signal(monkeypatch, tmp_path) -> None:
    """A skipped bar is a decision the trail has to explain, not silence."""
    book = _gift_book(monkeypatch, tmp_path)

    book.execute("BUY", None)

    rows = book.signals()
    assert len(rows) == 1
    assert rows[0]["accepted"] == 0
    assert "no GIFT Nifty price" in rows[0]["reason"]


def test_gift_book_does_not_record_a_repeat_as_a_signal(monkeypatch, tmp_path) -> None:
    """Holding the same position is a no-op, not a new decision."""
    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)

    book.execute("BUY", 25_050.0)

    assert len(book.signals()) == 1


def test_gift_signals_endpoint_reads_the_index_ledger(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    book = _gift_book(monkeypatch, tmp_path)
    book.execute("SELL", 25_000.0)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(bot_app, "gift_books", lambda: [book])

    payload = TestClient(bot_app.app).get("/api/gift/signals").json()

    assert payload["enabled"] is True
    assert payload["tradeable"] is False
    assert payload["count"] == 1
    assert payload["signals"][0]["action"] == "SELL"


def test_gift_signals_endpoint_reports_a_disabled_book(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    import gift_book

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app,
        "gift_books",
        lambda: [gift_book.GiftBook(path=str(tmp_path / "gift_book.db"), enabled=False)],
    )

    payload = TestClient(bot_app.app).get("/api/gift/signals").json()

    assert payload["enabled"] is False
    assert payload["signals"] == []


def test_gift_book_endpoints_report_a_disabled_book(monkeypatch, tmp_path) -> None:
    """Off is a normal state for the UI to render, not a 500."""
    from fastapi.testclient import TestClient

    import bot as bot_app
    import gift_book

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app,
        "gift_books",
        lambda: [gift_book.GiftBook(path=str(tmp_path / "gift_book.db"), enabled=False)],
    )

    client = TestClient(bot_app.app)
    for path in ("/api/gift/positions", "/api/gift/trades", "/api/gift/orders"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json()["tradeable"] is False
    summary = client.get("/api/gift/summary").json()
    assert summary["enabled"] is False
    assert summary["reason"] == "GIFT_BOOK is off"


# ---------------------------------------------------------------------------
# St strategy: one SuperTrend (14x3) AND PSAR, both required
# ---------------------------------------------------------------------------
def _rising_candles(count: int = 40) -> list[dict[str, float]]:
    """A clean uptrend, which an uptrend-following pair should both agree on."""
    return [
        {"high": 101.0 + i, "low": 99.0 + i, "close": 100.0 + i}
        for i in range(count)
    ]


def test_single_supertrend_is_14x3_by_default() -> None:
    """The strategy is one SuperTrend instance, not a 2-of-3 vote."""
    assert strategy.SUPERTREND_ATR_LENGTH == 14
    assert strategy.SUPERTREND_MULTIPLIER == 3.0
    assert config.strategy_summary()["supertrend"] == {"length": 14, "multiplier": 3.0}


def test_stale_vote_settings_cannot_reintroduce_a_second_supertrend(monkeypatch) -> None:
    """A leftover ``STRATEGY_ST_MIN_AGREE`` must not disarm the strategy.

    The threshold only made sense against a vote count. With one fixed instance
    there is nothing to count, so the setting is ignored rather than clamped -
    and the strategy still signals on a bar where both rules agree.
    """
    monkeypatch.setenv("STRATEGY_SUPERTRENDS", "14:3")
    monkeypatch.setenv("STRATEGY_ST_MIN_AGREE", "2")

    assert "st_min_agree" not in config.strategy_params()
    assert "min_agree" not in config.strategy_summary()
    assert strategy.evaluate(_rising_candles()).signal == strategy.BULL


def test_supertrend_agreeing_alone_is_not_a_signal(monkeypatch) -> None:
    """A BULL needs the SuperTrend vote AND PSAR, not either one.

    SuperTrend is left bullish and PSAR is forced to sit above price, so the two
    disagree: the SuperTrend condition is still true but the verdict must not be a
    BULL.
    """
    candles = _rising_candles()

    # Baseline: on this series the pair agrees and signals.
    assert strategy.evaluate(candles).signal == strategy.BULL

    monkeypatch.setattr(strategy, "psar", lambda *a, **k: [1.0e9] * len(candles))
    verdict = strategy.evaluate(candles)

    assert verdict.conditions["buy_supertrend"] is True
    assert verdict.conditions["buy_psar"] is False
    assert verdict.signal == strategy.NO_SIGNAL


def test_psar_agreeing_alone_is_not_a_signal(monkeypatch) -> None:
    """The mirror case: PSAR bullish but the SuperTrend vote against it.

    The forced direction is +1 (bearish) rather than 0, because a zero direction
    legitimately raises ``InsufficientData`` - the indicator is not warm, which is
    a different situation from disagreeing.
    """
    candles = _rising_candles()

    monkeypatch.setattr(
        strategy,
        "supertrend",
        lambda *a, **k: ([200.0] * len(candles), [1] * len(candles)),
    )
    verdict = strategy.evaluate(candles)

    assert verdict.conditions["buy_supertrend"] is False
    assert verdict.conditions["buy_psar"] is True
    assert verdict.conditions["sell_supertrend"] is True
    assert verdict.conditions["sell_psar"] is False
    assert verdict.signal == strategy.NO_SIGNAL


def test_both_agreeing_the_other_way_gives_bear() -> None:
    """The mirror case, so the AND is not accidentally one-directional."""
    falling = [
        {"high": 101.0 - i, "low": 99.0 - i, "close": 100.0 - i}
        for i in range(40)
    ]

    verdict = strategy.evaluate(falling)

    assert verdict.conditions == {
        "buy_supertrend": False,
        "buy_psar": False,
        "sell_supertrend": True,
        "sell_psar": True,
    }
    assert verdict.signal == strategy.BEAR


def test_confirmation_is_two_of_two_across_independent_rules() -> None:
    """The confirmation trade needs 2 of 2: SuperTrend AND PSAR.

    The count is what the dashboard and the engine log state, so it must agree
    with the verdict rather than being derived separately at each call site.
    """
    verdict = strategy.evaluate(_rising_candles())

    assert verdict.values["confirmations_total"] == 2
    assert verdict.values["buy_confirmations"] == 2
    assert verdict.values["sell_confirmations"] == 0
    assert verdict.signal == strategy.BULL
    assert "confirm BUY 2/2" in verdict.detail


def test_confirmation_count_is_one_when_only_one_rule_passes(monkeypatch) -> None:
    """1 of 2 is not a signal - that is the whole point of the confirmation."""
    candles = _rising_candles()
    monkeypatch.setattr(strategy, "psar", lambda *a, **k: [1.0e9] * len(candles))

    verdict = strategy.evaluate(candles)

    assert verdict.values["buy_confirmations"] == 1
    assert verdict.values["sell_confirmations"] == 1
    assert verdict.values["confirmations_total"] == 2
    assert verdict.signal == strategy.NO_SIGNAL


# ---------------------------------------------------------------------------
# Rollover: the dashboard warning, and the expiry it is derived from
# ---------------------------------------------------------------------------
def test_expiry_label_matches_the_contract_it_describes() -> None:
    """The label is derived from the symbol, so it cannot name a different day
    than the contract the engine will actually trade."""
    from kite_client import contract_expiry, expiry_label

    # November 2026's last Tuesday is the 24th.
    assert expiry_label("NIFTY26NOVFUT") == "Tue 24 Nov 2026"
    assert contract_expiry("NIFTY26NOVFUT") == date(2026, 11, 24)
    # December 2026's last Tuesday is the 29th.
    assert expiry_label("NIFTY26DECFUT") == "Tue 29 Dec 2026"
    assert expiry_label("NOTAFUTURE") is None


def test_rollover_status_carries_labels_for_both_contracts() -> None:
    """The banner names the expiring contract and its replacement, so both
    labels have to come from the same status the countdown uses."""
    from kite_client import rollover_status

    status = rollover_status(datetime(2026, 11, 9, 10, 0, tzinfo=IST))

    assert status["current_symbol"] == "NIFTY26NOVFUT"
    assert status["next_symbol"] == "NIFTY26DECFUT"
    assert status["expiry_label"] == "Tue 24 Nov 2026"
    assert status["next_expiry_label"] == "Tue 29 Dec 2026"
    assert status["rolled"] is False
    assert status["expiring_today"] is False


def test_future_symbol_rolls_five_days_before_expiry() -> None:
    """The engine switches to the next month five days before the old contract
    expires, so a signal inside that window opens the contract that will still
    exist next week rather than one about to settle."""
    from kite_client import future_symbol

    # November 2026 expires on Tuesday the 24th, so the window opens on the 19th.
    assert future_symbol(datetime(2026, 11, 18, 10, 0, tzinfo=IST)) == "NIFTY26NOVFUT"
    assert future_symbol(datetime(2026, 11, 19, 10, 0, tzinfo=IST)) == "NIFTY26DECFUT"
    assert future_symbol(datetime(2026, 11, 24, 10, 0, tzinfo=IST)) == "NIFTY26DECFUT"
    assert future_symbol(datetime(2026, 11, 25, 10, 0, tzinfo=IST)) == "NIFTY26DECFUT"


def test_future_symbol_rollover_window_is_a_date_range_not_a_countdown() -> None:
    """A signal at 09:20 and one at 15:20 on the same day must agree, or the
    contract would change mid-session."""
    from kite_client import future_symbol

    for hour in (9, 12, 15):
        assert future_symbol(datetime(2026, 11, 19, hour, 0, tzinfo=IST)) == "NIFTY26DECFUT"
        assert future_symbol(datetime(2026, 11, 18, hour, 0, tzinfo=IST)) == "NIFTY26NOVFUT"


def test_future_symbol_can_still_name_the_literal_front_month() -> None:
    """``rollover_days=0`` is the expiry calendar itself, which the rollover
    status and the expiry gate need in order to talk about the contract that is
    literally still trading."""
    from kite_client import future_symbol

    moment = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
    assert future_symbol(moment, rollover_days=0) == "NIFTY26NOVFUT"
    assert future_symbol(moment) == "NIFTY26DECFUT"


def test_rollover_window_does_not_skip_a_month() -> None:
    """months_ahead is relative to the contract being traded, so inside the
    window it must still step exactly one month - not two."""
    from kite_client import future_symbol

    moment = datetime(2026, 11, 20, 10, 0, tzinfo=IST)
    assert future_symbol(moment, months_ahead=0) == "NIFTY26DECFUT"
    assert future_symbol(moment, months_ahead=1) == "NIFTY27JANFUT"


def test_rollover_status_reports_the_window_and_the_calendar_contract() -> None:
    """Inside the window the engine trades next month's contract while the old
    one is still live, so the status has to report both or the banner cannot say
    which contract is expiring."""
    from kite_client import rollover_status

    inside = rollover_status(datetime(2026, 11, 19, 10, 0, tzinfo=IST))
    outside = rollover_status(datetime(2026, 11, 18, 10, 0, tzinfo=IST))

    assert inside["in_rollover_window"] is True
    assert inside["current_symbol"] == "NIFTY26DECFUT"
    assert inside["calendar_symbol"] == "NIFTY26NOVFUT"
    assert inside["days_to_calendar_expiry"] == 5
    assert inside["rollover_days"] == 5

    assert outside["in_rollover_window"] is False
    assert outside["current_symbol"] == "NIFTY26NOVFUT"
    assert outside["calendar_symbol"] == "NIFTY26NOVFUT"


def test_rollover_status_flags_a_position_carried_past_the_close() -> None:
    """The failure mode is a position held across the 15:30 close, not a new
    entry: the engine always trades the current front month, so a fresh entry can
    never name a dead contract. ``rolled`` therefore has to be driven by the
    contract actually held, or it can never be true at all."""
    from kite_client import rollover_status

    # Holding November's contract, one minute after it stopped trading.
    status = rollover_status(
        datetime(2026, 11, 24, 15, 31, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )

    assert status["rolled"] is True
    assert status["open_symbol"] == "NIFTY26NOVFUT"
    assert status["open_expiry"] == "2026-11-24"
    assert status["open_expiry_label"] == "Tue 24 Nov 2026"
    # The calendar has already rolled, so new entries would go to December's.
    assert status["current_symbol"] == "NIFTY26DECFUT"
    assert status["next_symbol"] == "NIFTY27JANFUT"
    # The countdown now points at the contract that is tradeable, not the dead one.
    assert status["expiry_date"] == "2026-12-29"
    assert status["expiry_label"] == "Tue 29 Dec 2026"
    assert status["seconds_to_expiry"] > 0


def test_rollover_status_is_not_rolled_without_an_expired_position() -> None:
    """``rolled`` must be false on every ordinary day, or the banner would sit
    red permanently and stop meaning anything."""
    from kite_client import rollover_status

    for moment in (
        datetime(2026, 11, 9, 10, 0, tzinfo=IST),
        datetime(2026, 11, 24, 10, 0, tzinfo=IST),       # expiry day, before the close
        datetime(2026, 11, 24, 15, 29, 59, tzinfo=IST),  # one second before the close
        datetime(2026, 11, 25, 10, 0, tzinfo=IST),
        datetime(2026, 12, 29, 10, 0, tzinfo=IST),
    ):
        assert rollover_status(moment)["rolled"] is False, moment
        # Holding the *current* contract is never an expiry, at any moment.
        assert rollover_status(moment, open_symbol=rollover_status(moment)["current_symbol"])[
            "rolled"
        ] is False, moment


def test_rollover_deadline_is_the_close_of_the_contract_being_traded() -> None:
    """Two deadlines, and they are different things.

    ``expiry`` is the contract the engine is trading, which is what the countdown
    points at. ``calendar_expiry_date`` is the contract that is literally still
    trading, which is what "expiring today" and the rollover window are measured
    against. Inside the window the engine has already moved on, so the two differ
    by a month - and conflating them is what made the old countdown show a
    healthy "48d" beside a red "has expired" banner.
    """
    from kite_client import rollover_status

    before = rollover_status(datetime(2026, 11, 24, 15, 29, 59, tzinfo=IST))
    after = rollover_status(
        datetime(2026, 11, 24, 15, 30, 1, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )

    # The engine rolled four days ago, so the countdown points at December's.
    assert before["expiry_date"] == "2026-12-29"
    assert before["seconds_to_expiry"] > 0
    # November's is the one expiring, and it is still tradeable for one second.
    assert before["calendar_expiry_date"] == "2026-11-24"
    assert before["days_to_calendar_expiry"] == 0
    assert before["expiring_today"] is True
    assert before["in_rollover_window"] is True
    # One second later November has stopped trading, and a position in it is an
    # expiry to resolve by hand.
    assert after["rolled"] is True
    assert after["open_symbol"] == "NIFTY26NOVFUT"


def test_a_contract_expires_at_the_close_not_at_midnight() -> None:
    """Expiry day is still a trading day: the contract is live until 15:30, so a
    date-only comparison would flag it as expired for the whole session."""
    from kite_client import rollover_status

    morning = rollover_status(
        datetime(2026, 11, 24, 9, 30, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )
    at_close = rollover_status(
        datetime(2026, 11, 24, 15, 30, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )
    after_close = rollover_status(
        datetime(2026, 11, 24, 15, 30, 1, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )

    assert morning["rolled"] is False
    assert at_close["rolled"] is False
    assert after_close["rolled"] is True


def test_rollover_status_marks_expiry_day_before_the_close() -> None:
    from kite_client import rollover_status

    status = rollover_status(datetime(2026, 11, 24, 10, 0, tzinfo=IST))

    assert status["expiring_today"] is True
    assert status["rolled"] is False
    assert status["seconds_to_expiry"] > 0


def test_engine_status_reports_rollover_before_the_first_loop_tick(monkeypatch, tmp_path) -> None:
    """The dashboard polls /api/status every 1.5s and the loop ticks every 5s, so
    the first poll must already carry the rollover state - otherwise the banner
    is blank exactly when the operator opens the page.

    It must also carry the *held* contract. ``status`` used to call
    ``rollover_status()`` with no ``open_symbol``, which reports ``rolled`` as
    False by design - so a position stranded in an expired contract produced a
    green banner, and ``rollover_paused`` stayed False, on every cold poll.
    """
    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")
    feed = GiftNiftyFeed("1minute")
    feed._history = [
        {
            "time": f"2026-11-05T09:{index:02d}:00+05:30",
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 1.0,
        }
        for index in range(15)
    ]
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": False, "reason": "closed"})
    monkeypatch.setattr(
        engine,
        "get_order_manager",
        lambda: SimpleNamespace(
            current_symbol=lambda: "NIFTY26NOVFUT", paused=False, paused_reason=""
        ),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)

    payload = engine.StrategyEngine().status()

    # No loop tick has run, so this can only have come from status() itself.
    assert payload["rollover"]["current_symbol"] == kite_client.future_symbol()
    assert payload["rollover"]["expiry_label"]
    # The bug: the held contract has to reach rollover_status, or `rolled` can
    # never be true here. One minute after the 15:30 close on expiry day the
    # position has no venue left, so the dashboard must say so.
    monkeypatch.setattr(
        engine,
        "rollover_status",
        lambda **kwargs: kite_client.rollover_status(
            datetime(2026, 11, 24, 15, 31, tzinfo=IST), **kwargs
        ),
    )
    expired = engine.StrategyEngine().status()

    assert expired["rollover"]["open_symbol"] == "NIFTY26NOVFUT"
    assert expired["rollover"]["rolled"] is True
    assert expired["rollover_paused"] is True


def test_enforce_rollover_flags_a_position_in_an_expired_contract(monkeypatch, tmp_path) -> None:
    """The engine reads the contract it is actually holding, so a position left
    open across the close is reported as an expiry rather than silently ignored."""
    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(
        engine,
        "rollover_status",
        lambda **kwargs: kite_client.rollover_status(
            datetime(2026, 11, 24, 15, 31, tzinfo=IST), **kwargs
        ),
    )

    status = engine.StrategyEngine().enforce_rollover()

    assert status["open_symbol"] == "NIFTY26NOVFUT"
    assert status["rolled"] is True


def test_enforce_rollover_ignores_a_flat_book(monkeypatch, tmp_path) -> None:
    """With nothing open there is no contract to be late for, so the banner must
    not claim an expiry is pending."""
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(
        engine,
        "rollover_status",
        lambda **kwargs: kite_client.rollover_status(
            datetime(2026, 11, 24, 15, 31, tzinfo=IST), **kwargs
        ),
    )

    status = engine.StrategyEngine().enforce_rollover()

    assert status["open_symbol"] is None
    assert status["rolled"] is False


def test_status_exposes_the_contract_expiry_for_the_exec_panel(monkeypatch, tmp_path) -> None:
    """The exec panel shows the expiry of the contract being traded. It is
    derived from the symbol, so it costs no broker round-trip on a 1.5s poll."""
    from fastapi.testclient import TestClient

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")

    payload = TestClient(bot_app.app).get("/api/status").json()

    assert payload["symbol"]["expiry"]
    assert payload["symbol"]["next_expiry"]
    assert payload["symbol"]["expiry"] != payload["symbol"]["next_expiry"]
    # The banner reads the same status, so it is populated on the first poll too.
    assert payload["engine"]["rollover"]["expiry_label"] == payload["symbol"]["expiry"]


# ---------------------------------------------------------------------------
# Venue-scoped ledger: the Orders and Performance tabs read one book at a time
# ---------------------------------------------------------------------------
def test_gift_analytics_reads_its_own_ledger(monkeypatch, tmp_path) -> None:
    """The index book's statistics must come from its own database. A shared
    query would silently report futures trades under the index heading."""
    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)
    book.execute("SELL", 25_100.0)

    analytics = book.analytics()

    assert analytics["summary"]["trades"] == 1
    assert analytics["summary"]["net_pnl"] > 0
    # The futures ledger is untouched, so its own analytics stay empty.
    assert db.get_trade_analytics(mode=PAPER)["summary"]["trades"] == 0


def test_gift_analytics_is_empty_when_the_book_is_off(monkeypatch, tmp_path) -> None:
    book = _gift_book(monkeypatch, tmp_path, enabled=False)

    assert book.analytics() == {}


def test_gift_analytics_endpoint_reports_a_disabled_book(monkeypatch, tmp_path) -> None:
    """Off is a normal state for the UI to render, not a 500."""
    from fastapi.testclient import TestClient

    import gift_book

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app,
        "gift_books",
        lambda: [gift_book.GiftBook(path=str(tmp_path / "gift_book.db"), enabled=False)],
    )

    response = TestClient(bot_app.app).get("/api/gift/analytics")

    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["tradeable"] is False


def test_gift_portfolio_matches_the_futures_payload_shape(monkeypatch, tmp_path) -> None:
    """The Performance tab's tiles are fed from either endpoint, so the two must
    expose the same keys or the index venue would render em dashes."""
    from fastapi.testclient import TestClient

    book = _gift_book(monkeypatch, tmp_path)
    book.execute("BUY", 25_000.0)
    book.execute("SELL", 25_100.0)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(bot_app, "gift_books", lambda: [book])

    payload = TestClient(bot_app.app).get("/api/gift/portfolio").json()

    for key in ("total_pnl", "total_return_pct", "max_drawdown_pct", "total_trades",
                "win_rate", "realized_total", "unrealized_pnl", "equity",
                "initial_balance", "open_position_count"):
        assert key in payload, key
    assert payload["total_trades"] == 1
    assert payload["tradeable"] is False
    assert payload["mode"] == PAPER


def test_gift_portfolio_is_empty_when_the_book_is_off(monkeypatch, tmp_path) -> None:
    from fastapi.testclient import TestClient

    import gift_book

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app,
        "gift_books",
        lambda: [gift_book.GiftBook(path=str(tmp_path / "gift_book.db"), enabled=False)],
    )

    payload = TestClient(bot_app.app).get("/api/gift/portfolio").json()

    assert payload["enabled"] is False


# ---------------------------------------------------------------------------
# Rollover execution: a signal inside the window closes the old leg and opens
# the new contract in one action
# ---------------------------------------------------------------------------
class _FrozenDatetime(datetime):
    """A ``datetime`` whose ``now()`` is pinned, for expiry-boundary tests.

    Only ``now()`` is overridden; construction and arithmetic behave normally, so
    the module under test can still build and compare real timestamps.
    """

    _now: Optional[datetime] = None

    @classmethod
    def now(cls, tz=None):
        moment = cls._now
        return moment if tz is None else moment.astimezone(tz)


def _pin_clock(monkeypatch, moment: datetime) -> None:
    """Freeze ``kite_client``'s clock, which is where expiry is decided."""
    _FrozenDatetime._now = moment
    monkeypatch.setattr(kite_client, "datetime", _FrozenDatetime)


def _pin_signal_clock(monkeypatch, moment: datetime) -> None:
    """Freeze the clock the *staleness* check reads.

    ``order_manager`` measures a signal's age against ``datetime.now`` in its own
    module, so pinning ``kite_client`` alone leaves the age computed from the real
    wall clock. A test that builds bars at a fixed timestamp then drifts into
    "Stale signal" as the calendar moves away from it.
    """
    _FrozenDatetime._now = moment
    monkeypatch.setattr(order_manager, "datetime", _FrozenDatetime)


def _rollover_manager(monkeypatch, tmp_path, *, lot_size=65, now=None):
    """A manager whose fills always succeed, with no broker access.

    The clock is pinned to ``now`` (default: a quiet day well before the November
    2026 expiry). The rollover decision itself depends only on which contract is
    held relative to the front month, but the *entry block* asks whether that
    contract has stopped trading - which is a question about the current time, so
    an unpinned clock would make these tests pass or fail by calendar date.
    """
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    _pin_clock(monkeypatch, now or datetime(2026, 11, 19, 10, 0, tzinfo=IST))
    stub = SimpleNamespace(
        product="NRML",
        order_type="MARKET",
        tick_size=0.05,
        slippage_ticks=1.0,
        lot_size=lot_size,
        default_lots=1,
        max_open_lots=4,
        lot_margin=150_000.0,
        virtual_balance=1_000_000.0,
    )
    monkeypatch.setattr(order_manager, "settings", stub)

    class FakeClient:
        mode = PAPER

        def __init__(self):
            self.orders = []

        def get_ltp(self, symbol, max_age=None):
            return 25_000.0

        def place_order(self, **kwargs):
            self.orders.append(kwargs)
            return {
                "order_id": f"o{len(self.orders)}",
                "status": "COMPLETE",
                "filled_quantity": kwargs["quantity"],
                "average_price": kwargs["price"],
            }

        def available_cash(self):
            return 1_000_000.0

        def data_source(self):
            return "test"

        def auth_status(self):
            return {"authenticated": True}

    client = FakeClient()
    return OrderManager(client), client


def test_a_flip_inside_the_window_rolls_the_position_to_the_new_contract(
    monkeypatch, tmp_path
) -> None:
    """The whole point of the early rollover: a *flip* after the switch closes
    the old contract and opens the new one, so the position follows the signal
    instead of being left behind in a contract about to settle."""
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    # Long November, and the signal is a SELL - a flip.
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "SELL", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert result.ok, result.reason
    # Two orders: the close of the old leg, then the open of the new one.
    assert [o["symbol"] for o in client.orders] == ["NIFTY26NOVFUT", "NIFTY26DECFUT"]
    assert [o["transaction_type"] for o in client.orders] == ["SELL", "SELL"]

    open_positions = db.get_open_positions(mode=PAPER)
    assert [p["symbol"] for p in open_positions] == ["NIFTY26DECFUT"]
    assert open_positions[0]["quantity"] == 65
    assert open_positions[0]["position_key"] == -1
    # The old leg is settled as a trade, not left dangling.
    trades = db.get_trades()
    assert [t["symbol"] for t in trades] == ["NIFTY26NOVFUT"]
    assert trades[0]["exit_reason"] == "ROLLOVER"


def test_a_signal_that_agrees_with_the_old_leg_does_not_roll_it(
    monkeypatch, tmp_path
) -> None:
    """Rolling a position that already points the same way as the signal would
    pay two spreads to end up exactly where it already is. Leave it in the
    contract it is in and let it run to expiry."""
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    # Long November, and the signal is also a BUY - no flip.
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "BUY", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "already matches" in result.reason
    assert client.orders == []
    # Untouched: still long November, nothing opened in December.
    open_positions = db.get_open_positions(mode=PAPER)
    assert [p["symbol"] for p in open_positions] == ["NIFTY26NOVFUT"]
    assert db.get_trades() == []


def test_a_short_old_leg_agrees_with_a_sell_signal(monkeypatch, tmp_path) -> None:
    """The agreement test is on direction, not on the action name: a short
    position already matches a SELL."""
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "SELL", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "already matches" in result.reason
    assert client.orders == []


def test_a_position_in_a_later_month_is_refused_as_double_exposure(
    monkeypatch, tmp_path
) -> None:
    """Two contracts at once is not a rollover, it is double exposure. Only a
    contract at or behind the front month is treated as the old leg."""
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    # Holding a *later* month than the one being traded.
    db.open_position("NIFTY27JANFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "BUY", "NIFTY26NOVFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "existing nifty futures exposure" in result.reason.lower()
    assert client.orders == []
    # Nothing was closed: the refusal happens before any order is placed.
    assert [p["symbol"] for p in db.get_open_positions(mode=PAPER)] == ["NIFTY27JANFUT"]


def test_rollover_does_not_touch_the_gift_index_book(monkeypatch, tmp_path) -> None:
    """The index book shares the position table's shape but is a different
    instrument in a different database. A rollover must never close it."""
    import gift_book

    manager, client = _rollover_manager(monkeypatch, tmp_path)
    book = gift_book.GiftBook(path=str(tmp_path / "gift.db"), enabled=True, units=65)
    monkeypatch.setattr(
        gift_book,
        "settings",
        SimpleNamespace(
            tick_size=0.05, slippage_ticks=1.0, lot_size=65,
            gift_book_balance=200_000.0, virtual_balance=1_000_000.0, gift_book_units=65,
        ),
    )
    book.execute("BUY", 25_000.0)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    # A SELL against a long November position is a flip, so the rollover path
    # runs - which is the case where the index book could be wrongly swept up.
    result = manager._execute_directional({}, "SELL", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert result.ok, result.reason
    # The index book is untouched: still long, still open.
    assert [p["symbol"] for p in book.positions()] == ["NSEIX:GIFT NIFTY"]
    assert book.net_units() == 65


def test_rollover_is_refused_when_the_old_leg_cannot_be_closed(monkeypatch, tmp_path) -> None:
    """Opening the new contract while the old one is still open would be double
    exposure, so a failed close must abort the whole rollover."""
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    def reject(**kwargs):
        client.orders.append(kwargs)
        return {"order_id": "x", "status": "REJECTED", "filled_quantity": 0, "message": "no"}

    client.place_order = reject

    # A SELL against a long November position is a flip, so it tries to roll.
    result = manager._execute_directional({}, "SELL", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "rollover blocked" in result.reason.lower()
    # Only the close was attempted; the new contract was never opened.
    assert [o["symbol"] for o in client.orders] == ["NIFTY26NOVFUT"]
    assert [p["symbol"] for p in db.get_open_positions(mode=PAPER)] == ["NIFTY26NOVFUT"]


def test_an_expired_leg_blocks_new_entries(monkeypatch, tmp_path) -> None:
    """A leg whose contract has stopped trading has no venue left, so it is an
    expiry to resolve by hand. Opening the next month beside it would be two
    contracts at once - the double exposure the rollover branch refuses - so
    entries are blocked until it is squared off."""
    manager, client = _rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 24, 15, 31, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "BUY", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "entries blocked" in result.reason.lower()
    assert "NIFTY26NOVFUT" in result.reason
    # Nothing was sent, and the expired leg is untouched.
    assert client.orders == []
    assert [p["symbol"] for p in db.get_open_positions(mode=PAPER)] == ["NIFTY26NOVFUT"]


def test_an_expired_leg_blocks_a_flip_too(monkeypatch, tmp_path) -> None:
    """The block is not about direction. A flip would close the dead leg and open
    the new one, but the close cannot fill - there is no venue - so the whole
    action is refused rather than half-done."""
    manager, client = _rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 24, 15, 31, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "SELL", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert not result.ok
    assert "entries blocked" in result.reason.lower()
    assert client.orders == []


def test_a_live_leg_inside_the_window_is_not_blocked(monkeypatch, tmp_path) -> None:
    """Expiry day is still a trading day. The contract is live until the 15:30
    close, so a flip one minute before it must still roll normally - a date-only
    test would block the whole final session."""
    manager, client = _rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 24, 15, 29, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager._execute_directional({}, "SELL", "NIFTY26DECFUT", PAPER, "test", strategy_id=db.DEFAULT_STRATEGY_ID)

    assert result.ok, result.reason
    assert [o["symbol"] for o in client.orders] == ["NIFTY26NOVFUT", "NIFTY26DECFUT"]
    assert [p["symbol"] for p in db.get_open_positions(mode=PAPER)] == ["NIFTY26DECFUT"]


def test_a_flat_signal_reaches_an_expired_leg(monkeypatch, tmp_path) -> None:
    """The engine names the *next* month once the window opens, so a FLAT signal
    arrives carrying a symbol the expired leg does not have. Filtering strictly
    on it would report "No open positions" while the exposure is still there."""
    manager, client = _rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 24, 15, 31, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager.square_off(
        "SIGNAL_FLAT", source="ENGINE", symbol="NIFTY26DECFUT", mode=PAPER
    )

    assert result.ok, result.reason
    assert [o["symbol"] for o in client.orders] == ["NIFTY26NOVFUT"]
    assert db.get_open_positions(mode=PAPER) == []


def test_a_flat_signal_still_ignores_an_unrelated_contract(monkeypatch, tmp_path) -> None:
    """The widened filter is for *expired* legs only. A live position in another
    contract is not something a FLAT for this symbol asked to close."""
    manager, client = _rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    )
    db.open_position("NIFTY26DECFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    result = manager.square_off(
        "SIGNAL_FLAT", source="ENGINE", symbol="NIFTY26NOVFUT", mode=PAPER
    )

    assert result.ok, result.reason
    assert client.orders == []
    assert [p["symbol"] for p in db.get_open_positions(mode=PAPER)] == ["NIFTY26DECFUT"]


def test_rollover_status_reports_that_entries_are_blocked() -> None:
    """The dashboard needs the block as a state, not as an unexplained rejection,
    so the status carries it alongside ``rolled``."""
    from kite_client import rollover_status

    expired = rollover_status(
        datetime(2026, 11, 24, 15, 31, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )
    live = rollover_status(
        datetime(2026, 11, 24, 15, 29, tzinfo=IST), open_symbol="NIFTY26NOVFUT"
    )

    assert expired["entries_blocked"] is True
    assert live["entries_blocked"] is False


# ---------------------------------------------------------------------------
# LIVE rollover parity
#
# The early rollover was PAPER-only: `current_symbol()` returned
# `front_month_contract()` for LIVE, and that helper walks the instrument dump in
# expiry order, so it never rolled early. LIVE rode the front month to the 15:30
# close while PAPER had already moved on - which also made the expired-leg entry
# blocks unreachable in LIVE, since by the time a contract had stopped trading
# the LIVE symbol had already followed the calendar past it.
# ---------------------------------------------------------------------------

def _live_rollover_manager(monkeypatch, tmp_path, *, now, lot_size=65):
    """A LIVE manager with an instrument dump covering Nov and Dec 2026.

    The real ``KiteClient`` instrument methods are bound onto the fake client so
    the test exercises the actual symbol selection rather than a re-implementation
    of it.
    """
    manager, client = _rollover_manager(monkeypatch, tmp_path, lot_size=lot_size, now=now)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: LIVE)

    instruments = [
        {
            "instrument_type": "FUT",
            "tradingsymbol": "NIFTY26NOVFUT",
            "expiry": date(2026, 11, 24),
            "lot_size": lot_size,
        },
        {
            "instrument_type": "FUT",
            "tradingsymbol": "NIFTY26DECFUT",
            "expiry": date(2026, 12, 29),
            "lot_size": lot_size,
        },
    ]
    client._load_instruments = lambda: instruments
    for name in (
        "nifty_future_instruments",
        "nifty_future_contracts",
        "front_month_contract",
        "front_month_symbol",
        "instrument_for_symbol",
    ):
        setattr(client, name, MethodType(getattr(KiteClient, name), client))
    client.net_position = lambda symbol, force=False: 0
    client.positions = lambda force=False: []
    client.required_margin = lambda *args, **kwargs: None
    client.session_status = lambda: {"valid": True}
    return manager, client


def test_live_current_symbol_rolls_early_like_paper(monkeypatch, tmp_path) -> None:
    """LIVE must switch to next month inside the same window PAPER does.

    Nov 24 2026 is the last Tuesday, so the five-day window opens on Nov 19: a
    paper signal at 10:00 on Nov 19 already trades DEC, and LIVE has to agree or
    the same signal opens two different contracts depending on the mode.
    """
    # The clock is a module-level patch, so each manager must be exercised while
    # its own pin is still the active one.
    before, _ = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 18, 10, 0, tzinfo=IST)
    )
    assert before.current_symbol() == "NIFTY26NOVFUT"

    inside, _ = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    )
    assert inside.current_symbol() == "NIFTY26DECFUT"
    # And the two modes now name the same contract on the same day, which is the
    # property the whole change exists to establish.
    assert inside.current_symbol() == kite_client.future_symbol(
        datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    )


def test_live_lot_size_accepts_the_rolled_contract(monkeypatch, tmp_path) -> None:
    """`_lot_size` used to demand the *calendar* front month.

    With LIVE rolling early the traded symbol is next month's for the last five
    days of the contract's life, so the old assertion would have rejected every
    live entry inside the window.
    """
    manager, _ = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 21, 10, 0, tzinfo=IST)
    )

    assert manager._lot_size("NIFTY26DECFUT", LIVE) == 65
    with pytest.raises(ValueError):
        # The calendar front month is no longer what LIVE trades.
        manager._lot_size("NIFTY26NOVFUT", LIVE)


def test_live_symbol_is_empty_when_the_dump_has_no_front_month(
    monkeypatch, tmp_path
) -> None:
    """A stale instrument cache must not produce an orderable symbol.

    Kite's dump is refreshed periodically, so between the rollover and the
    refresh the next month genuinely may not be listed yet. Returning "" makes
    the caller refuse rather than send an order for a contract the broker does
    not know.
    """
    manager, client = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 21, 10, 0, tzinfo=IST)
    )
    client._load_instruments = lambda: []

    assert manager.current_symbol() == ""


def test_live_square_off_reaches_the_old_leg_after_an_early_roll(
    monkeypatch, tmp_path
) -> None:
    """A FLAT names the contract the engine trades - which is next month's.

    The position is still in the old contract, so a strict symbol filter would
    report "No open positions" and leave the exposure open. LIVE does this for
    real now that it rolls early.
    """
    manager, client = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 21, 10, 0, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=LIVE, source="ENGINE")
    # The broker is in the same position, so the reconciliation guard passes and
    # the only thing standing between the FLAT and the old leg is the filter.
    client.positions = lambda force=False: [{"tradingsymbol": "NIFTY26NOVFUT", "quantity": 65}]

    # A FLAT arrives carrying the DEC symbol while NOV is still held.
    result = manager.square_off(
        "SIGNAL_FLAT", source="ENGINE", symbol=manager.current_symbol(), mode=LIVE
    )

    assert result.ok, result.reason
    assert [order["symbol"] for order in client.orders] == ["NIFTY26NOVFUT"]
    assert db.get_open_positions(mode=LIVE) == []


def test_live_sync_blocks_a_real_mismatch_on_the_old_leg(monkeypatch, tmp_path) -> None:
    """Rollover must not become a loophole in the reconciliation guard.

    The old leg is expected to trail the broker briefly, but only until the
    rollover flip books it. A genuine quantity disagreement on it is still drift
    and still has to block, so no leg is exempted from the comparison.
    """
    manager, client = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 21, 10, 0, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=LIVE, source="ENGINE")
    # The broker has already flattened the old leg behind the bot's back.
    client.positions = lambda force=False: []

    message = manager._live_position_sync_error()

    assert message is not None
    assert "NIFTY26NOVFUT" in message


def _readiness_env(monkeypatch, *, sync, strategies=None, margin=187_507.19,
                   cash=500_000.0):
    """Stub everything `_live_readiness` reads except the sync verdict.

    The engine default is a healthy single book because every test here is about
    a *different* check; leaving it unstubbed would fall through to the real
    engine and make these tests depend on whatever the workspace happens to be
    running. Tests about the strategy gate pass ``strategies`` explicitly.

    ``margin``/``cash`` default to a funded account so the margin gate is not
    what these tests accidentally measure. Pass ``margin=None`` to model an
    unreadable requirement.
    """
    if strategies is None:
        strategies = [{"id": "book-a", "state": {"ready": True, "last_error": None}}]
    # The market is pinned closed. `_live_readiness` also checks the mark loop
    # and quote freshness *in-session*, so an unpinned clock made these tests
    # start failing at 09:15 IST and pass again after 15:30 - the checks only
    # run while the exchange is open. That is a breach of this helper's own
    # contract ("stub everything the report reads"), not a change in behaviour.
    import kite_client

    monkeypatch.setattr(
        kite_client, "market_status", lambda: {"is_open": False, "reason": "closed"},
    )
    monkeypatch.setattr(
        bot_app, "get_engine",
        lambda: SimpleNamespace(status=lambda: {"strategies": strategies}),
    )
    monkeypatch.setattr(
        bot_app, "get_client",
        lambda: SimpleNamespace(
            session_status=lambda: {"valid": True},
            front_month_symbol=lambda *a, **k: "NIFTY26DECFUT",
            required_margin=lambda *a, **k: margin,
            available_cash=lambda: cash,
        ),
    )
    # lot_size and product are read by the margin check; a partial namespace
    # would make that check raise and silently fall into its non-blocking branch,
    # so every test here would pass for the wrong reason.
    monkeypatch.setattr(
        bot_app, "settings",
        SimpleNamespace(is_live_configured=True, lot_size=65, product="NRML"),
    )
    monkeypatch.setattr(
        bot_app, "get_calendar",
        lambda: SimpleNamespace(available=True, status=lambda: {"count": 20}),
    )
    manager = SimpleNamespace(
        paused=False,
        paused_reason=None,
        _live_sync_state=lambda mode=None: sync,
    )
    monkeypatch.setattr(bot_app, "get_order_manager", lambda: manager)


def test_live_readiness_blocks_on_a_position_mismatch(monkeypatch) -> None:
    """The reconciliation must be a gate before arming, not an afterthought.

    It used to be skipped unless the bot was already in LIVE, so the report that
    exists to decide whether LIVE is safe would declare `ready` without ever
    comparing the ledger to the broker.
    """
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": 0, "ledger": 65,
              "in_sync": False, "error": None},
    )

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert "position_sync" in report["blocking"]


def test_live_readiness_passes_when_the_ledger_matches_the_broker(monkeypatch) -> None:
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": 65, "ledger": 65,
              "in_sync": True, "error": None},
    )

    report = bot_app._live_readiness()

    assert report["ready"] is True
    assert report["blocking"] == []
    assert any(
        check["key"] == "position_sync" and check["ok"] for check in report["checks"]
    )


def test_live_readiness_reports_an_unreadable_broker_position(monkeypatch) -> None:
    """An unreadable position is not a pass - the gate cannot verify, so it blocks."""
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": None, "ledger": None,
              "in_sync": False, "error": "Zerodha positions unavailable"},
    )

    report = bot_app._live_readiness()

    assert report["ready"] is False
    check = next(c for c in report["checks"] if c["key"] == "position_sync")
    assert check["detail"] == "Zerodha positions unavailable"


def test_live_flip_inside_the_window_rolls_the_position(monkeypatch, tmp_path) -> None:
    """The end-to-end LIVE behaviour: the same flip that rolls PAPER.

    This is the case the whole change exists for. Before it, LIVE's symbol never
    left the front month, so this signal would have opened a second NOV leg or
    been refused as double exposure - and the position would have ridden NOV into
    its expiry while PAPER had already moved to DEC.
    """
    manager, client = _live_rollover_manager(
        monkeypatch, tmp_path, now=datetime(2026, 11, 21, 10, 0, tzinfo=IST)
    )
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=LIVE, source="ENGINE")
    client.positions = lambda force=False: [{"tradingsymbol": "NIFTY26NOVFUT", "quantity": 65}]

    result = manager._execute_directional(
        {}, "SELL", manager.current_symbol(), LIVE, "test",
        strategy_id=db.DEFAULT_STRATEGY_ID,
    )

    assert result.ok, result.reason


# ---------------------------------------------------------------------------
# The overnight open reconcile
#
# A signal that fires while the NSE is shut is refused by the executor, and that
# refusal happens *after* the previous verdict is advanced in `evaluate_now`. So a
# dropped signal also consumes its bar transition and the engine later reports "no
# signal change" - the flip is never placed. The morning is when the overnight
# risk is set, so that is the worst signal to lose.
#
# The rule is decided on the first bar that closes in-session. With 15-minute
# GIFT bars that is the 09:00 candle, which closes at exactly 09:15 (the NSE
# open), so the decision is the morning's own state rather than the overnight one.
# ---------------------------------------------------------------------------


def _overnight_env(monkeypatch, tmp_path, *, mode=PAPER):
    """An engine whose session, broker and executor are all stubbed.

    The fake broker tracks its own net quantity so the LIVE reconciliation guard
    sees a coherent book rather than a static one that disagrees after each fill.
    """
    use_test_database(monkeypatch, tmp_path)
    _client_mode = mode

    monkeypatch.setattr(engine, "get_execution_mode", lambda: mode)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: mode)
    monkeypatch.setattr(
        engine, "settings",
        SimpleNamespace(default_lots=1, max_open_lots=4, lot_margin=150_000.0),
    )
    # Pin the strategy list to the single default book. Without this the engine
    # builds whatever the developer's .env happens to configure, so adding a
    # second strategy to .env silently rewrites the behaviour of every test here.
    monkeypatch.setattr(
        engine, "strategy_configs",
        lambda: [config.StrategyConfig(
            index=1, id="Surikbs", source="gift", interval="60minute",
            exit="flip", lots=1, enabled=True, name="",
        )],
    )
    _pin_clock(monkeypatch, datetime(2026, 11, 16, 9, 15, tzinfo=IST))

    raw_instruments = [
        {"tradingsymbol": "NIFTY26NOVFUT", "instrument_type": "FUT",
         "expiry": date(2026, 11, 24), "lot_size": 65},
        {"tradingsymbol": "NIFTY26DECFUT", "instrument_type": "FUT",
         "expiry": date(2026, 12, 29), "lot_size": 65},
    ]

    class FakeClient:
        # A class body cannot read an enclosing local for a name it also assigns.
        mode = _client_mode

        def __init__(self):
            self.orders = []
            self.broker = {}

        def get_ltp(self, symbol, max_age=None):
            return 25_000.0

        def net_position(self, symbol, force=False):
            return self.broker.get(symbol, 0)

        def positions(self, force=False):
            return [
                {"tradingsymbol": symbol, "quantity": quantity}
                for symbol, quantity in self.broker.items()
                if quantity
            ]

        def data_source(self):
            return "test"

        def auth_status(self):
            return {"authenticated": True}

        def session_status(self):
            return {"valid": True}

        def available_cash(self):
            return 1_000_000.0

        def required_margin(self, *args, **kwargs):
            return None

        def place_order(self, **kwargs):
            self.orders.append(kwargs)
            symbol = kwargs["symbol"]
            delta = kwargs["quantity"]
            if kwargs["transaction_type"] == "SELL":
                delta = -delta
            self.broker[symbol] = self.broker.get(symbol, 0) + delta
            return {
                "order_id": f"o{len(self.orders)}",
                "status": "COMPLETE",
                "filled_quantity": kwargs["quantity"],
                "average_price": kwargs["price"],
            }

    client = FakeClient()
    manager = OrderManager(client)
    for name in ("nifty_future_instruments", "instrument_for_symbol",
                 "front_month_contract", "front_month_symbol"):
        setattr(client, name, MethodType(getattr(KiteClient, name), client))
    client._load_instruments = lambda: raw_instruments
    monkeypatch.setattr(engine, "get_order_manager", lambda: manager)
    monkeypatch.setattr(order_manager, "get_order_manager", lambda: manager)
    return engine.StrategyEngine(), manager, client


def test_the_0900_candle_is_the_one_that_closes_in_session() -> None:
    """Pin the bar boundary the whole feature is specified against.

    15-minute GIFT bars bucket on :00/:15/:30/:45, and the NSE opens at 09:15, so
    the 09:00 candle closes exactly at the open. The 08:45 candle closes at 09:00
    and is still pre-open - using it would decide on the overnight state.
    """
    engine_obj = engine.StrategyEngine()
    bar_seconds = 900

    # Pre-open: the newest closed bar's close is before 09:15.
    assert engine_obj._bar_before_first_session_close(
        "2026-11-16T08:45:00+05:30", bar_seconds, True
    ) is True
    # The 09:00 bar closes at 09:15 - this is the decisive one.
    assert engine_obj._bar_before_first_session_close(
        "2026-11-16T09:00:00+05:30", bar_seconds, True
    ) is False
    # Out of hours there is nothing to reconcile yet.
    assert engine_obj._bar_before_first_session_close(
        "2026-11-16T09:00:00+05:30", bar_seconds, False
    ) is True


def test_overnight_long_flips_on_a_bearish_morning_bar(monkeypatch, tmp_path) -> None:
    """Your case: long held overnight, the morning bar is a SELL -> flip."""
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    reconciled = eng._reconcile_overnight_position(strategy.BEAR, fresh=True)

    assert reconciled is not None
    assert reconciled["action"] == "SELL"
    # Closed the long, then opened the short.
    assert [o["transaction_type"] for o in client.orders] == ["SELL", "SELL"]
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1


def test_overnight_long_is_left_alone_on_a_bullish_morning_bar(
    monkeypatch, tmp_path
) -> None:
    """Signal BUY with a long already on: leave it as it is.

    It is already where the indicators point, so re-entering would pay a spread
    to duplicate the position.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    reconciled = eng._reconcile_overnight_position(strategy.BULL)

    assert reconciled is not None
    assert reconciled["action"] == "HOLD"
    assert reconciled["held"] is True
    assert client.orders == []
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1


def test_overnight_long_is_held_when_the_morning_bar_has_no_signal(
    monkeypatch, tmp_path
) -> None:
    """No signal is not an exit: the overnight leg is carried, not squared off.

    The two rules disagreeing says nothing about a position that is already on.
    Closing it there would turn every indecisive bar into a round trip, so the
    leg is left to its trailing stop instead.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    reconciled = eng._reconcile_overnight_position(strategy.NO_SIGNAL)

    assert reconciled is not None
    assert reconciled["action"] == "HOLD"
    assert reconciled["held"] is True
    assert "no directional signal" in reconciled["detail"]
    assert client.orders == []
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1


def test_overnight_short_flips_to_long_on_a_bullish_bar(monkeypatch, tmp_path) -> None:
    """The rule is symmetric across direction."""
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER, source="ENGINE")

    reconciled = eng._reconcile_overnight_position(strategy.BULL, fresh=True)

    assert reconciled is not None
    assert reconciled["action"] == "BUY"
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1


def test_overnight_leg_against_an_existing_state_is_closed_but_not_reversed(
    monkeypatch, tmp_path
) -> None:
    """A wrong overnight leg is closed; the reversal needs a fresh signal.

    Closing is unconditional - carrying a leg that opposes the indicators is the
    bigger risk. But opening the other side on a signal that has been in force all
    night is exactly the re-entry the transition rule forbids, so the bot goes
    flat and waits for the pair to form again.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    reconciled = eng._reconcile_overnight_position(strategy.BEAR, fresh=False)

    assert reconciled is not None
    assert reconciled["action"] == "FLAT"
    assert reconciled["closed"] is True
    assert reconciled["opened"] is False
    # Closed the long, and did not open the short.
    assert [o["transaction_type"] for o in client.orders] == ["SELL"]
    assert db.get_open_positions(mode=PAPER) == []


def test_overnight_reconcile_does_nothing_without_a_position(
    monkeypatch, tmp_path
) -> None:
    """No position means nothing to reconcile - it is not an entry trigger."""
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)

    assert eng._reconcile_overnight_position(strategy.BEAR) is None
    assert client.orders == []


def test_overnight_reconcile_does_not_open_opposite_leg_when_close_fails(
    monkeypatch, tmp_path
) -> None:
    """If the old leg will not close, opening the other side would leave both.

    That is the double exposure the order manager refuses everywhere else, so a
    failed close must abort rather than compound the position.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    def refuse(**kwargs):
        return {"status": "REJECTED", "message": "market just opened, no fill"}

    client.place_order = refuse

    reconciled = eng._reconcile_overnight_position(strategy.BEAR, fresh=True)

    assert reconciled is not None
    assert reconciled["opened"] is False
    assert "close failed" in reconciled["reason"]
    positions = db.get_open_positions(mode=PAPER)
    assert len(positions) == 1
    assert positions[0]["position_key"] == 1


def test_overnight_reconcile_runs_in_live(monkeypatch, tmp_path) -> None:
    """LIVE is the mode with the real overnight exposure, so it must be covered."""
    eng, manager, client = _overnight_env(monkeypatch, tmp_path, mode=LIVE)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=LIVE, source="ENGINE")
    client.broker["NIFTY26NOVFUT"] = 65

    reconciled = eng._reconcile_overnight_position(strategy.BEAR, fresh=True)

    assert reconciled is not None
    assert reconciled["opened"] is True
    assert client.broker["NIFTY26NOVFUT"] == -65
    assert db.get_open_positions(mode=LIVE)[0]["position_key"] == -1


def test_evaluate_now_closes_an_opposing_overnight_leg_without_reversing(
    monkeypatch, tmp_path
) -> None:
    """The reconcile runs ahead of the transition gate, but only to *close*.

    A long held overnight against a bearish morning bar is wrong, so it is closed
    on the first in-session bar. The reversal is not taken: the bearish state has
    been in force since before the close, so it is not a fresh signal, and
    reversing on it is exactly the re-entry the transition rule forbids. The bot
    goes flat and waits for the pair to form again.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    state = {"open": False}
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": state["open"], "reason": "Pre-market - opens 09:15 IST"},
    )

    # The 08:45 bar (closed 09:00, pre-open) then the 09:00 bar (closed 09:15).
    bars = [
        {"time": "2026-11-16T08:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    # BEAR on both bars: the state has not changed, so this is not a fresh signal.
    verdict = SimpleNamespace(
        signal=strategy.BEAR, previous_signal=strategy.BEAR, fresh=False,
        opposite=False,
                        flat=False,
        detail="test", bars=100, conditions={}, values={}, close=25_000.0,
    )

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: verdict)
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BEAR

    # 1. The pre-open bar: the decision must be withheld, not taken on it.
    feed.n = 1
    eng.evaluate_now()
    assert eng._overnight_pending is True
    assert client.orders == []

    # 2. The 09:00 bar closes in-session: the wrong leg is closed, and no short
    #    is opened on the state that was already in force overnight.
    state["open"] = True
    feed.n = 2
    result = eng.evaluate_now()

    assert result["action"] == "FLAT"
    assert [o["transaction_type"] for o in client.orders] == ["SELL"]
    assert db.get_open_positions(mode=PAPER) == []


def test_evaluate_now_holds_the_position_when_the_morning_bar_agrees(
    monkeypatch, tmp_path
) -> None:
    """An agreeing morning bar must not re-enter, and must report the hold."""
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    state = {"open": True}
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": state["open"], "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T08:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    verdict = SimpleNamespace(
        signal=strategy.BULL, previous_signal=strategy.BULL, fresh=False,
        opposite=False,
                        flat=False,
        detail="test", bars=100, conditions={}, values={}, close=25_000.0,
    )

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: verdict)
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.NO_SIGNAL

    feed.n = 1
    eng.evaluate_now()
    assert eng._overnight_pending is True

    feed.n = 2
    result = eng.evaluate_now()

    assert result["action"] == "HOLD"
    assert "agrees" in result["reason"]
    assert client.orders == []


def test_evaluate_now_enters_when_the_signal_arrived_while_the_market_was_shut(
    monkeypatch, tmp_path
) -> None:
    """A signal that closed out of session must still enter on the next open.

    This is the case the state-based entry exists for. GIFT Nifty trades ~20
    hours, so a BULL that turns up after the NSE close is deferred - no order is
    placed - while the bar still advances the previous verdict. Under a
    transition-gated entry every later BULL bar looked like "no signal change",
    so the bot stayed flat while the indicators pointed long. Driving the
    position to the verdict brings it in on the first in-session bar.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    # Pinned to the morning the entry is expected on, so the deferred signal is
    # one bar old rather than a day old.
    _pin_signal_clock(monkeypatch, datetime(2026, 11, 17, 9, 15, tzinfo=IST))

    state = {"open": True}
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": state["open"], "reason": "Market open" if state["open"] else "Post-market"},
    )
    bars = [
        {"time": "2026-11-16T15:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T15:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-17T09:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-17T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    # The BULL *starts* on the deferred bar and persists. Only the first of those
    # bars is a transition, so only it can latch the owed entry.
    verdicts = iter([
        SimpleNamespace(signal=strategy.NO_SIGNAL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.BULL,
                        opposite=False,
                        flat=False,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.BULL,
                        opposite=False,
                        flat=False,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BULL

    # 1. In-session NO_SIGNAL: nothing to do, and the bot stays flat.
    feed.n = 1
    assert eng.evaluate_now()["action"] is None
    assert db.get_open_positions(mode=PAPER) == []

    # 2. BULL closes while the NSE is shut: held, not acted on.
    state["open"] = False
    feed.n = 2
    assert eng.evaluate_now()["action"] is None
    assert db.get_open_positions(mode=PAPER) == []

    # 3. Still pre-open on the next bar: the decision stays held.
    feed.n = 3
    assert eng.evaluate_now()["action"] is None

    # 4. First in-session bar: the entry fires even though the verdict is the
    #    same one the deferred bar already recorded.
    state["open"] = True
    feed.n = 4
    result = eng.evaluate_now()

    assert result["action"] == "BUY"
    assert [o["transaction_type"] for o in client.orders] == ["BUY"]
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1


def test_evaluate_now_does_not_re_enter_while_already_positioned(
    monkeypatch, tmp_path
) -> None:
    """A run of agreeing bars enters once, not once per bar.

    Driving the position to the verdict must not turn every closed bar into an
    order: while a leg already matches the verdict there is nothing to do, and
    the order manager would refuse the duplicate anyway.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    _pin_signal_clock(monkeypatch, datetime(2026, 11, 16, 9, 15, tzinfo=IST))

    state = {"open": True}
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": state["open"], "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    # A transition on the first bar, then the same state on the rest.
    verdicts = iter([
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.BULL,
                        opposite=False,
                        flat=False,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.BULL,
                        opposite=False,
                        flat=False,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BULL

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    for n in (2, 3):
        feed.n = n
        result = eng.evaluate_now()
        assert result["action"] is None
        assert "not a new signal" in result["reason"]

    assert [o["transaction_type"] for o in client.orders] == ["BUY"]
    assert len(db.get_open_positions(mode=PAPER)) == 1


def _fresh_signal_env(monkeypatch, tmp_path, verdicts, bars=None):
    """An engine whose verdicts are scripted, one per closed bar.

    Returns ``(engine, feed, client)``. The clock is pinned to the bars so the
    executor's staleness check measures a one-bar-old signal rather than a
    calendar-old one.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    _pin_signal_clock(monkeypatch, datetime(2026, 11, 16, 9, 15, tzinfo=IST))
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )
    rows = bars or [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T10:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    script = iter(verdicts)

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return rows[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(script))
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    return eng, feed, client


def _verdict(signal, previous, close=25_000.0):
    return SimpleNamespace(
        signal=signal, previous_signal=previous, fresh=signal != previous,
        # The exit condition, derived the same way the real verdict derives it:
        # the bar where the pair turns against the direction it last held.
        opposite=(
            signal in {strategy.BULL, strategy.BEAR}
            and previous in {strategy.BULL, strategy.BEAR}
            and signal != previous
        ),
        flat=signal == strategy.NO_SIGNAL and previous != strategy.NO_SIGNAL,
        detail="test", bars=100, conditions={}, values={}, close=close,
        signal_candle={"time": "2026-11-16T09:30:00+05:30"},
    )


def test_each_run_reports_the_close_its_own_verdict_used(monkeypatch, tmp_path) -> None:
    """Per-book levels are useless without the per-book close.

    The signal panel shows each strategy's SuperTrend and PSAR levels. Turning
    those into a distance-to-flip needs the close that verdict was computed on -
    and it must be that run's close, not the active book's, or the second book's
    distance would be measured against the wrong price.
    """
    import bar_source

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BEAR, strategy.BULL, close=25_000.0),
        _verdict(strategy.BEAR, strategy.BULL, close=25_500.0),
    ])

    class OtherSource:
        seconds = 3600

        def refresh_history(self):
            return None

        def candles(self):
            return [{"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1,
                     "low": 1, "close": 1}]

        def status(self):
            return {"bars": 1}

    # GiftBarSource.status() delegates to the feed, so the stub needs one.
    monkeypatch.setattr(feed, "status", lambda: {"bars": feed.n}, raising=False)

    other = engine.StrategyRun("other-book", OtherSource(), params={}, lots=1)
    eng.runs.append(other)

    feed.n = 4
    eng.evaluate_run(eng.run)
    eng.evaluate_run(other)

    assert eng.run.status()["last_close"] == 25_000.0
    assert other.status()["last_close"] == 25_500.0, (
        "a run reported another book's close"
    )


def test_status_reports_the_close_the_verdict_was_computed_on(monkeypatch, tmp_path) -> None:
    """The indicator levels alone do not say how far price is from flipping.

    The signal panel shows SuperTrend and PSAR levels, but without the close the
    operator cannot tell whether the next bar is likely to keep the verdict. The
    close is the third number that makes the other two mean something.
    """
    _isolate_gift_book(monkeypatch, tmp_path)
    # status() recomputes the verdict for the dashboard, so the script needs an
    # entry for that pass as well as for the evaluation.
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BEAR, strategy.BULL, close=25_123.45),
        _verdict(strategy.BEAR, strategy.BULL, close=25_123.45),
    ])

    # status() reads the feed's snapshot rather than its candle list, so the
    # stub needs the same shape the real feed exposes.
    monkeypatch.setattr(
        feed, "candle_snapshot",
        lambda: (feed.candles(), False), raising=False,
    )
    monkeypatch.setattr(feed, "status", lambda: {"bars": feed.n}, raising=False)

    feed.n = 1
    eng.evaluate_now()
    payload = eng.status()

    assert payload["current_close"] == 25_123.45


def test_the_hero_warmup_counters_belong_to_the_active_run(monkeypatch, tmp_path) -> None:
    """A second strategy must not overwrite the hero panel's bar count.

    ``self.state.warmup_have`` was written by every run, so the last strategy to
    evaluate won. With a GIFT book on 63 bars and a NIFTY book on 310, the hero
    showed "310 closed" beside a GIFT signal - a bar count from a different book
    than the signal it sat next to.
    """
    import bar_source

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
        _verdict(strategy.BULL, strategy.BULL),
    ])

    class ShortSource:
        """A run with far fewer bars than the active one."""

        seconds = 3600

        def refresh_history(self):
            return None

        def candles(self):
            return [{"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1,
                     "low": 1, "close": 1}]

        def status(self):
            return {"bars": 1}

    active = eng.run
    other = engine.StrategyRun("other-book", ShortSource(), params={}, lots=1)
    eng.runs.append(other)

    feed.n = 4
    eng.evaluate_run(active)
    active_bars = eng.state.warmup_have
    assert active_bars == 4

    # The second run must leave the hero's counters alone.
    eng.evaluate_run(other)
    assert eng.state.warmup_have == active_bars, (
        "a non-active run overwrote the hero panel's bar count"
    )


def test_only_a_gift_signalled_run_settles_the_gift_index_book(
    monkeypatch, tmp_path
) -> None:
    """The GIFT index book is priced on GIFT Nifty, so only GIFT may settle it.

    ``_settle_gift_book`` used to be called from ``evaluate_run`` with no check on
    which strategy was running. Once a second strategy existed, every run settled
    the same book: a NIFTY-signalled strategy would drive a GIFT-priced ledger,
    and two runs would each read the other's position as their own.
    """
    import bar_source
    import gift_book

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
    ])
    # _fresh_signal_env stubs settlement out so the order tests stay focused.
    # Put the real method back: the guard is what is under test here.
    monkeypatch.setattr(
        eng, "_settle_gift_book",
        engine.StrategyEngine._settle_gift_book.__get__(eng),
    )

    book = gift_book.get_gift_book()
    before = len(book.positions())

    # A NIFTY-signalled run must not touch the GIFT-priced book.
    nifty_run = engine.StrategyRun(
        "nifty-book", bar_source.NiftyBarSource(), params={}, lots=1
    )
    eng._settle_gift_book("BUY", _verdict(strategy.BULL, strategy.NO_SIGNAL),
                          "2026-11-16T09:30:00+05:30", nifty_run)
    assert len(book.positions()) == before, "a NIFTY run settled the GIFT book"

    # The GIFT-signalled run still does.
    gift_run = engine.StrategyRun(
        "gift-book", bar_source.GiftBarSource(feed), params={}, lots=1
    )
    eng._settle_gift_book("BUY", _verdict(strategy.BULL, strategy.NO_SIGNAL),
                          "2026-11-16T09:30:00+05:30", gift_run)
    assert len(book.positions()) == before + 1, "the GIFT run did not settle its book"


def test_a_flip_exit_reverses_on_the_same_bar(monkeypatch, tmp_path) -> None:
    """A flip closes the old leg and opens the new one on that bar.

    The reversal has to happen here, not on a later bar: the bar that flips the
    pair is the only fresh transition into the new direction, and the bar after it
    is the same state persisting. Deferring it would close the trade and then sit
    flat for the whole of the new trend - taking one trade in two out of every
    swing.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),    # transition -> BUY
        _verdict(strategy.BEAR, strategy.BULL),         # flip -> exit and reverse
        _verdict(strategy.BEAR, strategy.BEAR),         # same state, nothing to do
    ])

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1

    # The flip closes the long and opens the short, in that order.
    feed.n = 2
    flipped = eng.evaluate_now()
    assert flipped["action"] == "SELL"
    assert flipped["result"]["exit"]["kind"] == "SIGNAL_FLIP"
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1

    # The next bar is the same BEAR state persisting: it must not add a second short.
    feed.n = 3
    assert eng.evaluate_now()["action"] is None
    assert len(db.get_open_positions(mode=PAPER)) == 1

    assert [o["transaction_type"] for o in client.orders] == ["BUY", "SELL", "SELL"]


def test_a_new_transition_after_an_exit_enters_again(monkeypatch, tmp_path) -> None:
    """The strategy keeps running: the next genuine signal trades, unattended."""
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),    # BUY
        _verdict(strategy.BEAR, strategy.BULL),        # flip -> exit and reverse
    ])

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    feed.n = 2
    assert eng.evaluate_now()["action"] == "SELL"
    # No manual step: the running strategy is now short, having flipped itself.
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1


def test_a_signal_exit_clears_the_owed_entry_latch(monkeypatch, tmp_path) -> None:
    """The latch belongs to the trade that ended, so it must not survive it.

    A BULL that was still owed when the long was closed must not be honoured
    afterwards - that would re-open the side the exit just killed. What the exit
    does owe is the *new* direction, which is entered on the same bar.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
        _verdict(strategy.BEAR, strategy.BULL),
    ])

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    # Simulate an entry that was owed but not yet filled.
    eng._set_pending_signal(strategy.BULL, "2026-11-16T09:15:00+05:30")
    assert eng._pending_signal == strategy.BULL

    feed.n = 2
    # The flip closes the long and reverses, so the BULL that was still owed is
    # replaced by the new direction rather than honoured.
    assert eng.evaluate_now()["action"] == "SELL"

    assert eng._pending_signal is None
    assert db.get_settings().get(engine.PENDING_SIGNAL_KEY) is None
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1


def test_the_owed_entry_survives_a_restart(monkeypatch, tmp_path) -> None:
    """A deferred entry must not need a manual restart to be honoured.

    The latch is persisted, so a process that bounced between the signal and the
    next in-session bar still knows it owes the entry.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
        _verdict(strategy.BULL, strategy.BULL),
    ])

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    # An entry owed from a bar this bot observed, then the process restarts.
    eng._set_pending_signal(strategy.BULL, "2026-11-16T09:15:00+05:30")
    db.close_position(db.get_open_positions(mode=PAPER)[0]["id"], 25_000.0)

    restarted = engine.StrategyEngine()
    monkeypatch.setattr(restarted, "_settle_gift_book", lambda *a, **k: None)
    restarted._last_bar_at = "seed"

    feed.n = 2
    result = restarted.evaluate_now()

    assert result["action"] == "BUY"
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1


def test_a_rejected_entry_stays_owed_and_is_retried(monkeypatch, tmp_path) -> None:
    """A failed entry must not consume the signal.

    The latch is cleared only once the position is on the ledger, so a rejection
    leaves the entry owed and the next bar tries again - no manual intervention.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
        _verdict(strategy.BULL, strategy.BULL),
    ])

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    eng._set_pending_signal(strategy.BULL, "2026-11-16T09:15:00+05:30")
    db.close_position(db.get_open_positions(mode=PAPER)[0]["id"], 25_000.0)

    # The broker refuses the next attempt.
    monkeypatch.setattr(
        eng, "execute", lambda *a, **k: {"ok": False, "reason": "RMS blocked"}
    )
    feed.n = 2
    assert eng.evaluate_now()["action"] == "BUY"
    assert eng._pending_signal == strategy.BULL, "a rejected entry must stay owed"


def test_an_owed_entry_fills_on_a_later_bar_without_a_new_signal(
    monkeypatch, tmp_path
) -> None:
    """The retry lands on a later bar, so it is not a duplicate of the first try.

    ``signal_id`` is derived from the bar, so a retry on the next bar is a
    genuinely new order rather than a repeat the executor would reject.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.BULL),
    ])
    eng._set_pending_signal(strategy.BULL, "2026-11-16T09:15:00+05:30")

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"
    assert eng._pending_signal is None
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1


def test_the_strategy_rules_are_identical_in_paper_and_live(monkeypatch, tmp_path) -> None:
    """The two modes must not disagree about what a signal is.

    ``strategy`` and ``risk`` never read the execution mode, so this is a guard
    against that ever changing: the same candles must produce the same verdicts
    and the same stop levels whichever book is trading.
    """
    import config
    import risk

    use_test_database(monkeypatch, tmp_path)
    candles = []
    price = 25_000.0
    for i in range(40):
        price += 20 if i < 20 else -20
        candles.append({
            "time": f"2026-11-16T09:{i:02d}:00+05:30",
            "open": price, "high": price + 15, "low": price - 15, "close": price,
        })

    def verdicts(mode):
        config.set_execution_mode(mode)
        return [
            strategy.evaluate(candles[:n])
            for n in range(strategy.required_bars(), len(candles) + 1)
        ]

    paper = verdicts(PAPER)
    live = verdicts(LIVE)

    assert len(paper) == len(live)
    for a, b in zip(paper, live):
        assert (a.signal, a.previous_signal, a.fresh) == (
            b.signal, b.previous_signal, b.fresh
        )
        assert a.values == b.values

    # The exit rule is mode-independent too: ``flat`` is derived from the two
    # signals alone, so the same bar closes a position in either book.
    for a, b in zip(paper, live):
        assert a.flat == b.flat


def test_switching_mode_clears_the_state_that_belongs_to_the_old_mode(
    monkeypatch, tmp_path
) -> None:
    """A mode switch must not carry strategy bookkeeping across.

    The rules are shared, but the bar guard, the overnight reconcile and the
    persisted owed entry are all per-mode. Carried across a switch they would skip
    the new mode's first bar, fire a reconcile on the wrong book, or fill a PAPER
    signal as a real order.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [])

    eng._last_bar_at = "2026-11-16T22:00:00+05:30"
    eng._last_signal = strategy.BULL
    eng._overnight_pending = True
    eng._set_pending_signal(strategy.BULL, "2026-11-16T22:00:00+05:30")

    eng.reset_for_mode(LIVE)

    assert eng._last_bar_at is None, "the new mode must evaluate its own first bar"
    assert eng._last_signal == strategy.NO_SIGNAL
    assert eng._overnight_pending is False
    assert eng._pending_signal is None
    assert db.get_settings().get(engine.PENDING_SIGNAL_KEY) is None


def test_a_mode_switch_leaves_open_positions_alone(monkeypatch, tmp_path) -> None:
    """Resetting the strategy state must not close or reopen anything."""
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [])
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")
    before = db.get_open_positions(mode=PAPER)[0]

    eng.reset_for_mode(LIVE)

    after = db.get_open_positions(mode=PAPER)[0]
    assert after["id"] == before["id"]
    assert after["quantity"] == before["quantity"]
    assert client.orders == []


def test_the_exit_closes_before_the_reversal_opens(monkeypatch, tmp_path) -> None:
    """Close first, then open: the order the broker sees matters.

    Opening the new side before the old one is gone would briefly carry both, and
    the order manager refuses that everywhere else. ``square_off`` must reach the
    ledger first, so the reversal is placed onto a flat book.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),   # BUY
        _verdict(strategy.BEAR, strategy.BULL),        # exit, then reverse
    ])
    manager = engine.get_order_manager()

    seen = []
    real_square_off = manager.square_off

    def spy_square_off(*args, **kwargs):
        result = real_square_off(*args, **kwargs)
        seen.append(("square_off", len(db.get_open_positions(mode=PAPER))))
        return result

    monkeypatch.setattr(manager, "square_off", spy_square_off)

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"

    feed.n = 2
    assert eng.evaluate_now()["action"] == "SELL"

    # At square-off time the long was still the only position; nothing was
    # opened ahead of it.
    assert seen == [("square_off", 0)], seen
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1


def test_both_exit_rules_are_selectable_and_disagree_where_it_matters() -> None:
    """``STRATEGY_EXIT`` picks the rule; the defaults must not drift.

    The two rules differ on exactly one bar shape - the first disagreement - so
    that bar is the whole test. Under ``flip`` it holds the position; under
    ``flat`` it closes it, which is the behaviour this replaced.
    """
    disagree = strategy.StrategyVerdict(
        signal=strategy.NO_SIGNAL, previous_signal=strategy.BULL, close=25_000.0
    )
    flipped = strategy.StrategyVerdict(
        signal=strategy.BEAR, previous_signal=strategy.BULL, close=25_000.0
    )

    assert disagree.exits("flip") is False, "a NO_SIGNAL bar must hold under flip"
    assert disagree.exits("flat") is True, "a NO_SIGNAL bar is the exit under flat"

    assert flipped.exits("flip") is True
    assert flipped.exits("flat") is False, "a reversal is not a disagreement"

    # The shipped default is flip, and an unrecognised value must not silently
    # become the other rule.
    assert strategy.DEFAULTS["exit"] == strategy.EXIT_FLIP == "flip"
    assert disagree.exits("") is False
    assert disagree.exits("nonsense") is False

    # The module-level helper applies the same rule to any verdict-shaped object,
    # which is what lets the engine share it with the tests.
    assert strategy.exits(flipped, "flip") is True
    assert strategy.exits(disagree, "flip") is False
    assert strategy.exits(SimpleNamespace(opposite=True), "flip") is True
    assert strategy.exits(SimpleNamespace(flat=True), "flat") is True


def test_flat_is_a_transition_not_a_state() -> None:
    """``flat`` fires on the first disagreeing bar and not on the ones after it.

    The exit is a transition, exactly like the entry. Without this a run of
    disagreeing bars would be one exit per bar - a stream of no-op square-offs
    and a signal trail that buries the real decision.
    """
    flat = strategy.StrategyVerdict(
        signal=strategy.NO_SIGNAL, previous_signal=strategy.BULL, close=25_000.0
    )
    still_flat = strategy.StrategyVerdict(
        signal=strategy.NO_SIGNAL, previous_signal=strategy.NO_SIGNAL, close=25_000.0
    )
    bull = strategy.StrategyVerdict(
        signal=strategy.BULL, previous_signal=strategy.NO_SIGNAL, close=25_000.0
    )

    assert flat.flat is True
    assert still_flat.flat is False
    assert bull.flat is False
    # A directional bar is never an exit, however it got there.
    assert bull.confirmed is True


def test_a_signal_exit_that_does_not_fill_is_retried(
    monkeypatch, tmp_path
) -> None:
    """An exit the broker did not complete must not be reported as done.

    The position is still on the ledger, so the next bar tries again. Reporting
    it as closed would leave a live position the bot believes is flat - the worst
    state this path can produce.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),    # BUY
        _verdict(strategy.BEAR, strategy.BULL),        # exit attempt
        _verdict(strategy.BEAR, strategy.BULL),        # retry
    ])
    manager = engine.get_order_manager()

    feed.n = 1
    assert eng.evaluate_now()["action"] == "BUY"

    # The broker refuses the square-off on the first attempt only.
    real_square_off = manager.square_off
    attempts = []

    def flaky_square_off(*args, **kwargs):
        attempts.append(kwargs.get("reason"))
        if len(attempts) == 1:
            return order_manager.ExecutionResult(
                False, "FLAT", PAPER, "NIFTY26NOVFUT", reason="broker rejected"
            )
        return real_square_off(*args, **kwargs)

    monkeypatch.setattr(manager, "square_off", flaky_square_off)

    feed.n = 2
    assert eng.evaluate_now()["action"] is None
    assert len(db.get_open_positions(mode=PAPER)) == 1, "the position is still open"

    # The next bar retries the exit, and once it confirms the reversal follows on
    # the same bar - the retry bar is still a BEAR flip against the long.
    feed.n = 3
    assert eng.evaluate_now()["action"] == "SELL"
    assert [p["position_key"] for p in db.get_open_positions(mode=PAPER)] == [-1]
    assert attempts == ["SIGNAL_FLIP", "SIGNAL_FLIP"]


def test_evaluate_now_holds_through_a_disagreeing_bar(
    monkeypatch, tmp_path
) -> None:
    """A ``NO_SIGNAL`` bar is not an exit - the position is carried through it.

    Only a flip to the opposite direction closes a trade. The pair disagreeing
    says nothing about a position already on, and cutting on the first indecisive
    bar is what the flip exit exists to avoid.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    verdict = SimpleNamespace(
        signal=strategy.NO_SIGNAL, previous_signal=strategy.BULL, fresh=False,
        opposite=False, flat=True, detail="test", bars=100, conditions={},
        values={}, close=25_000.0,
        signal_candle={"time": "2026-11-16T09:30:00+05:30"},
    )

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: verdict)
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BULL

    feed.n = 1
    result = eng.evaluate_now()

    assert result["action"] is None
    # The position is untouched: no order, no closed row.
    assert len(db.get_open_positions(mode=PAPER)) == 1
    assert client.orders == [], "a disagreeing bar must not reach the broker"
    # The strategy keeps running: nothing is paused.
    assert manager.paused is False


def test_strategy_exit_flat_restores_the_disagreeing_bar_exit(
    monkeypatch, tmp_path
) -> None:
    """``STRATEGY_EXIT=flat`` must change the live rule, not just the backtest.

    The same bar that is held under the default ``flip`` closes the position when
    the operator selects ``flat``. Without this the setting is a comment.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    verdict = SimpleNamespace(
        signal=strategy.NO_SIGNAL, previous_signal=strategy.BULL, fresh=False,
        opposite=False, flat=True, detail="test", bars=100, conditions={},
        values={}, close=25_000.0,
        signal_candle={"time": "2026-11-16T09:30:00+05:30"},
    )

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {"exit": "flat"})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: verdict)
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BULL

    feed.n = 1
    result = eng.evaluate_now()

    assert result["action"] == "FLAT"
    assert db.get_open_positions(mode=PAPER) == []
    assert client.orders, "the exit must reach the broker"
    assert manager.paused is False


def test_a_run_of_opposite_bars_exits_only_once(monkeypatch, tmp_path) -> None:
    """``opposite`` is a transition, so a run of opposite bars is one exit.

    Without this the engine would try to close an already-flat book on every bar
    the pair stayed reversed, which is a stream of no-op square-offs and a signal
    trail full of noise.
    """
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    # The first bar is the transition; the second is the same state persisting.
    verdicts = iter([
        SimpleNamespace(
            signal=strategy.BEAR, previous_signal=strategy.BULL, fresh=True,
            opposite=True, flat=False, detail="test", bars=100, conditions={},
            values={}, close=25_000.0,
            signal_candle={"time": "2026-11-16T09:30:00+05:30"},
        ),
        SimpleNamespace(
            signal=strategy.BEAR, previous_signal=strategy.BEAR, fresh=False,
            opposite=False, flat=False, detail="test", bars=100, conditions={},
            values={}, close=25_000.0,
            signal_candle={"time": "2026-11-16T09:45:00+05:30"},
        ),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BULL

    feed.n = 1
    # The first bar exits and reverses; that is one exit, one entry.
    assert eng.evaluate_now()["action"] == "SELL"
    orders_after_flip = len(client.orders)

    feed.n = 2
    second = eng.evaluate_now()
    assert second["action"] is None
    assert len(client.orders) == orders_after_flip, (
        "a persisting opposite state must not exit or enter again"
    )


def test_evaluate_now_flips_a_paper_position_on_the_opposite_verdict(
    monkeypatch, tmp_path
) -> None:
    """The mirror of the entry case: a long is flipped to a short on BEAR."""
    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    _pin_signal_clock(monkeypatch, datetime(2026, 11, 16, 9, 15, tzinfo=IST))
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")

    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    verdicts = iter([
        SimpleNamespace(signal=strategy.NO_SIGNAL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BEAR, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    monkeypatch.setattr(eng, "_settle_gift_book", lambda *a, **k: None)
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BEAR

    feed.n = 1
    assert eng.evaluate_now()["action"] is None
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1

    feed.n = 2
    result = eng.evaluate_now()

    assert result["action"] == "SELL"
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1


def test_evaluate_now_drives_the_gift_index_book_after_hours(
    monkeypatch, tmp_path
) -> None:
    """The index book must act on the same bars whatever the NSE session is.

    GIFT Nifty trades ~20 hours while NSE F&O trades 6 h 15 m, so gating the index
    book on the 09:15 open would make it react to the *overnight* verdict while
    the futures book waited for the morning's - the two venues would disagree on
    when they acted, which is the one thing that comparison exists not to do. A
    bar with no signal must leave the index book alone: there is no neutral exit,
    so its position is carried until the opposite pair confirms or its stop trips.
    """
    import gift_book

    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    book = gift_book.GiftBook(path=str(tmp_path / "gift.db"), enabled=True, units=65)
    monkeypatch.setattr(gift_book, "get_gift_book", lambda strategy_id=None: book)

    # After hours: the futures decision is held, the index book is not.
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": False, "reason": "Post-market - closed at 15:30 IST"},
    )
    bars = [
        {"time": "2026-11-16T15:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T16:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T16:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    # BULL is a transition on the first bar, then the state persists; the third
    # bar flips the pair into BEAR, which is the exit - a disagreeing bar before
    # it is carried, because NO_SIGNAL is not an exit.
    verdicts = iter([
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False, flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.NO_SIGNAL, previous_signal=strategy.BULL,
                        opposite=False, flat=True,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BEAR, previous_signal=strategy.NO_SIGNAL,
                        opposite=False, flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.NO_SIGNAL

    feed.n = 1
    opened = eng.evaluate_now()
    assert opened["action"] is None
    assert opened["gift_book"]["action"] == "BUY"
    assert book.net_units() == 65
    # The futures book did nothing: it is out of session.
    assert client.orders == []

    # The pair merely disagrees: the book keeps its long. There is no neutral
    # exit, so a NO_SIGNAL bar is carried exactly as the futures leg carries it.
    feed.n = 2
    held = eng.evaluate_now()
    assert held["gift_book"] is None
    assert book.net_units() == 65

    # The pair confirms the other way: the book flips, still without the futures
    # book, which is out of session.
    feed.n = 3
    flipped = eng.evaluate_now()
    assert flipped["gift_book"]["action"] == "SELL"
    assert book.net_units() == -65
    assert client.orders == []


def test_evaluate_now_drives_the_gift_index_book_in_live(monkeypatch, tmp_path) -> None:
    """LIVE must not change the index book's behavior - it is paper in every mode.

    GIFT Nifty has no tradeable contract, so the mode the futures book is in has
    no bearing on the index book: it follows the same verdict either way.
    """
    import gift_book

    eng, manager, client = _overnight_env(monkeypatch, tmp_path, mode=LIVE)
    book = gift_book.GiftBook(path=str(tmp_path / "gift.db"), enabled=True, units=65)
    monkeypatch.setattr(gift_book, "get_gift_book", lambda strategy_id=None: book)

    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": True, "reason": "Market open"},
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    verdicts = iter([
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BEAR, previous_signal=strategy.BULL,
                        opposite=True,
                        flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.NO_SIGNAL

    feed.n = 1
    entered = eng.evaluate_now()
    assert entered["gift_book"]["action"] == "BUY"
    assert book.net_units() == 65

    # The pair flips, so the book flattens - the exit rule is the same in LIVE as
    # in PAPER, because the index book is paper in every mode.
    feed.n = 2
    exited = eng.evaluate_now()
    assert exited["gift_book"]["action"] == "SELL"
    assert book.net_units() == -65


def test_a_book_may_not_open_against_another_books_position(monkeypatch, tmp_path) -> None:
    """The invariant that makes parallel trading safe.

    Zerodha nets long and short in one contract into a single position, so an
    opposing entry is not a second trade - it cancels the first. The aggregator
    must refuse it, and the reason must say why rather than reading as a generic
    rejection.
    """
    import portfolio

    use_test_database(monkeypatch, tmp_path)
    portfolio.reset_aggregator()

    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")

    aggregator = portfolio.PortfolioAggregator(max_lots=4)
    verdict = aggregator.authorise(
        "nifty-adx", "SELL", "NIFTY26NOVFUT", 1, lot_size=65, mode=PAPER,
    )

    assert verdict.allowed is False
    assert "opposing" in verdict.reason
    assert "gift-flip long" in verdict.reason
    # The same side is fine: adding to a direction is netting, not cancelling.
    assert aggregator.authorise(
        "nifty-adx", "BUY", "NIFTY26NOVFUT", 1, lot_size=65, mode=PAPER,
    ).allowed is True


def test_exits_are_never_blocked_by_the_portfolio(monkeypatch, tmp_path) -> None:
    """A trapped position is worse than an over-sized one.

    The opposing-leg rule must not be able to prevent a square-off, or the bot
    would be unable to get out of the very state the rule exists to avoid.
    """
    import portfolio

    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="nifty-adx")

    aggregator = portfolio.PortfolioAggregator(max_lots=4)
    for action in ("FLAT", "PANIC"):
        assert aggregator.authorise(
            "gift-flip", action, "NIFTY26NOVFUT", 1, lot_size=65, mode=PAPER,
        ).allowed is True


def test_combined_lots_stay_inside_the_portfolio_budget(monkeypatch, tmp_path) -> None:
    """MAX_OPEN_LOTS is a portfolio budget, not a per-strategy one.

    Two runs each at the limit would otherwise double the exposure the operator
    configured.
    """
    import portfolio

    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65 * 3, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")

    aggregator = portfolio.PortfolioAggregator(max_lots=4)
    # 3 lots held + 2 more would be 5, over the budget of 4.
    verdict = aggregator.authorise(
        "nifty-adx", "BUY", "NIFTY26NOVFUT", 2, lot_size=65, mode=PAPER,
    )
    assert verdict.allowed is False
    assert "MAX_OPEN_LOTS" in verdict.reason
    assert verdict.projected_lots == 5

    # One more lot fits exactly.
    assert aggregator.authorise(
        "nifty-adx", "BUY", "NIFTY26NOVFUT", 1, lot_size=65, mode=PAPER,
    ).allowed is True


def test_exposure_reports_each_book_and_the_netted_reality(monkeypatch, tmp_path) -> None:
    """Both figures are shown because they answer different questions.

    The per-strategy rows say what each book believes it holds; the netted row is
    what the broker will report. A divergence between them is the fact an
    operator needs to see.
    """
    import portfolio

    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="nifty-adx")

    view = portfolio.PortfolioAggregator(max_lots=4).exposure(PAPER)

    assert {r["strategy_id"] for r in view["strategies"]} == {"gift-flip", "nifty-adx"}
    assert view["netted"]["NIFTY26NOVFUT"] == 130
    assert view["gross_units"] == 130
    # Both books hold the same contract, which the dashboard must be able to flag.
    assert view["conflicts"] == ["NIFTY26NOVFUT"]


def test_every_run_reports_its_own_state(monkeypatch, tmp_path) -> None:
    """A second strategy must report its own numbers, not the active run's.

    The engine's EngineState is the single-strategy dashboard's view and is still
    written. But /api/strategies reads each run, and if only the engine is updated
    every strategy except the first reports zeros - a working book that looks
    broken.
    """
    use_test_database(monkeypatch, tmp_path)
    _isolate_gift_book(monkeypatch, tmp_path)

    import bar_source
    import strategy_run

    eng = engine.StrategyEngine()
    monkeypatch.setattr(
        engine, "market_status", lambda: {"is_open": True, "reason": "Market open"}
    )
    monkeypatch.setattr(engine, "required_bars", lambda: 1)

    bars = [
        {"time": "2026-11-18T10:15:00+05:30", "open": 1, "high": 2, "low": 1, "close": 2},
        {"time": "2026-11-18T11:15:00+05:30", "open": 2, "high": 3, "low": 2, "close": 3},
    ]

    class StubSource:
        key = "STUB"
        seconds = 3600

        def refresh_history(self, force=False):
            return len(bars)

        def candles(self):
            return list(bars)

        def bar_count(self):
            return len(bars)

        def status(self):
            return {"source": "StubSource", "bars": len(bars)}

    second = strategy_run.StrategyRun(
        "nifty-adx", StubSource(), params={"exit": "flip"}, lots=1,
    )
    eng.runs.append(second)
    monkeypatch.setattr(
        engine.strategy, "evaluate",
        lambda candles, params: strategy.StrategyVerdict(
            strategy.BEAR, 25_000.0, previous_signal=strategy.BULL
        ),
    )

    eng.evaluate_run(second)

    assert second.state.evaluations == 1
    assert second.state.ready is True
    assert second.state.last_signal == strategy.BEAR
    assert second.state.bars == 2
    assert second.state.last_bar_at == "2026-11-18T11:15:00+05:30"
    assert second.last_verdict is not None

    # And the run's status surfaces those numbers rather than zeros.
    reported = second.status()["state"]
    assert reported["evaluations"] == 1
    assert reported["last_signal"] == strategy.BEAR


def test_dashboard_calls_no_undefined_function() -> None:
    """Every bare call in app.js must resolve.

    A removed feature can take its neighbours with it: deleting the trailing-stop
    panel also deleted sendTestAlert, renderAlerts and renderDailyLoss, which the
    page still called. A ReferenceError inside init() aborts the rest of it, so
    the dashboard rendered as a wall of em-dashes - and because the asset URL was
    hardcoded, browsers kept replaying the broken file from cache.

    Static analysis rather than a browser, so it needs no server and catches the
    bug at the commit that introduces it.
    """
    import re
    from pathlib import Path

    app_js = Path(__file__).resolve().parent.parent / "bot" / "web" / "app.js"
    src = app_js.read_text(encoding="utf-8")

    defined: set[str] = set()
    defined |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", src))
    # "let signalText, signalClass;" declares two names, so each declarator is
    # read separately rather than only the first.
    for decl in re.findall(r"\b(?:const|let|var)\s+([^;\n]*)", src):
        for part in decl.split(","):
            name = re.match(r"\s*([A-Za-z_$][\w$]*)", part)
            if name:
                defined.add(name.group(1))
    for group in re.findall(r"\b(?:const|let|var)\s*[\{\[]([^\}\]]*)[\}\]]\s*=", src):
        defined |= set(re.findall(r"([A-Za-z_$][\w$]*)", group))
    for params in re.findall(r"\(([^)]*)\)\s*(?:=>|\{)", src):
        # The last parameter is followed by end-of-string, not a delimiter, so
        # the lookahead has to accept that or single-argument functions such as
        # withBusy(fn) leave `fn` looking undefined.
        defined |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*(?=[,=)]|$)", params))
    defined |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=>", src))

    builtins = {
        "window", "document", "console", "Math", "JSON", "Object", "Array", "String",
        "Number", "Boolean", "Date", "Promise", "Set", "Map", "Error", "RegExp",
        "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
        "decodeURIComponent", "setTimeout", "clearTimeout", "setInterval",
        "clearInterval", "requestAnimationFrame", "fetch", "EventSource", "URL",
        "URLSearchParams", "FormData", "Blob", "FileReader", "Intl", "localStorage",
        "sessionStorage", "navigator", "location", "history", "alert", "confirm",
        "prompt", "Symbol", "WeakMap", "Reflect", "Proxy", "BigInt",
        "structuredClone", "AbortController", "btoa", "atob", "queueMicrotask",
        "Uint8Array", "ArrayBuffer", "TextEncoder", "TextDecoder",
        # Browser globals constructed with `new` (Option for <select> entries).
        "Option",
        # keywords that look like calls in a regex scan
        "if", "for", "while", "switch", "catch", "return", "function", "typeof",
        "await", "new", "delete", "in", "of", "void", "yield", "async", "try",
        "else", "do", "case", "throw", "instanceof", "break", "continue",
        "default", "const", "let", "var", "class", "extends", "super", "this",
        "null", "true", "false", "undefined", "static", "get", "set",
    }

    # Prose mentions function names, and template strings contain words like
    # "error (3)" and "fill (s) seen". All of those look like calls to a regex,
    # so they are removed before the scan. Single-line comments go first, then
    # block comments, then quoted and template strings.
    code = re.sub(r"//[^\n]*", "", src)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"`(?:[^`\\]|\\.|\$\{[^}]*\})*`", "``", code)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", code)
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)
    # Regex literals hold words like CRITICAL that are not identifiers. Only
    # strip where a value is expected, so division is left alone.
    code = re.sub(
        r"(?<=[=(,:\[!&|?{};])\s*/(?:[^/\\\n\[]|\\.|\[(?:[^\]\\]|\\.)*\])+/[gimsuy]*",
        "/RE/",
        code,
    )

    # A declaration such as "function sendTestAlert()" also matches the call
    # pattern, which would let a function vouch for itself. Drop declarations
    # first so only genuine call sites remain.
    code = re.sub(r"\bfunction\s+[A-Za-z_$][\w$]*", "function", code)

    # Both call forms matter. The dashboard broke on a bare reference -
    # addEventListener('click', sendTestAlert) - which never has parentheses,
    # so scanning only for name( would have missed the very bug this guards.
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code))
    referenced = set(
        re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*(?=[,;)\]}=]|$)", code)
    )
    undefined = sorted((called | referenced) - defined - builtins)

    assert undefined == [], (
        "app.js uses names that are not defined anywhere: "
        + ", ".join(undefined)
    )


def test_every_element_the_dashboard_writes_to_exists() -> None:
    """A removed panel must take its JavaScript with it.

    Restructuring index.html without updating app.js leaves the script writing to
    ids that no longer exist. Nothing throws - setText and $ both no-op on a
    missing element - so the page renders as a wall of em-dashes with no error
    anywhere. That is exactly how the dashboard broke before, and it is invisible
    to every other test.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    # Every way the script addresses an element by id.
    js_ids = set(re.findall(r"""\$\(\s*['"]([^'"]+)['"]\s*\)""", js))
    js_ids |= set(re.findall(r"""setText\(\s*['"]([^'"]+)['"]""", js))
    js_ids |= set(re.findall(r"""setPill\(\s*['"]([^'"]+)['"]""", js))

    missing = sorted(js_ids - html_ids)

    assert missing == [], (
        "app.js writes to ids that index.html does not define: "
        + ", ".join(missing)
    )


def test_the_dashboard_page_has_no_duplicate_element_ids() -> None:
    """Duplicate ids make half the page unreachable.

    A bad merge left two copies of the alerting and session-loss-cap panels in
    index.html. getElementById returns the first match, so the second copy sat
    there rendering em-dashes forever - the page looked half-broken while the
    JavaScript was in fact correct.
    """
    import re
    from collections import Counter
    from pathlib import Path

    html = (
        Path(__file__).resolve().parent.parent / "bot" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    ids = re.findall(r'\bid="([^"]+)"', html)
    dupes = sorted(name for name, count in Counter(ids).items() if count > 1)

    assert dupes == [], "index.html repeats these element ids: " + ", ".join(dupes)


def test_the_dashboard_page_uses_a_hashed_asset_version() -> None:
    """A hardcoded ?v= pins browsers to a stale bundle forever.

    The page shipped ?v=trader-40 for its whole life, so a changed app.js was
    never picked up until someone cleared their cache - which is how a fixed
    dashboard can still look broken.
    """
    from pathlib import Path

    html = (
        Path(__file__).resolve().parent.parent / "bot" / "web" / "index.html"
    ).read_text(encoding="utf-8")

    assert "?v=trader-40" in html, "the placeholder is what the server rewrites"
    # And the server must do the rewriting.
    bot_py = (
        Path(__file__).resolve().parent.parent / "bot" / "bot.py"
    ).read_text(encoding="utf-8")
    assert "_asset_version()" in bot_py
    assert "no-store" in bot_py


def test_one_configured_strategy_is_the_default(monkeypatch) -> None:
    """An unchanged .env must behave exactly as it did before this existed.

    The parser is the migration boundary: if it invented a second strategy, or
    dropped the first, every existing deployment would change on upgrade.
    """
    import config

    for key in list(os.environ):
        if key.startswith("STRATEGY_"):
            monkeypatch.delenv(key, raising=False)

    configs = config.strategy_configs()

    assert len(configs) == 1
    first = configs[0]
    assert first.source == "gift"
    assert first.exit == "flip"
    assert first.interval == config.Settings().gift_candle_interval
    assert first.enabled is True
    # Filters are off unless asked for.
    assert first.filter_adx == 0.0
    assert first.filter_ma == 0


def test_a_second_strategy_is_purely_configuration(monkeypatch) -> None:
    """Adding the NIFTY book must not require a code change."""
    import config

    for key in list(os.environ):
        if key.startswith("STRATEGY_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRATEGY_2_SOURCE", "nifty")
    monkeypatch.setenv("STRATEGY_2_ID", "nifty-adx")
    monkeypatch.setenv("STRATEGY_2_FILTER_ADX", "15")
    monkeypatch.setenv("STRATEGY_2_FILTER_MA", "100")
    monkeypatch.setenv("STRATEGY_2_LOTS", "2")

    configs = config.strategy_configs()

    assert [c.id for c in configs] == ["Surikbs", "nifty-adx"]
    second = configs[1]
    assert second.source == "nifty"
    assert second.lots == 2
    assert second.filter_adx == 15.0
    assert second.filter_ma == 100
    assert second.strategy_params()["exit"] == "flip"


def test_a_second_strategy_must_declare_its_source(monkeypatch) -> None:
    """A numbered strategy with no SOURCE would otherwise duplicate the first.

    Two runs on one book would double every order and fight over the same
    positions, which is worse than not starting.
    """
    import config

    for key in list(os.environ):
        if key.startswith("STRATEGY_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRATEGY_2_ID", "typo")

    assert [c.id for c in config.strategy_configs()] == ["Surikbs"]


def test_a_disabled_strategy_is_not_started(monkeypatch) -> None:
    import config

    for key in list(os.environ):
        if key.startswith("STRATEGY_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STRATEGY_2_SOURCE", "nifty")
    monkeypatch.setenv("STRATEGY_2_ENABLED", "0")

    assert [c.id for c in config.enabled_strategies()] == ["Surikbs"]


def test_an_unknown_source_is_rejected_rather_than_defaulted(monkeypatch) -> None:
    """A typo must fail at startup, not trade the wrong instrument."""
    import config

    cfg = config.StrategyConfig(
        index=1, id="x", source="banknifty", interval="60minute"
    )
    with pytest.raises(ValueError, match="banknifty"):
        config.build_bar_source_for(cfg)


def test_two_runs_do_not_share_an_owed_entry(monkeypatch, tmp_path) -> None:
    """Two books must not overwrite each other's deferred signal.

    The latch used to live under one global settings key, so a second strategy
    would silently adopt the first's owed entry - opening a trade on a signal it
    never saw. The key is scoped to the strategy id for exactly this reason.
    """
    import bar_source
    import strategy_run

    use_test_database(monkeypatch, tmp_path)

    gift = strategy_run.StrategyRun("gift-flip", bar_source.GiftBarSource())
    nifty = strategy_run.StrategyRun("nifty-adx", bar_source.NiftyBarSource())

    assert gift.pending_key != nifty.pending_key

    gift.set_pending_signal(strategy.BULL, "2026-11-16T09:15:00")
    nifty.set_pending_signal(strategy.BEAR, "2026-11-16T10:15:00")

    stored = db.get_settings()
    assert stored[gift.pending_key] == strategy.BULL
    assert stored[nifty.pending_key] == strategy.BEAR

    # A fresh process resumes each book's own intent, not the other's.
    fresh_gift = strategy_run.StrategyRun("gift-flip", bar_source.GiftBarSource())
    fresh_nifty = strategy_run.StrategyRun("nifty-adx", bar_source.NiftyBarSource())
    fresh_gift.load_pending_signal()
    fresh_nifty.load_pending_signal()

    assert fresh_gift.pending_signal == strategy.BULL
    assert fresh_nifty.pending_signal == strategy.BEAR

    # Clearing one leaves the other's debt intact.
    gift.set_pending_signal(None)
    assert db.get_settings().get(gift.pending_key) is None
    assert db.get_settings()[nifty.pending_key] == strategy.BEAR


def test_a_second_strategy_does_not_inherit_a_legacy_latch(monkeypatch, tmp_path) -> None:
    """The pre-upgrade key belongs to the original book, and only to it.

    A ledger written before the latch was scoped holds a bare key. Letting a new
    strategy adopt it would make it resume a trade it never took.
    """
    import bar_source
    import strategy_run

    use_test_database(monkeypatch, tmp_path)
    db.set_settings(
        {
            strategy_run.PENDING_SIGNAL_KEY: strategy.BULL,
            strategy_run.PENDING_SIGNAL_BAR_KEY: "2026-11-16T09:15:00",
        }
    )

    legacy = strategy_run.StrategyRun(db.DEFAULT_STRATEGY_ID, bar_source.GiftBarSource())
    legacy.load_pending_signal()
    assert legacy.pending_signal == strategy.BULL, "the original book keeps its debt"

    second = strategy_run.StrategyRun("nifty-adx", bar_source.NiftyBarSource())
    second.load_pending_signal()
    assert second.pending_signal is None, "a new book must start flat"


def test_run_state_is_reported_per_strategy() -> None:
    import bar_source
    import strategy_run

    run = strategy_run.StrategyRun(
        "nifty-adx", bar_source.NiftyBarSource(),
        params={"exit": "flip"}, lots=2, display_name="NIFTY ADX",
    )
    state = run.status()

    assert state["id"] == "nifty-adx"
    assert state["name"] == "NIFTY ADX"
    assert state["lots"] == 2
    assert state["exit"] == "flip"
    assert state["symbol"] == "NSE:NIFTY 50"
    assert state["seconds"] == 3600
    assert state["state"]["evaluations"] == 0


def test_nifty_source_yields_closed_bars_the_strategy_accepts(monkeypatch) -> None:
    """The second source must produce exactly the series shape the engine wants.

    ``strategy.evaluate`` is the real consumer, so it is the real test: if the
    source's dicts are malformed or the forming bar leaks in, this fails.
    """
    import bar_source

    rows = []
    start = datetime(2026, 11, 10, 9, 15, tzinfo=IST)
    for i in range(60):
        opened = start + timedelta(hours=i)
        rows.append({
            "date": opened.replace(tzinfo=None),
            "open": 25_000.0 + i, "high": 25_010.0 + i,
            "low": 24_990.0 + i, "close": 25_005.0 + i,
        })

    class FakeClient:
        def historical_data(self, instrument_token, from_date, to_date, interval):
            assert instrument_token == bar_source.NIFTY_SPOT_TOKEN
            assert interval == "60minute"
            return list(rows)

    src = bar_source.NiftyBarSource(interval="60minute", client=FakeClient())
    src._is_forming = lambda bar_at, now: False   # no wall-clock dependence
    assert src.refresh_history(force=True) == 60

    candles = src.candles()
    assert [c["time"] for c in candles] == sorted(c["time"] for c in candles)
    assert src.seconds == 3600
    verdict = strategy.evaluate(candles, {})
    assert verdict.signal in {strategy.BULL, strategy.BEAR, strategy.NO_SIGNAL}
    assert verdict.bars == 60


def test_nifty_source_drops_the_forming_bar(monkeypatch) -> None:
    """Kite includes the bar still being built; "closed bar" has to stay true.

    A forming bar would let the strategy decide on a candle that can still
    change, which is the same class of bug the candle builder guards against.
    """
    import bar_source

    now = datetime(2026, 11, 18, 20, 0, tzinfo=IST)   # 20:00, market shut
    rows = [
        {"date": datetime(2026, 11, 18, 14, 15), "open": 1, "high": 2, "low": 1, "close": 2},
        {"date": datetime(2026, 11, 18, 15, 15), "open": 2, "high": 3, "low": 2, "close": 3},
    ]

    class FakeClient:
        def historical_data(self, **_kwargs):
            return list(rows)

    src = bar_source.NiftyBarSource(interval="60minute", client=FakeClient())
    # The 15:15 bucket closes at 16:15, which has passed; the 19:15 one has not.
    assert src._is_forming("2026-11-18 15:15:00", now) is False
    assert src._is_forming("2026-11-18 19:15:00", now) is True

    src._is_forming = lambda bar_at, _now: bar_at.endswith("19:15:00")
    rows.append({"date": datetime(2026, 11, 18, 19, 15), "open": 3, "high": 4, "low": 3, "close": 4})
    src.refresh_history(force=True)
    assert src.candles()[-1]["time"] == "2026-11-18 15:15:00"


def test_a_failed_nifty_refresh_keeps_the_previous_series(monkeypatch) -> None:
    """An empty series reads as "warming up" and would silently stop the strategy.

    A dead token must degrade to stale bars, not to no bars.
    """
    import bar_source

    rows = [
        {"date": datetime(2026, 11, 18, 10, 15) + timedelta(hours=i),
         "open": 1, "high": 2, "low": 1, "close": 2}
        for i in range(40)
    ]
    state = {"fail": False}

    class FakeClient:
        def historical_data(self, **_kwargs):
            return [] if state["fail"] else list(rows)

    src = bar_source.NiftyBarSource(interval="60minute", client=FakeClient())
    src._is_forming = lambda bar_at, now: False
    assert src.refresh_history(force=True) == 40

    state["fail"] = True
    assert src.refresh_history(force=True) == 40, "the old series must survive"
    assert src.status()["failures"] == 1
    assert src.status()["error"]


def test_both_sources_satisfy_the_protocol() -> None:
    import bar_source

    assert isinstance(bar_source.GiftBarSource(), bar_source.BarSource)
    assert isinstance(bar_source.NiftyBarSource(), bar_source.BarSource)


def test_one_strategy_exit_cannot_close_anothers_position(monkeypatch, tmp_path) -> None:
    """The whole reason for strategy_id: one book's exit must not hit the other.

    Both strategies trade the same front-month contract, so ``square_off``
    without the id would liquidate both legs. In LIVE that is a real order
    against a position the other strategy believes it still holds.
    """
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)

    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="nifty-adx")

    manager = order_manager.OrderManager(client=SimpleNamespace(
        get_ltp=lambda symbol, max_age=None: 25_000.0,
        place_order=lambda **kw: {"order_id": "1", "status": "COMPLETE",
                                  "filled_quantity": kw.get("quantity")},
    ))

    # A scoped square-off closes only that book's row.
    result = manager.square_off("SIGNAL_FLIP", mode=PAPER, strategy_id="gift-flip")

    assert result.ok
    assert db.get_open_positions(mode=PAPER, strategy_id="gift-flip") == []
    remaining = db.get_open_positions(mode=PAPER, strategy_id="nifty-adx")
    assert [p["position_key"] for p in remaining] == [-1], (
        "the other strategy's short must survive this book's exit"
    )


def test_netting_does_not_read_the_other_strategys_leg(monkeypatch, tmp_path) -> None:
    """Opposing books must not net to zero and blind each other.

    ``_current_net_lots`` used to sum every row for the symbol, so a long in one
    book and a short in the other looked like *no position at all*. Each strategy
    then saw "already flat" while it was in fact exposed.
    """
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)

    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="nifty-adx")

    manager = order_manager.OrderManager(client=SimpleNamespace())

    assert manager._current_net_lots("NIFTY26NOVFUT", PAPER, 65, "gift-flip") == 1
    assert manager._current_net_lots("NIFTY26NOVFUT", PAPER, 65, "nifty-adx") == -1
    # Unscoped is the portfolio net, which is genuinely flat here - and is only
    # ever the right answer for a portfolio-wide view, never for an entry.
    assert manager._current_net_lots("NIFTY26NOVFUT", PAPER, 65) == 0


def test_panic_closes_every_strategy(monkeypatch, tmp_path) -> None:
    """The operator's big red button is deliberately not strategy-scoped.

    A square-off that left one book open because it was called from the other
    would be the worst possible failure of this feature.
    """
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)

    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="nifty-adx")

    manager = order_manager.OrderManager(client=SimpleNamespace(
        get_ltp=lambda symbol, max_age=None: 25_000.0,
        place_order=lambda **kw: {"order_id": "1", "status": "COMPLETE",
                                  "filled_quantity": kw.get("quantity")},
    ))

    assert manager.square_off("PANIC", mode=PAPER).ok
    assert db.get_open_positions(mode=PAPER) == []


def test_an_entry_is_tagged_with_the_strategy_that_placed_it(monkeypatch, tmp_path) -> None:
    """The order and the position must both carry the id, or the exit path
    cannot find what the entry created."""
    use_test_database(monkeypatch, tmp_path)

    position_id = db.open_position(
        "NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE",
        strategy_id="nifty-adx",
    )
    row = [p for p in db.get_open_positions(mode=PAPER, strategy_id="nifty-adx")
           if p["id"] == position_id][0]
    assert row["strategy_id"] == "nifty-adx"

    db.record_order(
        symbol="NIFTY26NOVFUT", transaction_type="BUY", quantity=65, price=25_000.0,
        order_type="MARKET", product="NRML", status="COMPLETE", mode=PAPER,
        source="ENGINE", strategy_id="nifty-adx", position_id=position_id,
    )
    with db.session() as conn:
        tagged = conn.execute(
            "SELECT strategy_id FROM orders WHERE position_id = ?", (position_id,)
        ).fetchall()
    assert [r["strategy_id"] for r in tagged] == ["nifty-adx"]

    # And the trade row inherits from the position when it closes.
    db.close_position(position_id, 25_100.0, exit_reason="SIGNAL_FLIP")
    with db.session() as conn:
        trade = conn.execute(
            "SELECT strategy_id FROM trades WHERE position_id = ?", (position_id,)
        ).fetchone()
    assert trade["strategy_id"] == "nifty-adx"


def test_strategy_id_is_added_to_an_existing_ledger(monkeypatch, tmp_path) -> None:
    """An upgraded deployment must not lose attribution of its history.

    The column is new, so an older database has rows without it. They all belong
    to the strategy that existed then, and backfilling is what lets a scoped
    query keep returning them instead of making the whole history vanish.
    """
    import sqlite3

    path = str(tmp_path / "old.db")
    # A ledger in the pre-strategy shape: no strategy_id anywhere.
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, position_key INTEGER NOT NULL,
            quantity INTEGER NOT NULL, entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'OPEN',
            realized_pnl REAL NOT NULL DEFAULT 0, fees REAL NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'PAPER',
            source TEXT NOT NULL DEFAULT 'WEBHOOK'
        );
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            symbol TEXT NOT NULL, transaction_type TEXT NOT NULL,
            quantity INTEGER NOT NULL, price REAL, order_type TEXT NOT NULL,
            product TEXT NOT NULL, status TEXT NOT NULL, mode TEXT NOT NULL,
            source TEXT NOT NULL
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL,
            symbol TEXT NOT NULL, direction TEXT NOT NULL, quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL, exit_price REAL NOT NULL,
            entry_time TEXT NOT NULL, exit_time TEXT NOT NULL,
            gross_pnl REAL NOT NULL, fees REAL NOT NULL, net_pnl REAL NOT NULL,
            mode TEXT NOT NULL
        );
        CREATE TABLE signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            action TEXT NOT NULL, payload TEXT, accepted INTEGER NOT NULL, reason TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO positions (symbol, position_key, quantity, entry_price, entry_time) "
        "VALUES ('NIFTY26NOVFUT', 1, 65, 25000.0, '2026-11-01T09:15:00')"
    )
    conn.execute(
        "INSERT INTO orders (ts, symbol, transaction_type, quantity, order_type, product, "
        "status, mode, source) VALUES ('2026-11-01T09:15:00','NIFTY26NOVFUT','BUY',65,"
        "'MARKET','NRML','COMPLETE','PAPER','ENGINE')"
    )
    conn.execute(
        "INSERT INTO signals (ts, action, payload, accepted, reason) VALUES "
        "('2026-11-01T09:15:00','BUY','x',1,'legacy')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        db, "settings", SimpleNamespace(db_path=path, virtual_balance=1_000_000.0)
    )
    db.init_db()

    # The old rows are attributed to the default strategy, and a scoped query
    # finds them while a different strategy's query does not.
    assert len(db.get_open_positions(strategy_id=db.DEFAULT_STRATEGY_ID)) == 1
    assert db.get_open_positions(strategy_id="nifty-adx") == []

    with db.session() as c:
        for table in ("positions", "orders", "signals"):
            nulls = c.execute(
                f"SELECT COUNT(*) FROM {table} WHERE strategy_id IS NULL"
            ).fetchone()[0]
            assert nulls == 0, f"{table} still has unattributed rows"


def test_two_strategies_positions_are_isolated(monkeypatch, tmp_path) -> None:
    """A scoped query must never see the other book's leg.

    This is the whole point of the column: both strategies trade one contract, so
    without the filter each reads the other's position as its own.
    """
    use_test_database(monkeypatch, tmp_path)

    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-flip")
    db.open_position("NIFTY26NOVFUT", -65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="nifty-adx")

    assert [p["position_key"] for p in
            db.get_open_positions(mode=PAPER, strategy_id="gift-flip")] == [1]
    assert [p["position_key"] for p in
            db.get_open_positions(mode=PAPER, strategy_id="nifty-adx")] == [-1]
    # Unscoped is the whole portfolio, and must still see both.
    assert len(db.get_open_positions(mode=PAPER)) == 2


def test_both_books_reverse_on_the_same_flip(monkeypatch, tmp_path) -> None:
    """PAPER must trade both venues, and both must reverse on the flip.

    The two books are the whole point of PAPER mode: GIFT Nifty signals, GIFT
    Nifty fills in the index book *and* futures fills in the paper futures book,
    settled on the same bar. A reversal that happened in only one of them would
    make the comparison meaningless from the first flip onwards.
    """
    import gift_book

    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    book = gift_book.GiftBook(path=str(tmp_path / "gift.db"), enabled=True, units=65)
    monkeypatch.setattr(gift_book, "get_gift_book", lambda strategy_id=None: book)
    _pin_signal_clock(monkeypatch, datetime(2026, 11, 16, 9, 15, tzinfo=IST))
    monkeypatch.setattr(
        engine, "market_status", lambda: {"is_open": True, "reason": "Market open"}
    )
    bars = [
        {"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:30:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    verdicts = iter([
        SimpleNamespace(signal=strategy.BULL, previous_signal=strategy.NO_SIGNAL,
                        opposite=False, flat=False, fresh=True, detail="test",
                        bars=100, conditions={}, values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BEAR, previous_signal=strategy.BULL,
                        opposite=True, flat=False, fresh=True, detail="test",
                        bars=100, conditions={}, values={}, close=25_100.0),
    ])

    class FakeFeed:
        seconds = 3600

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {"exit": "flip"})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.NO_SIGNAL

    feed.n = 1
    opened = eng.evaluate_now()
    assert opened["gift_book"]["action"] == "BUY"
    assert book.net_units() == 65

    feed.n = 2
    flipped = eng.evaluate_now()

    # Both venues took the flip on the same bar, in opposite directions.
    assert flipped["action"] == "SELL"
    assert flipped["gift_book"]["action"] == "SELL"
    assert book.net_units() == -65
    assert [p["position_key"] for p in db.get_open_positions(mode=PAPER)] == [-1]


def test_overnight_reconcile_also_settles_the_gift_index_book(
    monkeypatch, tmp_path
) -> None:
    """The overnight path must settle the index book too, not only the futures.

    The two venues keep their own hours, so on the pre-open bar the index book
    takes the flip immediately while the futures leg waits for the morning's
    in-session bar. The point of the test is that the *futures* reconcile does not
    swallow the index book's settle: both ledgers end the morning on the verdict,
    and neither is left a bar behind.
    """
    import gift_book

    eng, manager, client = _overnight_env(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER, source="ENGINE")
    book = gift_book.GiftBook(path=str(tmp_path / "gift.db"), enabled=True, units=65)
    monkeypatch.setattr(gift_book, "get_gift_book", lambda strategy_id=None: book)
    book.execute("BUY", 25_000.0)

    state = {"open": False}
    monkeypatch.setattr(
        engine, "market_status",
        lambda: {"is_open": state["open"], "reason": "Pre-market"},
    )
    bars = [
        {"time": "2026-11-16T08:45:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
        {"time": "2026-11-16T09:00:00+05:30", "open": 1, "high": 1, "low": 1, "close": 1},
    ]
    # The pre-open bar merely disagrees, which is not an exit and not an entry:
    # both books hold and the futures leg is deferred to the morning's own bar.
    # That bar then opens the short, which closes the long overnight leg.
    verdicts = iter([
        SimpleNamespace(signal=strategy.NO_SIGNAL, previous_signal=strategy.BULL,
                        opposite=False,
                        flat=True,
                        fresh=False, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
        SimpleNamespace(signal=strategy.BEAR, previous_signal=strategy.NO_SIGNAL,
                        opposite=False,
                        flat=False,
                        fresh=True, detail="test", bars=100, conditions={},
                        values={}, close=25_000.0),
    ])

    class FakeFeed:
        seconds = 900

        def __init__(self):
            self.n = 0

        def refresh_history(self):
            return None

        def candles(self):
            return bars[: self.n]

    feed = FakeFeed()
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "required_bars", lambda: 1)
    monkeypatch.setattr(engine, "strategy_params", lambda: {})
    monkeypatch.setattr(engine.strategy, "evaluate", lambda c, p: next(verdicts))
    eng._last_bar_at = "seed"
    eng._last_signal = strategy.BEAR

    # Pre-open: nothing moves. The index book has no exit to take, and the
    # futures leg is deferred to the first in-session close.
    feed.n = 1
    pre_open = eng.evaluate_now()
    assert eng._overnight_pending is True
    assert pre_open["gift_book"] is None
    assert book.net_units() == 65
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == 1

    # The 09:00 bar closes in-session and confirms BEAR. The reconcile flips the
    # wrong long to the short; the index book opens the same side on the same bar,
    # on its own hours. Neither ledger is left a bar behind.
    state["open"] = True
    feed.n = 2
    result = eng.evaluate_now()

    assert result["action"] == "SELL"
    assert book.net_units() == -65
    assert db.get_open_positions(mode=PAPER)[0]["position_key"] == -1


def test_after_hours_paper_entry_books_the_future_price_not_the_gift_close(
    monkeypatch, tmp_path
) -> None:
    """The row must not open holding the index/future basis.

    Reproduces the reported symptom: a long opened from a GIFT close and marked
    at the future's LTP showed a loss equal to the basis, with no trade behind it.
    Filling and marking from one instrument is what removes it.
    """
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(order_manager, "is_market_open", lambda: False)
    monkeypatch.setattr(
        order_manager, "market_status", lambda: {"reason": "Post-market", "is_open": False}
    )
    monkeypatch.setattr(
        order_manager, "settings",
        SimpleNamespace(
            product="NRML", order_type="MARKET", tick_size=0.05, slippage_ticks=0.0,
            lot_size=65, default_lots=1, max_open_lots=4, lot_margin=150_000.0,
            virtual_balance=1_000_000.0,
        ),
    )

    # The GIFT close (25,293) and the future's LTP (25,235) differ by the basis.
    class FakeClient:
        mode = PAPER

        def get_ltp(self, symbol, max_age=None):
            return 25_235.0

        def latest_known_price(self, symbol):
            return 25_235.0

        def place_order(self, **kwargs):
            return {
                "order_id": "o1", "status": "COMPLETE",
                "filled_quantity": kwargs["quantity"], "average_price": kwargs["price"],
            }

        def available_cash(self):
            return 1_000_000.0

        def data_source(self):
            return "test"

        def auth_status(self):
            return {"authenticated": True}

    client = FakeClient()
    manager = OrderManager(client)
    monkeypatch.setattr(order_manager, "get_order_manager", lambda: manager)

    payload = {
        "action": "BUY",
        "symbol": "NIFTY26NOVFUT",
        "qty_lots": 1,
        "paper_after_hours": True,
        "price": 25_293.0,   # a GIFT close supplied by a caller
    }
    result = manager.process_signal(payload, source="ENGINE")

    assert result.ok, result.reason
    position = db.get_open_positions(mode=PAPER)[0]
    # Booked at the future's price, so entry and mark share one instrument.
    assert position["entry_price"] == 25_235.0
    assert position["entry_price"] != 25_293.0

    marks = manager.price_map()
    assert marks["NIFTY26NOVFUT"] == 25_235.0
    # Opened flat, not down by the basis.
    pnl = (marks["NIFTY26NOVFUT"] - position["entry_price"]) * position["quantity"]
    assert pnl == 0.0


def test_after_hours_fill_is_impossible_without_a_future_quote(monkeypatch, tmp_path) -> None:
    """Fail closed: no future price means no fill, not a GIFT-index fill.

    Booking at the index was the old fallback, and it is what put basis into the
    position. Refusing is the honest outcome - a paper entry at a price the
    contract never traded at is worse than no entry.
    """
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(order_manager, "is_market_open", lambda: False)
    monkeypatch.setattr(
        order_manager, "market_status", lambda: {"reason": "Post-market", "is_open": False}
    )
    monkeypatch.setattr(
        order_manager, "settings",
        SimpleNamespace(
            product="NRML", order_type="MARKET", tick_size=0.05, slippage_ticks=0.0,
            lot_size=65, default_lots=1, max_open_lots=4, lot_margin=150_000.0,
            virtual_balance=1_000_000.0,
        ),
    )

    class NoQuoteClient:
        mode = PAPER

        def get_ltp(self, symbol, max_age=None):
            return None

        def available_cash(self):
            return 1_000_000.0

        def data_source(self):
            return "CACHE"

    manager = OrderManager(NoQuoteClient())

    result = manager.process_signal(
        {
            "action": "BUY",
            "symbol": "NIFTY26NOVFUT",
            "qty_lots": 1,
            "price": 25_293.0,          # a GIFT close must not rescue this
            "paper_after_hours": True,
            "signal_id": "no-quote-after-hours",
        },
        source="ENGINE",
    )

    assert not result.ok
    assert "no reference price" in result.reason.lower()
    assert db.get_open_positions(mode=PAPER) == []


# ---------------------------------------------------------------------------
# Realistic paper fill model (fill.py + order_manager integration)
#
# PAPER mocks orders but not market data. A real market order walks the book, so
# the paper futures leg fills at the bid/ask touch (volume-weighted across
# levels) and falls back to the old adverse-tick approximation only when the
# quote has no depth. The GIFT index book is unaffected: an index has no order
# book and fills at the completed bar's close.
# ---------------------------------------------------------------------------
def test_fill_walks_the_book_from_the_touch(monkeypatch) -> None:
    """A BUY lifts the ask side from best to worse, volume-weighted."""
    depth = {
        "sell": [
            {"price": 25_000.0, "quantity": 100},
            {"price": 25_000.05, "quantity": 100},
        ],
    }
    result = fill.simulate_fill(
        "NIFTY26NOVFUT", "BUY", 130,
        ltp=25_010.0, depth=depth, tick_size=0.05, slippage_ticks=1.0,
    )
    # 100 @ 25_000.0 + 30 @ 25_000.05 -> VWAP 25_000.0115 -> 25_000.0
    assert result.filled_quantity == 130
    assert result.partial is False
    assert result.source == "book"
    assert result.levels_touched == 2
    assert result.price == 25_000.0


def test_fill_is_partial_when_the_book_is_too_thin(monkeypatch) -> None:
    """The visible book only shows 100 units; the rest is not invented."""
    depth = {"sell": [{"price": 25_000.0, "quantity": 100}]}
    result = fill.simulate_fill(
        "NIFTY26NOVFUT", "BUY", 195,
        ltp=25_010.0, depth=depth, tick_size=0.05, slippage_ticks=1.0,
    )
    assert result.filled_quantity == 100
    assert result.partial is True
    assert result.price == 25_000.0


def test_fill_uses_adverse_tick_when_no_depth(monkeypatch) -> None:
    """A quote with no depth grades exactly as the old apply_slippage model."""
    result = fill.simulate_fill(
        "NIFTY26NOVFUT", "BUY", 65,
        ltp=25_000.0, depth=None, tick_size=0.05, slippage_ticks=1.0,
    )
    assert result.source == "ltp"
    assert result.filled_quantity == 65
    assert result.price == 25_000.05

    zero = fill.simulate_fill(
        "NIFTY26NOVFUT", "SELL", 65,
        ltp=25_000.0, depth=None, tick_size=0.05, slippage_ticks=0.0,
    )
    assert zero.price == 25_000.0


def test_fill_sell_hits_the_bid_side(monkeypatch) -> None:
    """A SELL consumes the bid side, best (highest) first."""
    depth = {"buy": [{"price": 24_999.95, "quantity": 200}]}
    result = fill.simulate_fill(
        "NIFTY26NOVFUT", "SELL", 65,
        ltp=24_500.0, depth=depth, tick_size=0.05, slippage_ticks=1.0,
    )
    assert result.price == 24_999.95
    assert result.filled_quantity == 65
    assert result.partial is False


def test_paper_entry_fills_at_the_book_touch_when_depth_present(monkeypatch, tmp_path) -> None:
    """Integration: the order manager books the VWAP from the depth, not LTP±tick."""
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(order_manager, "is_market_open", lambda: True)
    monkeypatch.setattr(
        order_manager, "settings",
        SimpleNamespace(
            product="NRML", order_type="MARKET", tick_size=0.05, slippage_ticks=1.0,
            lot_size=65, default_lots=1, max_open_lots=4, lot_margin=150_000.0,
            virtual_balance=1_000_000.0,
        ),
    )

    class DepthClient:
        mode = PAPER

        def get_ltp(self, symbol, max_age=None):
            return 25_010.0

        def get_quote(self, symbol, max_age=None):
            return {"depth": {"sell": [{"price": 25_000.0, "quantity": 200}]}}

        def place_order(self, **kwargs):
            return {
                "order_id": "o1", "status": "COMPLETE",
                "filled_quantity": kwargs["quantity"], "average_price": kwargs["price"],
                "depth": self.get_quote("")["depth"],
            }

        def available_cash(self):
            return 1_000_000.0

        def data_source(self):
            return "test"

        def auth_status(self):
            return {"authenticated": True}

    manager = OrderManager(DepthClient())
    result = manager.process_signal(
        {"action": "BUY", "symbol": "NIFTY26NOVFUT", "qty_lots": 1, "signal_id": "depth-entry"},
        source="ENGINE",
    )

    assert result.ok, result.reason
    position = db.get_open_positions(mode=PAPER)[0]
    # The visible book's best ask is 25_000.0, not the LTP 25_010.0, and not
    # LTP + one tick (25_010.05).
    assert position["entry_price"] == 25_000.0
    assert result.details["fill_model"] == "book"
    assert result.details["levels_touched"] == 1


def test_paper_entry_keeps_old_behaviour_without_depth(monkeypatch, tmp_path) -> None:
    """No depth means the fill is LTP plus one adverse tick, as before."""
    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    monkeypatch.setattr(order_manager, "is_market_open", lambda: True)
    monkeypatch.setattr(
        order_manager, "settings",
        SimpleNamespace(
            product="NRML", order_type="MARKET", tick_size=0.05, slippage_ticks=1.0,
            lot_size=65, default_lots=1, max_open_lots=4, lot_margin=150_000.0,
            virtual_balance=1_000_000.0,
        ),
    )

    class NoDepthClient:
        mode = PAPER

        def get_ltp(self, symbol, max_age=None):
            return 25_000.0

        def place_order(self, **kwargs):
            return {
                "order_id": "o1", "status": "COMPLETE",
                "filled_quantity": kwargs["quantity"], "average_price": kwargs["price"],
                "depth": None,
            }

        def available_cash(self):
            return 1_000_000.0

        def data_source(self):
            return "test"

        def auth_status(self):
            return {"authenticated": True}

    manager = OrderManager(NoDepthClient())
    result = manager.process_signal(
        {"action": "BUY", "symbol": "NIFTY26NOVFUT", "qty_lots": 1, "signal_id": "no-depth-entry"},
        source="ENGINE",
    )

    assert result.ok, result.reason
    position = db.get_open_positions(mode=PAPER)[0]
    assert position["entry_price"] == 25_000.05
    assert result.details["fill_model"] == "ltp"


# ---------------------------------------------------------------------------
# Degenerate-bar guards
#
# A bar seeded from Kite's still-forming row carries only the OHLC of its first
# tick. Closed as-is it prices as a flat line, and the GIFT book filled an entry
# 84 points above the market off exactly that (LONG @ 25399.05 against a true
# close of 25315.5). These pin every layer that must refuse such a bar.
# ---------------------------------------------------------------------------
def test_is_degenerate_rejects_a_flat_bar() -> None:
    """A bar with no range is a placeholder, not a traded price."""
    import fill

    assert fill.is_degenerate(25_399.0, 25_399.0, 25_399.0, 25_399.0) is True
    assert fill.is_degenerate(25_399.0, 25_399.0, 25_296.0, 25_315.5) is False
    assert fill.is_degenerate(0.0, 0.0, 0.0, 0.0) is True
    assert fill.is_degenerate(None, 1.0, 2.0, 3.0) is True


def test_a_still_forming_kite_row_is_not_treated_as_closed() -> None:
    """Kite stamps bars at an offset, so a floored start can still be forming.

    The 16:01->16:06 bucket is stamped 16:01 and floors to 16:00, which matches
    ``bucket_start(now)`` at 16:05 and used to be emitted as a closed bar holding
    only its first tick's OHLC.
    """
    import gift_nifty

    stamp = datetime(2026, 11, 12, 16, 1, tzinfo=gift_nifty.IST)
    now = datetime(2026, 11, 12, 16, 5, tzinfo=gift_nifty.IST)
    assert stamp + timedelta(seconds=300) > now

    closed_now = now + timedelta(minutes=2)
    assert stamp + timedelta(seconds=300) <= closed_now


def test_merge_forming_keeps_the_true_range_and_tick_count() -> None:
    """Merging must not throw away the high/low the tick feed already saw."""
    import gift_nifty

    start = datetime(2026, 11, 12, 16, 0, tzinfo=gift_nifty.IST)
    live = gift_nifty.Candle(
        start=start, open=25_310.0, high=25_320.0, low=25_300.0, close=25_315.0, ticks=140
    )
    snapshot = gift_nifty.Candle(
        start=start, open=25_310.0, high=25_399.0, low=25_296.0, close=25_315.5
    )

    merged = gift_nifty._merge_forming(live, snapshot)

    assert merged.high == 25_399.0
    assert merged.low == 25_296.0
    assert merged.close == 25_315.5
    assert merged.ticks == 140, "the tick count must survive so the bar still counts"


def test_merge_forming_prefers_the_snapshot_for_a_bucket_it_never_saw() -> None:
    """A bucket the feed joined mid-way is replaced, not merged across starts."""
    import gift_nifty

    start = datetime(2026, 11, 12, 16, 0, tzinfo=gift_nifty.IST)
    other = datetime(2026, 11, 12, 15, 55, tzinfo=gift_nifty.IST)
    live = gift_nifty.Candle(
        start=other, open=1.0, high=2.0, low=0.5, close=1.5, ticks=9
    )
    snapshot = gift_nifty.Candle(
        start=start, open=25_310.0, high=25_320.0, low=25_300.0, close=25_315.0
    )

    merged = gift_nifty._merge_forming(live, snapshot)

    assert merged is snapshot


def test_a_tick_starved_bar_is_marked_partial_when_stored() -> None:
    """A bar that closed with no tick of its own must not enter the series."""
    import gift_nifty

    builder = gift_nifty.CandleBuilder(seconds=300)
    flat = gift_nifty.Candle(
        start=datetime(2026, 11, 12, 16, 0, tzinfo=gift_nifty.IST),
        open=25_399.0, high=25_399.0, low=25_399.0, close=25_399.0, ticks=0,
    )

    builder._store(flat)

    assert builder.closed[-1].partial == gift_nifty.PARTIAL_GAP


def test_a_close_exactly_on_the_psar_votes_sell_rather_than_abstaining() -> None:
    """A tie on the SAR must not silently abstain from the vote.

    ``buy_psar`` was ``close > psar`` and ``sell_psar`` was ``close < psar``, so a
    bar closing exactly *on* the SAR satisfied neither and the PSAR rule abstained
    while SuperTrend was confirming - 1 of 2 -> NEUTRAL. PSAR's two-bar clamp can
    pin the SAR precisely to the close, so this is reachable on real data: a GIFT
    bar closed at 25279.00 with the SAR at 25279.00 and the bot stood aside on an
    otherwise confirmed short.
    """
    import strategy

    psar_level = 25_279.0
    # The old pair abstained; the new pair partitions.
    assert not (psar_level > psar_level)
    assert not (psar_level < psar_level)
    assert psar_level <= psar_level, "the sell side must claim the tie"

    # Exactly one side holds for every close, so the rule can never abstain.
    for close in (25_200.0, 25_278.99, 25_279.0, 25_279.01, 25_350.0):
        buy_ok = close > psar_level
        sell_ok = close <= psar_level
        assert buy_ok != sell_ok, f"close={close} voted both or neither"


def test_evaluate_reads_a_psar_tie_as_a_sell_confirmation(monkeypatch) -> None:
    """A bearish SuperTrend plus a close on the SAR is a confirmed BEAR."""
    import strategy

    # Pin the SAR onto the close and hold SuperTrend bearish, so the only thing
    # under test is whether the tie counts as the second sell confirmation.
    monkeypatch.setattr(strategy, "psar", lambda *a, **k: [None] * 15 + [25_279.0])
    monkeypatch.setattr(
        strategy, "supertrend", lambda *a, **k: ([None] * 15 + [25_300.0], [0] * 15 + [1])
    )

    candles = [
        {"high": 25_300.0, "low": 25_250.0, "close": 25_279.0} for _ in range(16)
    ]
    verdict = strategy.evaluate(candles)

    assert verdict.conditions["sell_psar"] is True
    assert verdict.conditions["buy_psar"] is False
    assert verdict.values["sell_confirmations"] == 2
    assert verdict.signal == strategy.BEAR


# ---------------------------------------------------------------------------
# Zerodha NSE Nifty-futures cost model
#
# Pinned against a hand-worked contract note so a silent rate change cannot
# flatter P&L. The previous model under-charged STT by 4x (0.0125% vs the
# statutory 0.05% on sell), understating a round trip by ~620 on one lot.
# ---------------------------------------------------------------------------
def test_charge_breakdown_matches_the_contract_note() -> None:
    """One lot at 25,292.30 round-tripped at the same price."""
    import order_manager as om

    price, qty = 25_292.30, 65
    value = price * qty  # 1,643,999.50

    charges = om.charge_breakdown(value, value)

    assert charges["brokerage"] == 40.00     # two capped orders
    assert charges["stt"] == 822.00          # round(0.05% x sell value)
    assert charges["stamp_duty"] == 33.00    # round(0.002% x buy value)
    assert charges["exchange"] == 60.17      # 0.00183% x total turnover
    assert charges["sebi"] == 3.29           # 0.0001% x total turnover
    assert charges["gst"] == 18.62           # 18% x (brokerage + exchange + sebi)
    assert charges["total"] == 977.08
    assert om.breakeven_points(qty, value, value) == 15.03


def test_charges_are_asymmetric_because_stt_is_sell_side_only() -> None:
    """A long round trip pays STT on its exit, a short on its entry."""
    import order_manager as om

    qty = 65
    # Same 100-point move, opposite directions: different legs carry the STT.
    long_charges = om.charge_breakdown(25_000.0 * qty, 25_100.0 * qty)
    short_charges = om.charge_breakdown(25_100.0 * qty, 25_000.0 * qty)

    assert long_charges["stt"] == round(0.0005 * 25_100.0 * qty)
    assert short_charges["stt"] == round(0.0005 * 25_000.0 * qty)
    assert long_charges["stt"] > short_charges["stt"], (
        "the sell leg is the exit on a long, so a higher exit pays more STT"
    )
    assert long_charges["stamp_duty"] < short_charges["stamp_duty"]


def test_stt_and_stamp_are_rounded_to_whole_rupees() -> None:
    """The levies are rupee-rounded individually, not only in the total."""
    import order_manager as om

    charges = om.charge_breakdown(111_111.0, 222_222.0)

    assert charges["stt"] == float(round(0.0005 * 222_222.0))
    assert charges["stamp_duty"] == float(round(0.00002 * 111_111.0))
    assert charges["stt"] == int(charges["stt"])
    assert charges["stamp_duty"] == int(charges["stamp_duty"])


def test_breakeven_is_the_charge_stack_per_unit() -> None:
    """Breakeven points must be recoverable from the total."""
    import order_manager as om

    qty = 65
    buy, sell = 25_000.0 * qty, 25_000.0 * qty
    total = om.estimate_charges(buy, sell)

    assert om.breakeven_points(qty, buy, sell) == round(total / qty, 2)
    assert om.breakeven_points(0, buy, sell) == 0.0


def test_estimate_charges_equals_the_breakdown_total() -> None:
    """The scalar helper and the itemised one must never disagree."""
    import order_manager as om

    for buy, sell in ((1_000.0, 1_000.0), (1_643_999.5, 1_650_000.0), (0.0, 0.0)):
        assert om.estimate_charges(buy, sell) == om.charge_breakdown(buy, sell)["total"]


def test_status_does_not_fetch_history_on_the_poll_path(monkeypatch) -> None:
    """The dashboard's poll endpoint must not make a blocking Kite call.

    ``status()`` is polled every 1.5 s. ``refresh_history`` is a synchronous
    network round-trip whenever its 30 s throttle expires, so calling it here
    stalled one poll in twenty - which reads as the whole dashboard lagging.
    The engine and mark loops refresh the series on their own cadence.
    """
    import engine as engine_mod

    calls = []

    class FakeFeed:
        def refresh_history(self, force: bool = False):
            calls.append(force)
            return 0

        def candle_snapshot(self):
            return [], False

        def status(self):
            return {}

    monkeypatch.setattr(engine_mod, "get_feed", lambda: FakeFeed())
    monkeypatch.setattr(engine_mod, "market_status", lambda: {"is_open": False})
    monkeypatch.setattr(engine_mod, "get_execution_mode", lambda: "PAPER")

    eng = engine_mod.StrategyEngine()
    eng.status()

    assert calls == [], "status() must not refresh history on the poll path"


def test_mark_loop_marks_without_placing_orders(monkeypatch) -> None:
    """The mark loop values open positions; it never trades.

    It used to also test a stop on the tick that wrote the mark. There is no
    stop any more - the exit is the signal, evaluated on a closed bar - so the
    mark loop's only job is keeping unrealised PnL and the equity curve live.
    """
    import threading
    import bot as bot_mod

    order = []

    class FakeEngine:
        def enforce_risk(self):
            order.append("risk")
            return None

    class FakeFeed:
        def refresh_exec_quote(self):
            order.append("quote")

        def status(self):
            return {"price": 25_000.0}

    class FakeManager:
        def price_map(self):
            order.append("mark")
            return {"NIFTY26NOVFUT": 25_000.0}

    monkeypatch.setattr(bot_mod, "get_feed", lambda: FakeFeed())
    monkeypatch.setattr(bot_mod, "get_order_manager", lambda: FakeManager())
    monkeypatch.setattr(bot_mod, "get_engine", lambda: FakeEngine())
    monkeypatch.setattr(bot_mod, "get_execution_mode", lambda: "PAPER")
    monkeypatch.setattr(bot_mod.db, "mark_open_positions", lambda *a, **k: None)
    monkeypatch.setattr(bot_mod.db, "get_open_positions", lambda **k: [])

    stop = threading.Event()

    def run():
        stop.wait(0.05)
        stop.set()

    threading.Thread(target=run, daemon=True).start()
    bot_mod.mark_loop(stop, interval=0.01)

    assert "mark" in order, "the mark must still be written"
    assert "risk" not in order, "the mark loop must not run a stop check"


def test_net_position_force_reads_the_broker_while_in_paper(monkeypatch) -> None:
    """The LIVE readiness gate must be able to read the broker before the switch.

    ``net_position`` short-circuited to ``None`` whenever the client was in
    PAPER. The readiness report deliberately runs *before* the mode switch - a
    gate that only works once you are already live is not a gate - so it always
    saw ``None`` and always blocked arming LIVE. ``force`` is the escape hatch,
    and it must not leak into the trading path.
    """
    import kite_client as kc

    client = kc.KiteClient.__new__(kc.KiteClient)
    client.mode = "PAPER"
    client._kite = object()
    client._auth_error = None
    client._last_prices = {}

    calls = []

    class FakeKite:
        def positions(self, force=False):
            calls.append(1)
            return {"net": [{"tradingsymbol": "NIFTY26NOVFUT", "quantity": 65}]}

    client._kite = FakeKite()

    # Without force, PAPER must not touch the broker's book at all.
    assert client.net_position("NIFTY26NOVFUT") is None
    assert client.positions() == []
    assert calls == [], "PAPER must not read broker positions without force"

    # With force, the reconciliation path sees the real figure.
    assert client.net_position("NIFTY26NOVFUT", force=True) == 65
    assert calls == [1]


def test_live_readiness_passes_position_sync_while_in_paper(monkeypatch, tmp_path) -> None:
    """The gate must be able to report ready while the bot is still in PAPER.

    This is the regression: ``position_sync`` was permanently false because the
    client refused to read the broker in PAPER, so LIVE could never be armed
    through the readiness gate.
    """
    import order_manager as om
    import database as db

    manager, client = _rollover_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(om, "get_execution_mode", lambda: "PAPER")
    monkeypatch.setattr(db, "get_open_positions", lambda **k: [])

    client.net_position = lambda symbol, force=False: 0 if force else None
    client.positions = lambda force=False: [] if not force else []
    client.front_month_symbol = lambda: "NIFTY26NOVFUT"
    client.nifty_future_instruments = lambda: [
        {"tradingsymbol": "NIFTY26NOVFUT", "instrument_type": "FUT"}
    ]

    state = manager._live_sync_state(mode="LIVE")
    assert state is not None, "the report must run even while in PAPER"
    assert state["in_sync"] is True
    assert state["error"] is None
    assert manager._live_position_sync_error(mode="LIVE") is None


def test_each_alert_channel_gets_a_payload_it_accepts(monkeypatch) -> None:
    """Every channel must receive the shape it actually parses.

    The old code sent one generic JSON body to all of them. Slack wants "text",
    Discord "content", ntfy a plain body - so all three rejected it with a 400
    while the operator believed they were being alerted.
    """
    import dataclasses
    import notifier
    from config import settings

    sent: list[tuple[str, str, object]] = []
    monkeypatch.setattr(notifier, "_post_json",
                        lambda url, body, timeout=8.0: sent.append(("json", url, body)) or True)
    monkeypatch.setattr(notifier, "_post_text",
                        lambda url, text, timeout=8.0: sent.append(("text", url, text)) or True)

    payload = {"kind": "STOP_OUT", "detail": "hit the stop", "severity": "warning"}

    cases = {
        "telegram": ("json", "chat_id"),
        "slack": ("json", "text"),
        "discord": ("json", "content"),
        "ntfy": ("text", None),
        "generic": ("json", "kind"),
    }
    for channel, (kind, field) in cases.items():
        sent.clear()
        monkeypatch.setattr(notifier, "settings", dataclasses.replace(
            settings,
            alert_enabled=True,
            alert_channel=channel,
            alert_webhook_url="https://example.test/hook",
            alert_telegram_bot_token="TOK",
            alert_telegram_chat_id="42",
        ))
        assert notifier.send(payload) is True
        assert len(sent) == 1, f"{channel} sent {len(sent)} requests"
        got_kind, url, body = sent[0]
        assert got_kind == kind, f"{channel} used {got_kind}, expected {kind}"
        if field:
            assert field in body, f"{channel} payload has no {field!r}: {body}"
        if channel == "telegram":
            assert "TOK" in url and url.endswith("/sendMessage")


def test_alerts_are_silent_unless_switched_on(monkeypatch, tmp_path) -> None:
    """A configured channel is not permission to send.

    Credentials in .env used to be enough to make the bot message the operator on
    every fill and every flip, which turns a phone into a log stream. The channel
    says *where* a message would go; ``ALERT_ENABLED`` says whether one goes at
    all, and it is off by default.

    Run against a private database. ``enabled()`` also consults a stored
    dashboard override, so testing the .env default against the workspace
    database made this pass or fail on whatever the last manual toggle left
    behind - the settings row is real state, not a fixture.
    """
    use_test_database(monkeypatch, tmp_path)
    db.delete_settings(["alert_enabled"])
    import dataclasses
    import notifier
    from config import settings

    sent = []
    monkeypatch.setattr(notifier, "_post_json",
                        lambda url, body, timeout=8.0: sent.append(body) or True)

    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=False,
        alert_channel="telegram",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="42",
    ))

    assert notifier.configured() is False
    assert notifier.send({"kind": "TRADE_OPENED", "detail": "BUY 65"}) is False
    assert sent == [], "nothing may leave the bot while alerting is off"

    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        notifier.settings, alert_enabled=True,
    ))
    assert notifier.configured() is True
    assert notifier.send({"kind": "TRADE_OPENED", "detail": "BUY 65"}) is True
    assert len(sent) == 1


def test_alert_status_reports_the_master_switch(monkeypatch, tmp_path) -> None:
    """The dashboard must be able to tell "off" from "misconfigured"."""
    use_test_database(monkeypatch, tmp_path)
    db.delete_settings(["alert_enabled"])
    import dataclasses
    import notifier
    from config import settings

    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=False,
        alert_channel="telegram",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="321056238",
    ))

    state = notifier.status()
    assert state["enabled"] is False
    assert state["configured"] is False
    assert state["token_set"] is True, "the token is still set, just not in use"


def test_a_bot_username_is_not_a_telegram_chat_id(monkeypatch) -> None:
    """A non-empty chat id is not a valid one, and treating it as valid is what
    made a permanently failing channel look armed.

    ALERT_TELEGRAM_CHAT_ID was set to the bot's own username, which Telegram can
    never resolve - a bot cannot message itself. Both fields were non-empty, so
    ``configured()`` answered yes, the dashboard showed "Armed", and every alert
    failed with ``400 chat not found``. Over a session that is one failed HTTP
    round-trip and one warning line every 30s, with no alert ever arriving.
    """
    import dataclasses
    import notifier
    from config import settings

    assert notifier.telegram_chat_id_valid("321056238") is True
    assert notifier.telegram_chat_id_valid("-1001234567890") is True, "group ids are negative"
    assert notifier.telegram_chat_id_valid("@belaku_alerts") is True, "public channel"
    for bad in ("belaku_bot", "42x", "@", "", None, "-"):
        assert notifier.telegram_chat_id_valid(bad) is False, bad

    sent: list = []
    monkeypatch.setattr(notifier, "_post_json",
                        lambda url, body, timeout=8.0: sent.append(body) or True)
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=True,
        alert_channel="telegram",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="belaku_bot",
    ))

    assert notifier.channel_ready() is False
    assert notifier.configured() is False
    assert notifier.status()["chat_id_valid"] is False
    assert notifier.send({"kind": "TEST", "detail": "x"}) is False
    assert sent == [], "an unusable target must not be called at all"


def test_alert_delivery_failure_is_reported_not_raised(monkeypatch) -> None:
    """A dead channel must not take the watchdog down with it.

    dispatch() runs on the watchdog thread; an exception here would kill the loop
    and silently stop all future alerting - the worst failure for a watchdog.
    """
    import dataclasses
    import urllib.error
    import notifier
    from config import settings

    def boom(url, body, timeout=8.0):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(notifier, "_post_json", boom)
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings, alert_channel="slack", alert_webhook_url="https://example.test/hook",
    ))

    assert notifier.send({"kind": "TEST", "detail": "x"}) is False


def test_watchdog_alerts_on_a_rejected_order(monkeypatch) -> None:
    """A rejected order means the bot decided to trade and could not.

    Nothing else surfaces this: the feed is fine, the strategy looks healthy, and
    the book simply stays flat through a signal.
    """
    import watchdog

    wd = watchdog.Watchdog(webhook_url="", cooldown_seconds=0)
    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1},
        engine={"last_result": {"ok": False, "action": "BUY", "reason": "margin short"}},
    )
    kinds = [a.kind for a in alerts]
    assert "ORDER_REJECTED" in kinds
    assert any("margin short" in a.detail for a in alerts if a.kind == "ORDER_REJECTED")


def test_watchdog_alerts_on_an_expired_kite_token(monkeypatch) -> None:
    """Without a token the bot runs on cached candles and cannot trade."""
    import watchdog

    wd = watchdog.Watchdog(webhook_url="", cooldown_seconds=0)
    alerts = wd.check(
        market_open=False,
        feed={"connected": True},
        engine={},
        kite={"authenticated": False, "detail": "token expired"},
    )
    assert [a.kind for a in alerts] == ["TOKEN_EXPIRED"]


def test_a_confirmed_entry_sends_a_trade_alert(monkeypatch, tmp_path) -> None:
    """A working bot must be distinguishable from a dead one.

    Without a trade notification the only messages are problems, so a session
    that is quietly taking signals looks identical to one that has stopped - and
    the first thing anyone asks after a signal is "did it take it?".
    """
    import engine as engine_mod
    import notifier

    sent: list[dict] = []
    monkeypatch.setattr(notifier, "configured", lambda: True)
    monkeypatch.setattr(notifier, "send", lambda payload, timeout=8.0: sent.append(payload) or True)

    engine_mod._trade_alert(
        "TRADE_OPENED", "BUY NIFTY26NOVFUT 65 @ 25388.50", severity="info",
        symbol="NIFTY26NOVFUT",
    )

    assert len(sent) == 1
    assert sent[0]["kind"] == "TRADE_OPENED"
    assert sent[0]["symbol"] == "NIFTY26NOVFUT"
    assert "25388.50" in sent[0]["detail"]


def test_trade_alert_never_raises_when_the_channel_is_broken(monkeypatch) -> None:
    """A notification must never be able to fail an order that already succeeded."""
    import engine as engine_mod
    import notifier

    monkeypatch.setattr(notifier, "configured", lambda: True)

    def boom(payload, timeout=8.0):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(notifier, "send", boom)

    # Must not raise.
    engine_mod._trade_alert("TRADE_OPENED", "BUY 65", symbol="NIFTY26NOVFUT")


def test_an_unconfirmed_exit_does_not_also_page_as_an_engine_error(monkeypatch) -> None:
    """One event must not page twice under two different kinds.

    The unconfirmed-exit text also lands in last_error, so without an exclusion
    the operator gets a warning beside the critical and reads it as two problems.
    """
    import watchdog

    wd = watchdog.Watchdog(webhook_url="", cooldown_seconds=0)
    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1},
        engine={"last_error": "stop-out unconfirmed: square-off raised"},
    )
    assert [a.kind for a in alerts] == ["EXIT_UNCONFIRMED"]


def test_a_position_closed_by_another_check_is_not_reported_as_a_failure(monkeypatch, tmp_path) -> None:
    """The engine loop and the mark loop both call enforce_risk.

    Both can reach the same position in the same tick. The loser of that race
    finds the row already closed - which is the outcome it wanted. Reporting it
    as a failure made the engine log "stop-out NOT confirmed" and retry a
    position that was already flat.
    """
    import order_manager as om
    import database as db

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(om, "get_execution_mode", lambda: "PAPER")

    pid = db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode="PAPER", source="TEST")
    # Simulate the other check having already closed it.
    db.close_position(pid, exit_price=24_950.0, fees=0.0, exit_reason="TRAILING_STOP")

    # Closing again must be recognised as "already done", not raised.
    try:
        db.close_position(pid, exit_price=24_950.0, fees=0.0, exit_reason="TRAILING_STOP")
    except ValueError as exc:
        assert "already closed" in str(exc)
    else:
        raise AssertionError("expected the second close to report already-closed")


def test_the_gift_index_book_alerts_on_its_own_open_and_close(monkeypatch) -> None:
    """The index book is a separate ledger, so its fills are their own events.

    Without this the only notifications were for the futures book, and an index
    exit went unseen - which is exactly what happened when the GIFT book stopped
    out at 10:03 and no alert arrived.
    """
    import gift_book
    import notifier

    sent: list[dict] = []
    monkeypatch.setattr(notifier, "configured", lambda: True)
    monkeypatch.setattr(notifier, "send", lambda payload, timeout=8.0: sent.append(payload) or True)

    gift_book._book_alert("GIFT_TRADE_OPENED", "BUY 65 units @ 25374.55", severity="info")
    gift_book._book_alert("GIFT_TRADE_CLOSED", "closed 1 position at 25403.45", severity="warning")

    assert [p["kind"] for p in sent] == ["GIFT_TRADE_OPENED", "GIFT_TRADE_CLOSED"]
    assert all(p["symbol"] == "NSEIX:GIFT NIFTY" for p in sent)


def test_a_gift_book_alert_never_raises_when_the_channel_is_broken(monkeypatch) -> None:
    """A notification must never be able to break the index book."""
    import gift_book
    import notifier

    monkeypatch.setattr(notifier, "configured", lambda: True)

    def boom(payload, timeout=8.0):
        raise RuntimeError("telegram is down")

    monkeypatch.setattr(notifier, "send", boom)
    gift_book._book_alert("GIFT_TRADE_CLOSED", "closed 1 position")


def test_a_token_written_by_the_login_flow_beats_the_deployment_spec(monkeypatch, tmp_path) -> None:
    """A browser login must survive a restart, not revert to the spec value.

    ``load_dotenv`` never overrides an existing variable, so on Lightsail the
    stale ``KITE_ACCESS_TOKEN`` from the deployment spec won every restart and
    the fresh token was silently thrown away. Only the token is re-read from
    .env - the rest of the file must not override the deployment.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "KITE_ACCESS_TOKEN=FRESH-from-browser\nWEBHOOK_HOST=127.0.0.1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "STALE-from-spec")
    monkeypatch.setenv("WEBHOOK_HOST", "0.0.0.0")

    config._prefer_env_file_token()

    assert os.environ["KITE_ACCESS_TOKEN"] == "FRESH-from-browser"
    assert os.environ["WEBHOOK_HOST"] == "0.0.0.0"


def test_an_empty_token_in_the_env_file_does_not_erase_the_spec_value(monkeypatch, tmp_path) -> None:
    """The shipped .env.example has a blank token; it must not blank the spec."""
    env_file = tmp_path / ".env"
    env_file.write_text("KITE_ACCESS_TOKEN=\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env_file)
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "from-spec")

    config._prefer_env_file_token()

    assert os.environ["KITE_ACCESS_TOKEN"] == "from-spec"


def test_writing_a_token_does_not_seed_the_whole_example_file(monkeypatch, tmp_path) -> None:
    """Seeding .env.example would activate non-default settings on restart.

    The template carries ``ALERT_CHANNEL=telegram`` and ``WEBHOOK_HOST=127.0.0.1``.
    Copying it wholesale into .env meant a single browser login quietly changed
    the deployment's alerting and bind address on the next start.
    """
    env_file = tmp_path / ".env"
    monkeypatch.setattr(kite_auth, "ENV_PATH", env_file)

    assert kite_auth.write_token("FRESH-from-browser") is True

    written = env_file.read_text(encoding="utf-8")
    assert written.strip() == "KITE_ACCESS_TOKEN=FRESH-from-browser"
    assert "ALERT_CHANNEL" not in written
    assert "WEBHOOK_HOST" not in written


def test_writing_a_token_preserves_the_other_entries_in_an_existing_env_file(
    monkeypatch, tmp_path
) -> None:
    """A real .env must keep its settings; only the token line is rewritten."""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EXECUTION_MODE=LIVE\nKITE_ACCESS_TOKEN=old\nDASHBOARD_PASSWORD=hunter2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(kite_auth, "ENV_PATH", env_file)

    assert kite_auth.write_token("new-token") is True

    written = env_file.read_text(encoding="utf-8")
    assert "KITE_ACCESS_TOKEN=new-token" in written
    assert "KITE_ACCESS_TOKEN=old" not in written
    assert "EXECUTION_MODE=LIVE" in written
    assert "DASHBOARD_PASSWORD=hunter2" in written


def test_each_rule_check_row_names_its_own_indicator() -> None:
    """A single "2 of 2 confirmed" count hides which indicator voted which way.

    The band used to show one row per *side* (BUY 0 of 2, SELL 2 of 2). That
    states the total but not the split, so a bar where SuperTrend is bearish and
    PSAR is bullish reads exactly like a bar where both agree - and the split is
    the case that decides whether the next bar flips the book. Each row must name
    its indicator and carry that indicator's own direction.
    """
    from pathlib import Path

    js = (
        Path(__file__).resolve().parent.parent / "bot" / "web" / "app.js"
    ).read_text(encoding="utf-8")

    # The two rules the strategy actually votes on.
    assert "ruleRow('SuperTrend'" in js
    assert "ruleRow('PSAR'" in js

    # Direction comes from each indicator's own condition flag, not the verdict.
    assert "conditions.buy_supertrend" in js
    assert "conditions.buy_psar" in js

    # A split bar must say so rather than render a dash that reads as no data.
    assert "'split'" in js


def test_the_band_colours_a_long_book_green_and_a_short_book_red() -> None:
    """Direction has to survive a glance, not a read.

    The band's accent rail carried the direction but the signal itself was plain
    text, so two books at a glance looked identical. The signal chip and the band
    background must both be driven by the verdict.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    css = (root / "styles.css").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")

    assert ".book-band.is-bull" in css
    assert ".book-band.is-bear" in css
    assert ".book-band-signal-value.pos" in css
    assert ".book-band-signal-value.neg" in css

    # The renderer must actually apply them.
    assert "book-band ${tone}" in js
    assert "book-band-signal-value ${signalClass}" in js


def test_the_dashboard_has_no_aggregate_portfolio_panel() -> None:
    """Each band carries its own account, so the aggregate panel was a duplicate.

    It repeated equity and P&L at the bottom of the page while each band already
    showed its own. The Performance tab carries the venue-scoped totals, so
    nothing was lost by removing it - but the renderer must not keep writing to
    ids that no longer exist.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")

    assert 'id="account-panel"' not in html
    assert "m-equity" not in html
    assert "m-equity" not in js
    # The Performance tab still needs the payload, so the cache must survive.
    assert "state.portfolio = portfolio" in js


def test_the_backtest_can_measure_the_live_entry_filters() -> None:
    """The filters were configurable but unmeasurable.

    ``signals()`` passed ``{}`` as params, so ``--filter-adx`` could not exist and
    the numbers once quoted for ADX in .env.example came from somewhere other than
    this script. A filter that cannot be backtested is a filter nobody can check.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "scripts" / "backtest.py"
    ).read_text(encoding="utf-8")

    assert "--filter-adx" in src
    assert "--filter-ma" in src
    # The params must reach evaluate(), not just the CLI.
    assert "strategy.evaluate(candles[: i + 1], params)" in src
    # And the warm-up must follow the MA window, or the gate is tested on a
    # window it does not have.
    assert "strategy.required_bars(params)" in src


def test_the_env_example_does_not_claim_adx_improves_the_strategy() -> None:
    """The shipped comment contradicted the measured result.

    It claimed ADX>15 lifted net from Rs160,318 to Rs269,612. Re-measured with
    the filter actually wired into the backtest, ADX>15 gives Rs92,221 - worse,
    and every higher threshold loses money. A comment that recommends a filter
    the data rejects is worse than no comment, because it is what an operator
    reads before turning it on.
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent / "bot" / ".env.example"
    ).read_text(encoding="utf-8")

    assert "269,612" not in text
    assert "49.4%" not in text
    # The measured numbers must be present instead.
    assert "92,221" in text
    assert "monotonic" in text.lower()


def test_each_run_books_its_orders_under_its_own_strategy_id():
    """A second strategy must not book into the first one's ledger.

    The engine read `settings.strategy_id` (the single-book default, "Surikbs")
    everywhere it wrote an order, so with two strategies configured every order
    from both landed in one ledger. The visible symptom was subtle: both books
    showed zero trades forever, because `_strategy_accounts` looks up each
    strategy's own id and found nothing. The dangerous symptom was that the
    no-opposing-legs rule could never fire, because the aggregator saw one book
    holding both legs instead of two books holding one each.
    """
    import inspect

    import engine

    # Every order-writing path must accept the run, so the book is knowable.
    for name in ("execute", "exit_on_signal", "_reconcile_overnight_position"):
        sig = inspect.signature(getattr(engine.StrategyEngine, name))
        assert "run" in name or "run" in inspect.signature(
            getattr(engine.StrategyEngine, name)
        ).parameters, f"{name} cannot name the book it acts for"

    # And the payload must carry the run's id, not the engine's default.
    src = inspect.getsource(engine.StrategyEngine.execute)
    assert '"strategy": book' in src
    assert "strategy_id=book" in src
    assert '"strategy": self.strategy_id' not in src


def test_the_exit_only_closes_the_position_of_its_own_book():
    """An exit must not square off the other strategy's leg.

    Two books trade the same contract, so an unscoped lookup finds the other
    book's position and closes it - the exact double exposure the order manager
    exists to prevent, arrived at from the exit side.
    """
    import inspect

    import engine

    src = inspect.getsource(engine.StrategyEngine.exit_on_signal)
    assert "strategy_id=book" in src
    # The unscoped form must be gone.
    assert "db.get_open_positions(mode=mode)" not in src


def test_a_stalled_mark_loop_alerts() -> None:
    """A dead mark loop is invisible from outside.

    The API still answers, the feed still ticks, and the dashboard shows a frozen
    P&L that reads exactly like a quiet market. Nothing else in the bot notices:
    the loop is the only thing that marks positions to market and samples equity.
    """
    from watchdog import Watchdog

    wd = Watchdog(webhook_url="", sender=lambda url, payload: True)
    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1.0},
        engine={},
        heartbeat={"age_seconds": 120.0, "interval_seconds": 5.0},
    )

    kinds = [a.kind for a in alerts]
    assert "LOOP_STALLED" in kinds
    stalled = next(a for a in alerts if a.kind == "LOOP_STALLED")
    assert stalled.severity == "critical"


def test_a_healthy_loop_does_not_alert() -> None:
    """The threshold has to be generous enough not to page on a slow tick."""
    from watchdog import Watchdog

    wd = Watchdog(webhook_url="", sender=lambda url, payload: True)
    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1.0},
        engine={},
        heartbeat={"age_seconds": 6.0, "interval_seconds": 5.0},
    )

    assert [a.kind for a in alerts] == []


def test_a_stalled_loop_out_of_hours_does_not_alert() -> None:
    """Out of hours the loop still runs but nothing moves, so a gap is not news."""
    from watchdog import Watchdog

    wd = Watchdog(webhook_url="", sender=lambda url, payload: True)
    alerts = wd.check(
        market_open=False,
        feed={"connected": True, "tick_age_seconds": 1.0},
        engine={},
        heartbeat={"age_seconds": 600.0, "interval_seconds": 5.0},
    )

    assert "LOOP_STALLED" not in [a.kind for a in alerts]


def test_an_authenticated_but_unreachable_kite_alerts() -> None:
    """A valid token is not a working connection.

    With a valid session but no SDK, or with quotes that have stopped arriving,
    every other check passes while the bot cannot price a single order.
    """
    from watchdog import Watchdog

    wd = Watchdog(webhook_url="", sender=lambda url, payload: True)

    no_sdk = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1.0},
        engine={},
        kite={"authenticated": True, "sdk_available": False},
        heartbeat={"age_seconds": 1.0, "interval_seconds": 5.0},
    )
    assert "KITE_SDK_MISSING" in [a.kind for a in no_sdk]

    stale_quote = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 1.0},
        engine={},
        kite={"authenticated": True, "sdk_available": True, "quote_age_seconds": 400.0},
        heartbeat={"age_seconds": 1.0, "interval_seconds": 5.0},
    )
    assert "KITE_QUOTE_STALE" in [a.kind for a in stale_quote]


def test_the_status_payload_reports_the_quote_age() -> None:
    """The watchdog compares an age, so the payload has to carry one.

    A timestamp would put the clock comparison in two places and make the
    threshold depend on the reader's timezone.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "bot" / "bot.py"
    ).read_text(encoding="utf-8")

    assert '"quote_age_seconds"' in src


def test_live_readiness_checks_the_strategy_not_just_the_process(monkeypatch) -> None:
    """Arming LIVE with a warming-up strategy would place no orders.

    The rules are identical in both modes - strategy.py and risk.py never read
    the mode - so the thing that can differ is whether the books are actually
    evaluating. A LIVE arm on a strategy that is still warming up looks exactly
    like a quiet market: no orders, no errors, nothing to notice.
    """
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": 0, "ledger": 0,
              "in_sync": True, "error": None},
    )
    monkeypatch.setattr(
        bot_app, "get_engine",
        lambda: SimpleNamespace(status=lambda: {"strategies": [
            {"id": "gift-flip", "state": {"ready": False, "last_error": None}},
        ]}),
    )

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert "strategy_ready" in report["blocking"]


def test_live_readiness_passes_when_a_book_is_evaluating(monkeypatch) -> None:
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": 0, "ledger": 0,
              "in_sync": True, "error": None},
    )
    monkeypatch.setattr(
        bot_app, "get_engine",
        lambda: SimpleNamespace(status=lambda: {"strategies": [
            {"id": "gift-flip", "state": {"ready": True, "last_error": None}},
        ]}),
    )

    report = bot_app._live_readiness()

    assert report["ready"] is True
    assert any(c["key"] == "strategy_ready" and c["ok"] for c in report["checks"])


def test_live_readiness_blocks_on_a_strategy_error(monkeypatch) -> None:
    """An erroring book is a blocking condition, not a warning.

    It is the state where the bot believes it is trading and is not.
    """
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": 0, "ledger": 0,
              "in_sync": True, "error": None},
    )
    monkeypatch.setattr(
        bot_app, "get_engine",
        lambda: SimpleNamespace(status=lambda: {"strategies": [
            {"id": "gift-flip", "state": {"ready": True, "last_error": "boom"}},
        ]}),
    )

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert "strategy_errors" in report["blocking"]


def test_live_readiness_reads_the_key_engine_status_actually_publishes(monkeypatch) -> None:
    """The strategy gate must read the real payload, not a convenient stub.

    These tests used to mock ``{"runs": [...]}``. ``Engine.status()`` never
    publishes ``runs`` - it publishes ``strategies`` - so every test passed
    against a shape production could not produce, and the gate read an empty
    list and skipped itself. Arming LIVE then reported "ready" with the
    strategy unverified. Asserting against the real key is the only version of
    this test that can fail when the two drift apart again.
    """
    from engine import StrategyEngine

    source = inspect.getsource(StrategyEngine.status)
    assert '"strategies"' in source, (
        "Engine.status no longer publishes 'strategies'; _live_readiness reads "
        "that key, so the strategy gate would silently stop running."
    )
    assert '"runs"' not in source, (
        "Engine.status publishes 'runs'; update _live_readiness to match."
    )
    # And the reader must name the same key, so the two cannot drift apart.
    reader = inspect.getsource(bot_app._live_readiness)
    assert '.get("strategies")' in reader, (
        "_live_readiness no longer reads 'strategies' from the engine status, so "
        "it is checking a key that is not there."
    )


def test_live_readiness_blocks_when_no_book_is_reported(monkeypatch) -> None:
    """An engine that names no books is not evidence that one is trading."""
    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26NOVFUT", "broker": 0, "ledger": 0,
              "in_sync": True, "error": None},
    )
    monkeypatch.setattr(
        bot_app, "get_engine",
        lambda: SimpleNamespace(status=lambda: {"strategies": []}),
    )

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert "strategy_ready" in report["blocking"]


def test_two_books_never_hold_opposing_legs_on_one_contract(monkeypatch, tmp_path) -> None:
    """The unavailable rule, stated as a test.

    Both books place orders on the *same* front-month future, because
    ``execute`` prices and trades ``manager.current_symbol()`` regardless of which
    series produced the signal. Position lookups are scoped per book, so nothing
    stops one book going long while the other goes short: the ledger then holds
    +1 and -1 lots that cancel at the broker, and the only thing joining a signal
    to its own leg is the ``strategy_id`` stamp.

    That is fine while it is one operator reading one dashboard, and it is the
    reason this test exists rather than a fix: the failure is invisible in every
    aggregate. Net exposure reads flat, the portfolio shows nothing, and the two
    opened trades are the only evidence.
    """
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    symbol = "NIFTY26DECFUT"

    long_book = manager._execute_directional({}, "BUY", symbol, PAPER, "test", strategy_id="book-a")
    short_book = manager._execute_directional({}, "SELL", symbol, PAPER, "test", strategy_id="book-b")

    assert long_book.ok, long_book.reason
    assert short_book.ok, short_book.reason

    positions = db.get_open_positions(mode=PAPER)
    # Two legs on one contract, opposite signs.
    assert len(positions) == 2
    assert {p["strategy_id"] for p in positions} == {"book-a", "book-b"}
    assert sum(int(p["quantity"]) * int(p["position_key"]) for p in positions) == 0

    # Each book sees only its own leg, which is what makes the double entry
    # possible in the first place.
    assert manager._current_net_lots(symbol, PAPER, 65, "book-a") == 1
    assert manager._current_net_lots(symbol, PAPER, 65, "book-b") == -1
    # But the broker sees nothing open.
    assert manager._current_net_lots(symbol, PAPER, 65, None) == 0


def _readiness_live_screen(monkeypatch, *, loop_age, quote_age):
    """Make the readiness report think the market is open, on a given screen."""
    import kite_client

    _readiness_env(
        monkeypatch,
        sync={"symbol": "NIFTY26DECFUT", "broker": 0, "ledger": 0,
              "in_sync": True, "error": None},
    )
    # After the helper, which pins the market shut for the other tests here.
    monkeypatch.setattr(
        kite_client, "market_status", lambda: {"is_open": True, "reason": "open"}
    )
    monkeypatch.setattr(
        bot_app, "api_status",
        lambda: {
            "strategy": {"heartbeat": {"age_seconds": loop_age, "interval_seconds": 5.0}},
            "kite": {"quote_age_seconds": quote_age},
        },
    )


def test_live_readiness_blocks_when_the_mark_loop_has_died(monkeypatch) -> None:
    """Every other check passes against a bot whose screen is frozen.

    The mark loop is what marks positions to market and samples equity, so when
    it dies the P&L stops updating and the dashboard shows a quiet market. Arming
    LIVE on that screen is the one mistake the configuration checks cannot catch,
    because none of them is about whether anything is still running.
    """
    _readiness_live_screen(monkeypatch, loop_age=600.0, quote_age=1.0)

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert "loop_alive" in report["blocking"]


def test_live_readiness_blocks_when_no_quote_is_arriving(monkeypatch) -> None:
    """An authenticated session with no quotes cannot price an order."""
    _readiness_live_screen(monkeypatch, loop_age=1.0, quote_age=600.0)

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert "quote_fresh" in report["blocking"]


def test_live_readiness_passes_on_a_live_updating_screen(monkeypatch) -> None:
    _readiness_live_screen(monkeypatch, loop_age=1.0, quote_age=1.0)

    report = bot_app._live_readiness()

    assert report["ready"] is True
    assert {c["key"] for c in report["checks"]} >= {"loop_alive", "quote_fresh"}


def test_live_readiness_treats_a_never_run_loop_as_dead(monkeypatch) -> None:
    """``None`` is "no evidence", not "fresh" - the bug that made the gate skip."""
    _readiness_live_screen(monkeypatch, loop_age=None, quote_age=None)

    report = bot_app._live_readiness()

    assert report["ready"] is False
    assert {"loop_alive", "quote_fresh"} <= set(report["blocking"])


def test_the_alert_toggle_is_reachable_and_persists(monkeypatch, tmp_path) -> None:
    """The toggle read an undefined name, so every call silently took the fallback.

    ``enabled()`` wrapped the lookup in ``except Exception`` and logged a warning,
    which made a ``NameError`` indistinguishable from "no override stored": the
    button appeared to work, wrote nothing, and the .env default applied forever.
    A warning-level failure inside the function that decides whether to alert is
    itself invisible, so the behaviour is asserted rather than the log.
    """
    use_test_database(monkeypatch, tmp_path)
    import notifier

    monkeypatch.setattr(notifier, "settings", SimpleNamespace(alert_enabled=False))

    assert notifier.enabled() is False

    assert notifier.set_enabled(True) is True
    assert notifier.enabled() is True
    assert db.get_settings().get(notifier.ENABLED_KEY) == "1"

    assert notifier.set_enabled(False) is False
    assert notifier.enabled() is False

    # Clearing the override returns to the file's default rather than to False:
    # storing "0" would make a deleted setting indistinguishable from an
    # operator decision.
    assert notifier.clear_enabled() is False
    assert notifier.ENABLED_KEY not in db.get_settings()

    monkeypatch.setattr(notifier, "settings", SimpleNamespace(alert_enabled=True))
    assert notifier.set_enabled(False) is False
    assert notifier.clear_enabled() is True


def _book(live: bool, strategy_id: str = "book"):
    from bar_source import GiftBarSource
    from strategy_run import StrategyRun

    return StrategyRun(strategy_id, GiftBarSource(None), live=live)


def test_a_book_that_did_not_opt_in_stays_paper_when_the_bot_goes_live() -> None:
    """The bot's switch is the master control; the book's flag is its consent.

    Both directions matter. A book flagged live must still simulate while the bot
    is in PAPER - the flag is not a bypass. And a book that did not opt in must
    keep its paper ledger when the bot is armed, which is what lets the GIFT index
    book keep running unchanged beside a live NIFTY book.
    """
    from config import LIVE, PAPER

    paper_book = _book(live=False)
    live_book = _book(live=True)

    assert paper_book.mode(PAPER) == PAPER
    assert live_book.mode(PAPER) == PAPER, "a per-book flag must not bypass PAPER"
    assert paper_book.mode(LIVE) == PAPER, "no consent, no real orders"
    assert live_book.mode(LIVE) == LIVE


def test_the_default_for_a_book_is_paper() -> None:
    """An unset flag must not be what puts real money on a signal.

    The failure this prevents is silent and expensive: a new strategy added
    without thinking about the flag would otherwise trade live the moment the bot
    was armed, on a rule nobody had considered funding.
    """
    from bar_source import GiftBarSource
    from strategy_run import StrategyRun

    run = StrategyRun("new-book", GiftBarSource(None))

    assert run.live is False
    assert run.status()["live"] is False
    assert run.status()["mode"] == "PAPER"


def test_each_book_reports_its_own_mode(monkeypatch) -> None:
    """The dashboard must not show a paper book as live."""
    from config import LIVE

    # The bot is armed; only one book consented. Reporting from the bot's mode
    # alone would label both bands live. StrategyRun.mode resolves the global
    # mode through config, so that is the module to patch.
    monkeypatch.setattr(config, "get_execution_mode", lambda: LIVE)

    paper_book = _book(live=False, strategy_id="gift-flip")
    live_book = _book(live=True, strategy_id="nifty-flip")

    assert paper_book.status()["mode"] == "PAPER"
    assert live_book.status()["mode"] == LIVE
    # Both are *willing* to be live; only one is armed. The band needs both
    # numbers, because "live" alone would read as "placing real orders now".
    assert live_book.status()["live"] is True
    assert paper_book.status()["live"] is False


def test_accounts_read_each_books_own_ledger(monkeypatch, tmp_path) -> None:
    """A paper book's equity comes from paper, a live book's from the broker.

    Keying accounts on one global mode was invisible while every book shared it.
    With one book live, the paper band would read the live balance and the live
    band the simulated one - the two numbers a trader would act on, swapped.

    A live book now reports the broker's cash and P&L rather than the virtual
    balance the paper ledger was seeded with, so it is asserted separately: the
    seed is money that does not exist at the broker, and showing it beside a LIVE
    chip is the specific mistake this test is about.
    """
    use_test_database(monkeypatch, tmp_path)
    from config import LIVE, PAPER

    monkeypatch.setattr(
        bot_app, "get_client",
        lambda: SimpleNamespace(
            available_cash=lambda: 91_229.6,
            positions=lambda force=False: [
                {"tradingsymbol": "NIFTY26DECFUT", "quantity": 65, "pnl": 3_900.0}
            ],
        ),
    )

    # The account row is what the helper iterates, so seed it explicitly.
    db.get_account(strategy_id="book-a")
    # Same book, an open position in each mode at a different mark.
    db.open_position("NIFTY26DECFUT", 65, 100.0, mode=PAPER, strategy_id="book-a")
    db.open_position("NIFTY26DECFUT", 65, 200.0, mode=LIVE, strategy_id="book-a")
    db.mark_open_positions(PAPER, {"NIFTY26DECFUT": 110.0})
    db.mark_open_positions(LIVE, {"NIFTY26DECFUT": 260.0})

    paper = bot_app._strategy_accounts({"book-a": PAPER})["book-a"]
    live = bot_app._strategy_accounts({"book-a": LIVE})["book-a"]

    # Paper: the ledger's own mark, 10 points on 65 units, from the seed balance
    # (whatever VIRTUAL_BALANCE seeds it with, not the broker's cash).
    assert paper["source"] == "ledger"
    assert paper["unrealised"] == 65 * 10.0
    assert paper["balance"] > 0 and paper["balance"] != 91_229.6
    assert paper["mode"] == PAPER

    # Live: the broker's cash and the broker's P&L, not the 200k seed.
    assert live["source"] == "zerodha"
    assert live["balance"] == 91_229.6
    assert live["unrealised"] == 3_900.0
    assert live["equity"] == 91_229.6 + 3_900.0
    assert live["mode"] == LIVE


def test_a_live_book_equity_falls_back_to_the_ledger_without_the_broker(
    monkeypatch, tmp_path
) -> None:
    """If Zerodha cannot be read, report the ledger and say so.

    A blank equity on a live band is worse than a stale one: it looks like the
    account is empty rather than unreadable, and the operator cannot tell which.
    """
    use_test_database(monkeypatch, tmp_path)
    from config import LIVE

    monkeypatch.setattr(
        bot_app, "get_client",
        lambda: SimpleNamespace(available_cash=lambda: None, positions=lambda force=False: []),
    )
    db.get_account(strategy_id="book-a")

    live = bot_app._strategy_accounts({"book-a": LIVE})["book-a"]

    assert live["source"] == "ledger"
    assert live["equity"] is not None


def test_a_single_mode_string_still_means_one_mode_for_all_books() -> None:
    """The map is additive: existing callers pass a plain mode and must work."""
    from config import PAPER

    assert bot_app._book_mode(PAPER, "anything") == PAPER
    assert bot_app._book_mode(None, "anything") in {"PAPER", "LIVE"}
    assert bot_app._book_mode({"book-a": "LIVE"}, "book-a") == "LIVE"


def test_performance_counts_only_the_books_own_mode(monkeypatch, tmp_path) -> None:
    """A live book's score must not include the paper trades it made first."""
    use_test_database(monkeypatch, tmp_path)
    from config import LIVE, PAPER

    # The same book closed a trade in each mode, plus one for another book.
    paper_id = db.open_position("NIFTY26DECFUT", 65, 100.0, mode=PAPER, strategy_id="book-a")
    live_id = db.open_position("NIFTY26DECFUT", 65, 100.0, mode=LIVE, strategy_id="book-a")
    other_id = db.open_position("NIFTY26DECFUT", 65, 100.0, mode=PAPER, strategy_id="book-b")
    db.close_position(paper_id, 110.0)   # +650 paper
    db.close_position(live_id, 180.0)    # +5200 live
    db.close_position(other_id, 120.0)   # +1300 paper, book-b

    perf = bot_app._strategy_performance({"book-a": LIVE, "book-b": PAPER})

    assert perf["book-a"]["trades"] == 1
    assert perf["book-a"]["net_pnl"] == 5200.0, "the paper trade must not count"
    assert perf["book-a"]["mode"] == LIVE
    assert perf["book-b"]["net_pnl"] == 1300.0


def test_the_order_is_booked_into_the_mode_its_book_trades(monkeypatch, tmp_path) -> None:
    """A paper book must not book a fill into the live ledger.

    ``process_signal`` used to read ``get_execution_mode()`` for itself, so the
    caller's per-book mode never reached the ledger: with the bot armed and one
    book still in paper, that book's fills were written as LIVE. This asserts the
    mode the *caller* resolved is the one the order is booked under, which is the
    only way two books with different modes can share one order manager.
    """
    manager, client = _rollover_manager(monkeypatch, tmp_path)
    # The bot is armed, so the *global* mode is LIVE. A book that did not opt in
    # must still be booked as PAPER; without that split the global read wins and
    # this fill is written to the live ledger.
    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: LIVE)
    # LIVE asks the broker for its positions before every order. The question
    # here is which ledger the fill lands in, not the reconciliation, so it is
    # answered with an empty book rather than a fake Zerodha.
    monkeypatch.setattr(
        order_manager.OrderManager, "_live_position_sync_error", lambda self: None
    )
    payload = {"action": "BUY", "symbol": "NIFTY26DECFUT", "signal_id": "sig-1"}

    result = manager.process_signal(
        payload, source="ENGINE", strategy_id="book-a", mode=PAPER
    )

    assert result.ok, result.reason
    assert result.mode == PAPER
    # Booked into the paper ledger, not the live one.
    assert [p["mode"] for p in db.get_open_positions(mode=PAPER)] == [PAPER]
    assert db.get_open_positions(mode=LIVE) == []
    assert len(client.orders) == 1


def test_the_engine_passes_its_books_mode_to_the_order_manager() -> None:
    """The caller must hand the resolved mode down, not let it be re-read."""
    import inspect

    import engine
    import order_manager

    signature = inspect.signature(order_manager.OrderManager.process_signal)
    assert "mode" in signature.parameters, (
        "process_signal cannot honour a per-book mode; it will re-read the global one"
    )
    source = inspect.getsource(engine.StrategyEngine.execute)
    assert "mode=mode" in source, (
        "execute resolves the book's mode but does not pass it to process_signal, "
        "so the order is booked under the global mode instead"
    )


def test_a_mode_switch_resets_every_book_not_just_the_first(monkeypatch, tmp_path) -> None:
    """Arming LIVE must not leave the second book's bar guard behind.

    ``reset_for_mode`` cleared the engine-level attributes, and those mirror
    ``runs[0]`` only. With two books, the second kept its "already evaluated this
    bar" guard: after the switch it skipped the bar it had seen and the dashboard
    went on showing the previous mode's indicators while the loop reported the
    book as running. The visible symptom is that switching to LIVE freezes one
    book's indicators - which is what this pins.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [])
    second = eng.runs[0]
    second.id = "second-book"

    eng._last_bar_at = "2026-11-16T22:00:00+05:30"
    second.state.last_bar_at = "2026-11-16T22:00:00+05:30"
    second.state.overnight_pending = True

    eng.reset_for_mode(LIVE)

    assert eng._last_bar_at is None
    assert second.state.last_bar_at is None, (
        "the second book kept its bar guard, so it will skip the first bar in "
        "the new mode and display the old indicators"
    )
    assert second.state.overnight_pending is False


def test_the_pre_open_reconcile_flag_is_per_book(monkeypatch, tmp_path) -> None:
    """One book's pre-open bar must not arm the other book's reconcile.

    The flag was a single engine field, so a pre-open bar in the GIFT series
    armed a reconcile for the NIFTY book as well - which would then decide the
    morning on a leg it does not hold.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [])
    active = eng.run

    eng._set_overnight_pending(True, active)

    assert active.state.overnight_pending is True
    assert eng._overnight_pending is True

    # A different book must not see it.
    from strategy_run import RunState

    other = type("R", (), {"state": RunState()})()
    assert other.state.overnight_pending is False


def _broker_spy(monkeypatch, mode):
    """A real KiteClient on a fake SDK, so only the broker call is faked."""
    from config import LIVE, PAPER  # noqa: F401
    import kite_client

    class FakeSdk:
        VARIETY_REGULAR = "regular"
        EXCHANGE_NFO = "NFO"
        VALIDITY_DAY = "DAY"
        TRANSACTION_TYPE_BUY = "BUY"
        TRANSACTION_TYPE_SELL = "SELL"
        PRODUCT_NRML = "NRML"
        ORDER_TYPE_MARKET = "MARKET"

        def __init__(self):
            self.submitted = []

        def place_order(self, **kwargs):
            self.submitted.append(kwargs)
            return "real-order-id"

        def quote(self, keys):
            return {k: {"depth": {}, "last_price": 25_000.0} for k in keys}

    sdk = FakeSdk()
    client = kite_client.KiteClient()
    client.mode = mode
    client._kite = sdk
    client._auth_error = None
    monkeypatch.setattr(
        kite_client.KiteClient, "_wait_for_fill",
        lambda self, order_id, timeout=10.0: {
            "order_id": order_id, "status": "COMPLETE",
            "average_price": 25_000.0, "filled_quantity": 65,
        },
    )
    return client, sdk


def test_a_paper_book_cannot_submit_a_real_order(monkeypatch) -> None:
    """The one bug that would cost money without anyone noticing.

    ``place_order`` decided paper-vs-real from ``self.mode`` - the *client's*
    mode, set once by the mode switch. Once one book could be live while another
    stayed in paper, the paper book's order reached the broker for real: the
    order manager had already booked it as simulated, so the ledger said nothing
    happened while money left the account.
    """
    from config import LIVE, PAPER

    client, sdk = _broker_spy(monkeypatch, LIVE)  # the bot is armed

    ack = client.place_order("NIFTY26DECFUT", "BUY", 65, 25_000.0, mode=PAPER)

    assert sdk.submitted == [], "a paper book must never reach the broker"
    assert ack["mocked"] is True
    assert ack["mode"] == PAPER
    assert ack["order_id"].startswith("PAPER-")


def test_a_live_book_does_submit_a_real_order(monkeypatch) -> None:
    """The converse, so the guard cannot be satisfied by never trading at all."""
    from config import LIVE

    client, sdk = _broker_spy(monkeypatch, LIVE)

    ack = client.place_order("NIFTY26DECFUT", "BUY", 65, 25_000.0, mode=LIVE)

    assert len(sdk.submitted) == 1
    assert sdk.submitted[0]["tradingsymbol"] == "NIFTY26DECFUT"
    assert ack["mocked"] is False


def test_the_order_manager_passes_the_books_mode_to_the_broker(monkeypatch, tmp_path) -> None:
    """Threading the mode through is what makes the guard above reachable."""
    import inspect

    import order_manager

    source = inspect.getsource(order_manager.OrderManager)
    assert "mode=mode" in source, (
        "place_order is called without the book's mode, so it falls back to the "
        "client's global mode and a paper book can submit a real order"
    )


def test_a_configured_book_with_no_account_row_still_shows_an_equity(
    monkeypatch, tmp_path
) -> None:
    """Every configured book must appear in `accounts`, not just the ones with rows.

    A fresh database holds one legacy row keyed by the old default id ("Surikbs").
    With gift-flip / nifty-flip configured, neither id had a row, so both bands
    rendered a blank equity - which reads as a broken book rather than one that
    has simply not traded yet. Reading the account seeds it.
    """
    use_test_database(monkeypatch, tmp_path)
    from config import PAPER

    monkeypatch.setattr(
        config, "strategy_configs",
        lambda: [
            SimpleNamespace(id="gift-flip"),
            SimpleNamespace(id="nifty-flip"),
        ],
    )
    # Only the legacy row exists, exactly as in a freshly migrated database.
    assert "gift-flip" not in db.get_accounts()

    accounts = bot_app._strategy_accounts({"gift-flip": PAPER, "nifty-flip": PAPER})

    assert "gift-flip" in accounts, "the band has no account to render"
    assert "nifty-flip" in accounts
    assert accounts["gift-flip"]["equity"] is not None


def test_a_switched_off_channel_is_not_reported_as_unconfigured(
    monkeypatch, tmp_path
) -> None:
    """"Configured" and "armed" are different questions.

    `configured()` answers "would a message go out right now", so it is False
    whenever alerting is switched off. The dashboard read it as "is the channel
    set up", and a fully configured Telegram channel therefore reported "not
    configured" the moment the operator switched it off - the switch looked
    broken rather than off.
    """
    use_test_database(monkeypatch, tmp_path)
    db.delete_settings(["alert_enabled"])

    import dataclasses
    import notifier
    from config import settings

    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=False,
        alert_channel="telegram",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="321056238",
    ))

    state = notifier.status()

    assert state["enabled"] is False
    assert state["configured"] is False, "nothing would be sent right now"
    assert state["channel_ready"] is True, "the channel is set up and merely off"

    # And an actually unconfigured channel reports both false.
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        notifier.settings, alert_telegram_bot_token="",
    ))
    state = notifier.status()
    assert state["channel_ready"] is False


def test_a_gift_books_configured_interval_is_honoured() -> None:
    """A GIFT book must read the timeframe it was configured for.

    `build_bar_source_for` dropped `cfg.interval` for GIFT sources, so
    STRATEGY_<n>_INTERVAL silently did nothing: every GIFT book read the one
    global feed's bars whatever it asked for. A 15-minute GIFT twin would have
    reported 60-minute on the dashboard and evaluated a series it was not
    configured to read.
    """
    from config import StrategyConfig, build_bar_source_for
    from bar_source import GiftBarSource

    class FakeFeed:
        interval = "60minute"
        seconds = 3600

        def __init__(self):
            self.asked = []

        def candles(self):
            return [{"time": "primary"}]

        def ensure_interval(self, interval):
            self.asked.append(interval)

        def interval_candles(self, interval=None):
            if interval in (None, "60minute"):
                return [{"time": "primary"}]
            return [{"time": "15m", "interval": interval}]

    feed = FakeFeed()
    cfg = StrategyConfig(
        index=3, id="gift-15m", source="gift", interval="15minute",
    )

    src = build_bar_source_for(cfg, gift_feed=feed)

    assert isinstance(src, GiftBarSource)
    assert src.seconds == 900, "the configured interval must set the bar length"
    # The feed is asked to build it, and the book reads that series.
    assert feed.asked == ["15minute"]
    assert src.candles() == [{"time": "15m", "interval": "15minute"}]


def test_one_feed_builds_two_timeframes_from_the_same_ticks() -> None:
    """The websocket cannot be opened twice, so the feed aggregates both.

    Twisted's reactor is process-global and one-shot: a second GIFT book cannot
    create its own ticker. Building the extra timeframe in the same feed is what
    makes two GIFT intervals possible at all.
    """
    from datetime import datetime, timedelta
    from gift_nifty import GiftNiftyFeed, interval_to_seconds
    from database import IST

    feed = GiftNiftyFeed("60minute")
    feed.ensure_interval("15minute")
    feed.ensure_interval("15minute")  # idempotent - polled every evaluation

    assert set(feed._extra_builders) == {"15minute"}

    # Four ticks inside one 15-minute bucket but spread across it.
    base = datetime(2026, 11, 19, 10, 0, tzinfo=IST)
    for minute, price in ((0, 100.0), (4, 104.0), (9, 98.0), (14, 101.0)):
        moment = base + timedelta(minutes=minute)
        feed._builder.add_tick(price, moment, volume=1.0)
        for builder in feed._extra_builders.values():
            builder.on_close = lambda bar, _i="15minute": feed._on_extra_close(_i, bar)
            builder.add_tick(price, moment, volume=1.0)

    # A tick in the next bucket closes both timeframes' current bar.
    later = base + timedelta(minutes=15)
    feed._builder.add_tick(105.0, later, volume=1.0)
    for builder in feed._extra_builders.values():
        builder.add_tick(105.0, later, volume=1.0)

    fifteen = feed.interval_candles("15minute")
    assert len(fifteen) == 1, "one 15-minute bar should have closed"
    bar = fifteen[0]
    assert bar["open"] == 100.0
    assert bar["high"] == 104.0
    assert bar["low"] == 98.0
    assert bar["close"] == 101.0
    # The 60-minute series is still inside its first bar, so nothing closed there.
    assert feed.candles() == []


def test_the_tiles_are_grouped_by_instrument() -> None:
    """One column per instrument, ordered by timeframe inside it.

    GIFT Nifty and NIFTY 50 are different markets - one an around-the-clock index
    that cannot itself be filled, the other the NSE session on the future's own
    underlying - so interleaving them by timeframe mixed two unlike things. The
    columns carry the instrument; the row order inside a column carries the
    like-for-like comparison.

    Grouping by timeframe was also what exposed a real bug: a book whose interval
    was silently reset by a reconnect landed in the wrong group and split the
    four tiles 1-and-3.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    # Two columns, GIFT first, and books sorted by bar length inside a column.
    assert "key: 'gift', title: 'GIFT Nifty'" in js
    assert "key: 'nifty', title: 'NIFTY 50'" in js
    assert ".sort((a, b) => (a.seconds || 0) - (b.seconds || 0))" in js
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in css

    # A tile must not still be built from the old boxed sections.
    assert "book-band-section" not in js, "the boxed sections were replaced by compact rows"
    assert "book-band-foot" in js


def test_a_tile_shows_its_numbers_without_a_labels_column() -> None:
    """The four rows a trader needs, and nothing else.

    Signal, the two rule checks with their distance to a flip, and position /
    P&L / equity. The old tile stacked four headed sections, which is why four
    of them did not fit on one screen.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")

    for expected in (
        "book-band-signal-value",
        "book-band-rules",
        "book-band-foot",
        "ruleRow('SuperTrend'",
        "ruleRow('PSAR'",
    ):
        assert expected in js, expected
    # The mode that the money figure came from stays on the tile: it is the one
    # label that changes what the number means.
    assert "book-band-mode" in js


def test_a_reconnect_keeps_the_books_configured_interval(monkeypatch) -> None:
    """Replacing the feed must not reset the book to the feed's own timeframe.

    `evaluate_run` rebuilds the source when the process feed has been replaced -
    a reconnect does exactly that - and rebuilt it without the interval. A
    15-minute GIFT book silently became 60-minute on the first reconnect: its
    `seconds` went back to the feed's, so it evaluated and reported the wrong
    series for the rest of the process's life. The dashboard showed it as a 60m
    book because it had become one.
    """
    import gift_nifty
    import engine as engine_module

    # The book's interval is configuration, so the test has to state it. It used
    # to rely on the developer's own bot/.env setting STRATEGY_1_INTERVAL=15minute,
    # which passed locally and failed in CI (where .env is not committed) with
    # "60minute != 15minute" - reporting a reconnect bug that was not there.
    monkeypatch.setenv("STRATEGY_1_SOURCE", "gift")
    monkeypatch.setenv("STRATEGY_1_INTERVAL", "15minute")

    first = gift_nifty.GiftNiftyFeed("60minute")
    monkeypatch.setattr(engine_module, "get_feed", lambda: first)
    eng = engine_module.StrategyEngine()
    run = eng.runs[0]

    # The book is configured for 15 minutes on a 60-minute feed.
    assert run.source.interval == "15minute"
    assert run.source.seconds == 900

    # A reconnect swaps the process feed.
    second = gift_nifty.GiftNiftyFeed("60minute")
    monkeypatch.setattr(engine_module, "get_feed", lambda: second)
    monkeypatch.setattr(run.source, "refresh_history", lambda force=False: 0)
    monkeypatch.setattr(run.source, "candles", lambda: [])
    eng.evaluate_run(run)

    assert run.source.feed is second, "the run must follow the new feed"
    assert run.source.interval == "15minute", (
        "the rebuild dropped the configured interval, so the book silently "
        "reverted to the feed's own timeframe"
    )
    assert run.source.seconds == 900


def test_a_second_timeframe_is_seeded_when_it_is_registered(monkeypatch) -> None:
    """A timeframe registered after a refresh must not wait for the next one.

    The primary history refresh is throttled to HISTORY_REFRESH_SECONDS and
    returns early inside the throttle, so a book whose timeframe was registered
    just after one had already run got no history at all: its series stayed empty
    and the dashboard reported "warming up" while the ticks were being aggregated
    correctly. Reproduced against a live feed - a 15-minute GIFT book read 0 bars
    where its 60-minute twin read 64.
    """
    import gift_nifty
    from datetime import datetime, timedelta
    from database import IST

    feed = gift_nifty.GiftNiftyFeed("60minute")
    now = datetime.now(IST)
    rows = [
        {"date": now - timedelta(minutes=15 * n),
         "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5}
        for n in range(40, 0, -1)
    ]
    monkeypatch.setattr(feed, "token", 12345)
    monkeypatch.setattr(gift_nifty, "get_client", lambda: SimpleNamespace(
        historical_data=lambda **kw: rows,
    ))

    feed.ensure_interval("15minute")

    seeded = feed.interval_candles("15minute")
    assert seeded, "registering a timeframe must seed it, not leave it empty"
    assert len(seeded) > 1


def test_pausing_one_book_leaves_the_others_trading(monkeypatch, tmp_path) -> None:
    """A per-book pause must narrow, never widen.

    Pausing Book B from its own tile has to leave Book A entering on its next
    signal - the whole point of a per-book control over the global switch. And
    the global pause must still stop everything regardless of per-book state, so
    the operator's kill switch cannot be undermined by a tile left unpaused.
    """
    use_test_database(monkeypatch, tmp_path)
    import order_manager

    monkeypatch.setattr(order_manager, "get_execution_mode", lambda: PAPER)
    manager = order_manager.OrderManager()

    assert manager.book_paused("book-a") is False

    manager.pause_book("book-b")
    assert manager.book_paused("book-b") is True
    assert manager.book_paused("book-a") is False, "pausing one book must not stop another"

    # The global switch overrides every book.
    manager.pause("PANIC")
    assert manager.book_paused("book-a") is True
    assert manager.book_paused("book-b") is True
    assert manager.book_paused_reason("book-a") == "PANIC"

    # Clearing the global pause restores the per-book state, not to "all clear".
    manager.resume()
    assert manager.book_paused("book-a") is False
    assert manager.book_paused("book-b") is True, "the per-book pause survives the global resume"

    manager.resume_book("book-b")
    assert manager.book_paused("book-b") is False


def test_exits_are_never_blocked_by_a_pause() -> None:
    """A position that cannot be closed is worse than one that cannot be opened."""
    import inspect
    import order_manager

    source = inspect.getsource(order_manager.OrderManager.process_signal)
    assert 'if action in {BUY, SELL} and self.book_paused(strategy_id):' in source, (
        "the pause must gate entries only; gating FLAT/PANIC would trap a book"
    )


def test_an_entry_is_scoped_to_the_book_that_sent_it(monkeypatch, tmp_path) -> None:
    """Every entry must be booked under the signal's own book.

    ``process_signal`` called ``_execute_directional`` without ``strategy_id``,
    which defaulted to the legacy id. So the position lookup was scoped to a book
    nobody had configured: the configured book looked flat while that default id
    accumulated its legs, and the second entry was then rejected as "already
    positioned at -1 lots" against the first book's fill. On the live bot this
    read as "why was the GIFT trade rejected" - nothing was wrong with the signal.
    """
    import inspect

    import order_manager

    source = inspect.getsource(order_manager.OrderManager.process_signal)
    assert "strategy_id=strategy_id" in source, (
        "_execute_directional is called without the book, so the entry is scoped "
        "to the legacy default and lands in the wrong ledger"
    )

    # And the parameter must not have a default that hides the omission again.
    signature = inspect.signature(order_manager.OrderManager._execute_directional)
    assert signature.parameters["strategy_id"].default is inspect.Parameter.empty, (
        "a default strategy_id makes a missing one silent rather than a TypeError"
    )


def test_dashboard_positions_are_grouped_by_book_with_its_timeframe() -> None:
    """A leg is only meaningful with the book and the clock that produced it.

    The same contract held by a 15m and a 60m book is two different trades, so
    the panel groups by book and puts the timeframe beside the name - a flat list
    of legs under one contract said nothing about which strategy owned which.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    assert "pos-group-head" in js and ".pos-group-head" in css
    assert "pos-group-tf" in js and ".pos-group-tf" in css
    assert "pos-group-subtotal" in js

    # Grouped on the book, not the contract: two books can hold the same symbol.
    assert "groups.set(key, [])" in js
    assert "row.strategy_id || '—'" in js

    # An unattributed leg must still be shown - never silently dropped.
    assert "unattributed" in js


def test_a_portfolio_refusal_does_not_raise_on_an_undefined_name() -> None:
    """`evaluate_run` used a bare `detail` in six places; it is `verdict.detail`.

    The name did not exist in that scope, so every one of those paths raised
    NameError instead of recording a refusal. It only surfaced when a book
    actually hit one - here the portfolio gate, when the orphaned leg was still
    open - and the dashboard showed the exception text in place of the verdict,
    which reads as a broken strategy rather than a refused entry.
    """
    import inspect

    import engine

    source = inspect.getsource(engine.StrategyEngine.evaluate_run)
    assert 'f"GIFT {detail}"' not in source, (
        "`detail` is not defined in evaluate_run; the record_signal call will "
        "raise NameError the first time it runs"
    )
    assert 'f"GIFT {verdict.detail}"' in source


# ---------------------------------------------------------------------------
# Execution legs: one strategy, one signal, one or two venues
#
# A strategy reads one series and may carry the signal on more than one
# instrument. Each leg fills, marks and exits on its own, so the leg list is a
# property of the strategy rather than of the signal - and getting it wrong is
# silent: a strategy configured for the index alone still traded the future,
# which looks like a working bot trading an instrument nobody asked for.
# ---------------------------------------------------------------------------
def test_legs_are_parsed_and_deduplicated(monkeypatch) -> None:
    """A repeated leg must not double the position size."""
    import config

    assert config._parse_legs("") == ()
    assert config._parse_legs("gift") == ("gift",)
    assert config._parse_legs("GIFT, futures") == ("gift", "futures")
    assert config._parse_legs("futures,futures") == ("futures",)
    # An unknown leg is a startup error, not a silently dropped order.
    try:
        config._parse_legs("gift,options")
    except ValueError as exc:
        assert "options" in str(exc)
    else:  # pragma: no cover - the assertion above is the test
        raise AssertionError("an unknown leg was accepted")


def test_unset_legs_default_to_what_that_source_used_to_do(monkeypatch) -> None:
    """The historical default differs by source, so one shared default is wrong.

    A GIFT-sourced book has always settled the paper index book *and* placed the
    futures order. A NIFTY-sourced one never had an index leg. Defaulting both to
    futures alone would silently switch off an existing deployment's GIFT book on
    upgrade - a change nobody asked for and nothing would report.
    """
    import config

    gift = config.StrategyConfig(index=1, id="a", source="gift", interval="15minute")
    nifty = config.StrategyConfig(index=2, id="b", source="nifty", interval="60minute")
    explicit = config.StrategyConfig(
        index=3, id="c", source="gift", interval="60minute", legs=("gift",)
    )

    assert gift.resolved_legs == ("gift", "futures")
    assert nifty.resolved_legs == ("futures",)
    assert explicit.resolved_legs == ("gift",)
    # Reported to the dashboard, so the tile can draw a row per leg.
    assert gift.to_dict()["legs"] == ["gift", "futures"]
    assert nifty.to_dict()["legs"] == ["futures"]


def test_a_gift_only_book_places_no_futures_order(monkeypatch, tmp_path) -> None:
    """``LEGS=gift`` means the index leg and nothing else.

    Without the guard the leg list was parsed, reported, and then ignored: the
    book settled its index ledger *and* placed a futures order, so a strategy the
    operator had confined to a paper index was trading a real contract.
    """
    import bar_source

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
    ])
    gift_only = engine.StrategyRun(
        "gift-only", bar_source.GiftBarSource(feed), params={"exit": "flip"}, lots=1,
        legs=("gift",),
    )
    eng.runs = [gift_only]

    before = len(db.get_open_positions(mode=PAPER, strategy_id="gift-only"))
    feed.n = 1
    result = eng.evaluate_run(gift_only)

    assert result["action"] == "BUY"
    assert len(db.get_open_positions(mode=PAPER, strategy_id="gift-only")) == before, (
        "a gift-only book placed a futures order"
    )


def test_a_two_leg_book_places_the_futures_order_and_settles_the_index(monkeypatch, tmp_path) -> None:
    """The two legs are independent: one filling does not stand in for the other."""
    import bar_source
    import gift_book

    _isolate_gift_book(monkeypatch, tmp_path)
    book = gift_book.get_gift_book()
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
    ])
    monkeypatch.setattr(
        eng, "_settle_gift_book",
        engine.StrategyEngine._settle_gift_book.__get__(eng),
    )
    both = engine.StrategyRun(
        "both-legs", bar_source.GiftBarSource(feed), params={"exit": "flip"}, lots=1,
        legs=("gift", "futures"),
    )
    eng.runs = [both]

    feed.n = 1
    result = eng.evaluate_run(both)

    assert result["action"] == "BUY"
    # The futures leg is on the ledger...
    assert len(db.get_open_positions(mode=PAPER, strategy_id="both-legs")) == 1
    # ...and the index leg is on its own, priced on the index.
    assert book.net_units() == 65


def test_the_index_ledger_is_scoped_to_the_strategy_that_owns_it(monkeypatch, tmp_path) -> None:
    """Two GIFT-sourced strategies are two books, not two views of one.

    A single process-wide book let the 15-minute strategy read the 60-minute
    strategy's position as its own, close it, and report P&L that belonged to
    neither - which looks like a strategy closing trades it never opened.
    """
    import gift_book

    monkeypatch.setattr(
        gift_book, "settings",
        SimpleNamespace(
            gift_book=True, gift_book_db_path=str(tmp_path / "gift.db"),
            gift_book_units=65, gift_book_balance=200_000.0,
            tick_size=0.05, slippage_ticks=1.0, lot_size=65,
        ),
    )
    gift_book.reset_gift_books()
    try:
        fast = gift_book.get_gift_book("gift-15m")
        slow = gift_book.get_gift_book("gift-60m")

        assert fast is not slow
        assert fast.strategy_id == "gift-15m"
        # Separate files, so one cannot even read the other's rows.
        assert fast.path != slow.path

        fast.execute("BUY", 25_000.0)
        assert fast.net_units() == 65
        assert slow.net_units() == 0, "one strategy's book read another's position"
    finally:
        gift_book.reset_gift_books()


def test_a_paused_book_also_holds_its_index_leg(monkeypatch, tmp_path) -> None:
    """The pause is one operator decision, not one per venue.

    The index book read the global switch only, so a book paused from its own
    tile kept opening index trades - the pause looked applied on the futures leg
    and ignored on the other.
    """
    import order_manager

    book = _gift_book(monkeypatch, tmp_path)
    book.strategy_id = "gift-15m"
    monkeypatch.setattr(
        order_manager, "get_order_manager",
        lambda: SimpleNamespace(book_paused=lambda sid: sid == "gift-15m"),
    )

    assert book.execute("BUY", 25_000.0) is None
    assert book.net_units() == 0


def test_the_dashboard_draws_one_card_per_execution_leg() -> None:
    """A two-leg strategy is two positions, and each must get its own card.

    The tile had one Position cell, so a strategy carrying its signal on two
    venues showed one of them and hid the other - the half that was working and
    the half that was not were indistinguishable. A table row was the first fix;
    it still put both legs under one shared P&L and one shared mode, so the split
    is now a card per leg with its own levels and score.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    assert "function renderLegCards(run, legPerf, columnKey)" in js
    assert "book-leg-cards" in js and ".book-leg-cards" in css
    assert "leg-card-venue" in js and ".leg-card-venue" in css
    assert "leg-card-levels" in js and ".leg-card-levels" in css
    # Both legs' scores come from the server, per venue.
    assert "payload.leg_performance" in js
    assert '"leg_performance": legs' in (Path(__file__).resolve().parent.parent
                                         / "bot" / "bot.py").read_text(encoding="utf-8")
    # A single-leg book keeps its one footer row: a card would repeat the same
    # numbers in twice the height.
    assert "if (legs.length < 2) return '';" in js
    assert "Array.isArray(run.legs)" in js
    # The GIFT leg is paper by construction and must never inherit a LIVE chip.
    assert "isGift ? 'PAPER'" in js
    # The split tile must not restyle itself from its own container query.
    assert ".book-band-split { container-type: inline-size; }" in css


def test_a_half_filled_two_leg_strategy_is_reported_as_partial(monkeypatch, tmp_path) -> None:
    """Two legs, two fills, and either can fail on its own.

    Treating "the strategy acted" as one outcome is what made a half-filled
    strategy invisible: the tile showed a position and nothing said the other leg
    had been rejected. The report is what turns a partial fill into a recorded
    fact rather than something to infer from two unrelated logs.
    """
    import bar_source

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
    ])
    run = engine.StrategyRun(
        "both-legs", bar_source.GiftBarSource(feed), params={"exit": "flip"}, lots=1,
        legs=("gift", "futures"),
    )

    # The futures order was rejected; the index leg settled.
    report = eng._report_partial_legs(
        "BUY", {"ok": False, "reason": "rejected"}, {"action": "BUY"}, run,
        "2026-11-16T09:30:00+05:30",
    )

    assert report is not None
    assert report["filled"] == ["gift"]
    assert report["failed"] == ["futures"]
    assert "Partial execution" in run.state.last_error


def test_both_legs_filling_is_not_reported_as_partial(monkeypatch, tmp_path) -> None:
    """A clean two-leg fill must not be flagged; a warning on every bar is noise."""
    import bar_source

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [])
    run = engine.StrategyRun(
        "both-legs", bar_source.GiftBarSource(feed), params={}, lots=1,
        legs=("gift", "futures"),
    )

    assert eng._report_partial_legs(
        "BUY", {"ok": True}, {"action": "BUY"}, run, "2026-11-16T09:30:00+05:30"
    ) is None
    # A single-leg book has nothing to be partial about.
    single = engine.StrategyRun(
        "one-leg", bar_source.GiftBarSource(feed), params={}, lots=1, legs=("futures",),
    )
    assert eng._report_partial_legs(
        "BUY", {"ok": False}, None, single, "2026-11-16T09:30:00+05:30"
    ) is None


def test_merged_gift_analytics_keeps_the_single_book_shape() -> None:
    """A consolidated report must not blank the page it consolidates for.

    The Performance tab reads ``days`` and ``summary`` off the top level. With one
    book per strategy, returning only a per-book map left both undefined and the
    calendar rendered empty - a consolidation that silently removed the page.
    """
    import bot as bot_app

    merged = bot_app._merge_analytics([
        {
            "summary": {"trades": 2, "wins": 1, "losses": 1, "net_pnl": 100.0,
                        "gross_pnl": 300.0, "fees": 200.0, "points": 10.0,
                        "gross_win": 300.0, "gross_loss": -200.0,
                        "largest_win": 300.0, "largest_loss": -200.0,
                        "avg_hold_minutes": 30.0},
            "streaks": {"max_win": 1, "max_loss": 1},
            "days": [{"date": "2026-11-19", "trades": 2, "wins": 1, "losses": 1,
                      "net_pnl": 100.0, "gross_pnl": 300.0, "fees": 200.0, "points": 10.0}],
            "by_hour": [{"hour": 10, "trades": 2, "wins": 1, "losses": 1,
                         "net_pnl": 100.0, "gross_pnl": 300.0, "fees": 200.0, "points": 10.0}],
            "by_direction": {"LONG": {"trades": 2, "wins": 1, "losses": 1, "net_pnl": 100.0}},
        },
        {
            "summary": {"trades": 3, "wins": 3, "losses": 0, "net_pnl": 50.0,
                        "gross_pnl": 100.0, "fees": 50.0, "points": 5.0,
                        "gross_win": 100.0, "gross_loss": 0.0,
                        "largest_win": 60.0, "largest_loss": 0.0,
                        "avg_hold_minutes": 90.0},
            "streaks": {"max_win": 3, "max_loss": 0},
            # The same date, from the other book: one day carrying both.
            "days": [{"date": "2026-11-19", "trades": 3, "wins": 3, "losses": 0,
                      "net_pnl": 50.0, "gross_pnl": 100.0, "fees": 50.0, "points": 5.0}],
            "by_hour": [{"hour": 10, "trades": 3, "wins": 3, "losses": 0,
                         "net_pnl": 50.0, "gross_pnl": 100.0, "fees": 50.0, "points": 5.0}],
            "by_direction": {"LONG": {"trades": 3, "wins": 3, "losses": 0, "net_pnl": 50.0}},
        },
    ])

    # Additive figures are summed...
    assert merged["summary"]["trades"] == 5
    assert merged["summary"]["net_pnl"] == 150.0
    # ...and the ratios are recomputed, not averaged: 4 wins of 5 trades.
    assert merged["summary"]["win_rate"] == 80.0
    assert merged["summary"]["profit_factor"] == pytest.approx(400.0 / 200.0)
    assert merged["summary"]["avg_win"] == pytest.approx(400.0 / 4)
    # The same date must be one bucket carrying both books.
    assert len(merged["days"]) == 1
    assert merged["days"][0]["trades"] == 5
    assert merged["days"][0]["net_pnl"] == 150.0
    assert merged["by_hour"][0]["trades"] == 5
    assert merged["by_direction"]["LONG"]["trades"] == 5
    # The maxima survive, so a long streak in one book is not lost.
    assert merged["streaks"]["max_win"] == 3
    # Nothing may be a non-finite float that JSON cannot represent.
    import json

    json.dumps(merged, allow_nan=False)


def test_the_default_gift_book_keeps_the_unsuffixed_ledger(monkeypatch, tmp_path) -> None:
    """Pointing the default book at a new file would erase the operator's history.

    Every row written before books were per-strategy lives in ``gift_book.db``.
    A per-strategy file for the default id would open an empty ledger beside it,
    so the dashboard would show a GIFT book that had never traded while the real
    history sat unread on disk - a silent data loss on upgrade.
    """
    import gift_book

    monkeypatch.setattr(
        gift_book, "settings",
        SimpleNamespace(
            gift_book=True, gift_book_db_path=str(tmp_path / "gift.db"),
            gift_book_units=65, gift_book_balance=200_000.0,
        ),
    )
    gift_book.reset_gift_books()
    try:
        default = gift_book.get_gift_book()
        explicit = gift_book.get_gift_book("gift-15m")

        assert default.strategy_id == gift_book.DEFAULT_STRATEGY
        # The legacy path, not a suffixed one.
        assert default.path == str(tmp_path / "gift.db")
        # A named strategy still gets its own file.
        assert explicit.path == str(tmp_path / "gift.db.gift-15m")
    finally:
        gift_book.reset_gift_books()


def test_a_legacy_index_ledger_is_listed_rather_than_hidden(monkeypatch, tmp_path) -> None:
    """Rows from before per-strategy books must not vanish from the dashboard.

    A book nobody configured still holds the operator's GIFT history. Omitting it
    from the list does not delete the rows - it hides them, so the venue reports
    a book that has never traded while the real history sits unread on disk.
    """
    import gift_book
    import order_manager

    stub = SimpleNamespace(
        gift_book=True, gift_book_db_path=str(tmp_path / "gift.db"),
        gift_book_units=65, gift_book_balance=200_000.0,
        tick_size=0.05, slippage_ticks=1.0, lot_size=65,
        default_lots=1,
    )
    monkeypatch.setattr(gift_book, "settings", stub)
    monkeypatch.setattr(order_manager, "settings", stub)
    # One configured strategy that trades the index leg.
    monkeypatch.setattr(
        "config.strategy_configs",
        lambda: [SimpleNamespace(
            id="gift-15m", enabled=True, resolved_legs=("gift", "futures"),
        )],
    )
    gift_book.reset_gift_books()
    try:
        # The legacy book trades; the configured one has not.
        gift_book.get_gift_book().execute("BUY", 25_000.0)

        listed = {book.strategy_id for book in gift_book.gift_books()}

        assert "gift-15m" in listed
        assert gift_book.DEFAULT_STRATEGY in listed, (
            "the legacy ledger holds real rows and must be listed"
        )
    finally:
        gift_book.reset_gift_books()


def test_live_readiness_refuses_an_account_that_cannot_fund_one_lot(
    monkeypatch, tmp_path
) -> None:
    """Every other check passes on an unfunded account, which is the trap.

    Configuration, session, reconciliation and the loop all look healthy on an
    account that cannot afford a single contract. Arming LIVE there produces a
    bot that rejects every entry with "Insufficient margin" - which reads as a
    strategy that never fires rather than as an account that cannot trade.
    """
    import bot as bot_app

    # One NRML NIFTY lot needs ~Rs 1.87 lakh; the account holds Rs 91k.
    _readiness_env(
        monkeypatch, sync={"in_sync": True, "broker": 0, "ledger": 0, "error": None},
        margin=187_507.19, cash=91_229.60,
    )

    report = bot_app._live_readiness()

    margin = next(c for c in report["checks"] if c["key"] == "margin")
    assert margin["ok"] is False
    assert margin["blocking"] is True
    assert "margin" in report["blocking"]
    assert report["ready"] is False
    # The figures are in the detail, so the operator knows how much is missing.
    assert "91,230" in margin["detail"]
    assert "187,507" in margin["detail"]


def test_live_readiness_passes_when_the_account_can_fund_a_lot(
    monkeypatch, tmp_path
) -> None:
    """A funded account must not be blocked by the new check."""
    import bot as bot_app

    _readiness_env(
        monkeypatch, sync={"in_sync": True, "broker": 0, "ledger": 0, "error": None},
        margin=187_507.19, cash=500_000.0,
    )

    report = bot_app._live_readiness()

    margin = next(c for c in report["checks"] if c["key"] == "margin")
    assert margin["ok"] is True
    assert "margin" not in report["blocking"]


def test_an_unreadable_margin_is_reported_but_does_not_block(
    monkeypatch, tmp_path
) -> None:
    """Unknown is not the same as fine, but it is not evidence of a problem.

    The margin API can be unavailable while the session is valid, and refusing
    the arm outright would block an operator who funded the account another way.
    """
    import bot as bot_app

    _readiness_env(
        monkeypatch, sync={"in_sync": True, "broker": 0, "ledger": 0, "error": None},
        margin=None, cash=91_229.60,
    )

    report = bot_app._live_readiness()

    margin = next(c for c in report["checks"] if c["key"] == "margin")
    assert margin["ok"] is False
    assert margin["blocking"] is False
    assert "margin" not in report["blocking"]


def test_the_board_files_a_book_under_the_venue_it_is_exposed_to() -> None:
    """The column is the venue the operator is exposed to, not the signal series.

    A GIFT-sourced book that carries its trade on the Nifty future is NIFTY
    exposure. Filing it under "GIFT Nifty" put a real futures position in a
    column labelled "paper only", which is the one thing that column must never
    contain.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")

    # The column comes from the legs, not from the source symbol.
    assert "const columnFor = (run) => (legsOf(run).includes('futures') ? 'nifty' : 'gift');" in js
    assert "const legsOf = (run) =>" in js
    # The old source-based grouping must be gone, or the two would disagree.
    assert "if (/gift/i.test(symbol) || /\\bgift\\b/i.test(name)) return 'gift';" not in js
    # A book with both legs is one strategy, so it is drawn once - not in both
    # columns, which would read as two books.
    assert "for (const key of columnsFor(run))" not in js
    # The tile names its signal series when that differs from the column, and
    # the comparison is case-insensitive - the column key is lowercase and the
    # label is not, so a strict compare printed "signal NIFTY" on the NIFTY tile.
    assert "signal ${signalVenue}" in js
    assert "signalVenue.toLowerCase() !== String(columnKey || '').toLowerCase()" in js


def test_a_gift_signalled_book_can_trade_the_future_live(monkeypatch) -> None:
    """The signal series and the traded instrument are separate decisions.

    GIFT Nifty is quote-only, so the *index* can never be filled - but a book
    that reads GIFT and executes on the Nifty future is trading a real
    instrument on a real venue. Refusing it live would be refusing the future
    because of the index, which is not the thing being bought.
    """
    import config

    monkeypatch.setenv("STRATEGY_1_SOURCE", "gift")
    monkeypatch.setenv("STRATEGY_1_ID", "gift-15m")
    monkeypatch.setenv("STRATEGY_1_INTERVAL", "15minute")
    monkeypatch.setenv("STRATEGY_1_LEGS", "gift,futures")
    monkeypatch.setenv("STRATEGY_1_LIVE", "1")

    cfg = next(c for c in config.strategy_configs() if c.id == "gift-15m")

    assert cfg.source == "gift"
    assert cfg.resolved_legs == ("gift", "futures")
    assert cfg.live is True


def test_an_armed_book_says_so_before_the_switch_is_thrown() -> None:
    """A PAPER chip alone cannot distinguish an armed book from a paper-only one.

    ``run.live`` is the book's consent to trade real money; ``run.mode`` is what
    it is doing now. With the bot still in PAPER both books show "PAPER", so the
    operator could not tell which one will place real orders the moment the
    switch is thrown - and that is the one fact the chip exists to convey.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    assert "book-band-armed" in js and ".book-band-armed" in css
    # Only when armed but not yet live - a live book already says LIVE.
    assert "run.live && bookMode !== 'LIVE'" in js
    # The tooltip has to explain the difference, not just repeat the label.
    assert "Armed for LIVE - real orders once the bot is switched to LIVE" in js


def test_arming_live_is_refused_when_a_blocking_check_fails(monkeypatch, tmp_path) -> None:
    """The pre-flight report must be enforced, not merely displayed.

    It used to be advisory: the dashboard showed "LIVE blocked by 1 check" and
    the switch armed anyway, so an account that could not fund a single lot went
    live and then rejected every entry with "Insufficient margin" - which reads
    as a strategy that never fires rather than as an unfunded account. A gate
    that only warns is not a gate.
    """
    from fastapi.testclient import TestClient

    import bot as bot_app

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app, "settings",
        SimpleNamespace(is_live_configured=True, lot_size=65, product="NRML"),
    )
    monkeypatch.setattr(
        bot_app, "_live_readiness",
        lambda: {
            "ready": False,
            "blocking": ["margin"],
            "checks": [{
                "key": "margin", "ok": False, "blocking": True,
                "detail": "Rs 91,230 available, Rs 187,519 needed for 1 lot",
            }],
        },
    )
    # If the gate fails to stop it, this records the switch so the test can say
    # so rather than passing on a silent no-op.
    switched: list[str] = []
    monkeypatch.setattr(bot_app, "set_execution_mode", lambda mode: switched.append(mode))

    response = TestClient(bot_app.app, base_url="http://localhost").post(
        "/api/toggle-mode", json={"mode": "LIVE", "confirm": True}
    )

    assert response.status_code == 409
    assert "margin" in response.json()["detail"]
    assert "187,519" in response.json()["detail"]
    assert switched == [], "LIVE was armed despite a blocking check"


def test_arming_live_is_allowed_when_every_check_passes(monkeypatch, tmp_path) -> None:
    """A clean report must not be blocked by the new gate."""
    from fastapi.testclient import TestClient

    import bot as bot_app

    use_test_database(monkeypatch, tmp_path)
    monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
    monkeypatch.setattr(
        bot_app, "settings",
        SimpleNamespace(is_live_configured=True, lot_size=65, product="NRML"),
    )
    monkeypatch.setattr(
        bot_app, "_live_readiness",
        lambda: {"ready": True, "blocking": [], "checks": []},
    )
    monkeypatch.setattr(bot_app, "set_execution_mode", lambda mode: None)
    monkeypatch.setattr(
        bot_app, "get_client", lambda: SimpleNamespace(mode="PAPER"),
    )
    monkeypatch.setattr(
        bot_app, "get_engine", lambda: SimpleNamespace(reset_for_mode=lambda mode: None),
    )

    response = TestClient(bot_app.app, base_url="http://localhost").post(
        "/api/toggle-mode", json={"mode": "LIVE", "confirm": True}
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "LIVE"


def test_leg_performance_scores_each_venue_separately(monkeypatch, tmp_path) -> None:
    """A two-leg strategy is two positions, so its score is two scores.

    Reporting one P&L for the pair hides the half that is working and the half
    that is not - which is the whole reason the legs are tracked separately. The
    index legs live in their own databases, so they have to be read from there;
    the futures ledger cannot see them.
    """
    import gift_book
    import order_manager

    use_test_database(monkeypatch, tmp_path)
    stub = SimpleNamespace(
        gift_book=True, gift_book_db_path=str(tmp_path / "gift.db"),
        gift_book_units=65, gift_book_balance=200_000.0,
        tick_size=0.05, slippage_ticks=1.0, lot_size=65, default_lots=1,
    )
    monkeypatch.setattr(gift_book, "settings", stub)
    monkeypatch.setattr(order_manager, "settings", stub)
    monkeypatch.setattr(
        "config.strategy_configs",
        lambda: [SimpleNamespace(
            id="gift-15m", enabled=True, resolved_legs=("gift", "futures"),
        )],
    )
    gift_book.reset_gift_books()
    try:
        # One index leg round trip, in profit.
        book = gift_book.get_gift_book("gift-15m")
        book.execute("BUY", 25_000.0)
        book.execute("SELL", 25_100.0)

        # One futures round trip, in loss.
        db.open_position("NIFTY26NOVFUT", 65, 25_400.0, mode=PAPER,
                         source="ENGINE", strategy_id="gift-15m")
        position_id = db.get_open_positions(mode=PAPER, strategy_id="gift-15m")[0]["id"]
        db.close_position(position_id, 25_300.0, fees=0.0, exit_reason="FLIP",
                          quantity=65)

        legs = bot_app._leg_performance(PAPER)

        assert set(legs["gift-15m"]) == {"gift", "futures"}
        assert legs["gift-15m"]["gift"]["trades"] == 1
        assert legs["gift-15m"]["gift"]["net_pnl"] > 0
        assert legs["gift-15m"]["futures"]["trades"] == 1
        assert legs["gift-15m"]["futures"]["net_pnl"] < 0
        # Each venue's own win rate, not the pair's.
        assert legs["gift-15m"]["gift"]["win_rate"] == 100.0
        assert legs["gift-15m"]["futures"]["win_rate"] == 0.0
    finally:
        gift_book.reset_gift_books()


def test_leg_performance_omits_a_leg_that_never_traded(monkeypatch, tmp_path) -> None:
    """An absent key means "no closed trades", not a fabricated zero row.

    Inventing a 0.00 result for a leg that never filled would claim a
    performance it never had; the UI draws the configured legs and fills them
    from here, so absence is the honest answer.
    """
    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_400.0, mode=PAPER,
                     source="ENGINE", strategy_id="gift-15m")
    position_id = db.get_open_positions(mode=PAPER, strategy_id="gift-15m")[0]["id"]
    db.close_position(position_id, 25_300.0, fees=0.0, exit_reason="FLIP", quantity=65)

    legs = bot_app._leg_performance(PAPER)

    assert set(legs["gift-15m"]) == {"futures"}
    assert "gift" not in legs["gift-15m"]


def test_a_flat_leg_does_not_render_as_a_wall_of_dashes() -> None:
    """A flat leg is the common case and it was the least readable.

    Four columns of em-dashes under a big "Flat" said nothing about why the leg
    was not holding, or where it would act. A flat leg now says so in words -
    and it says the *right* words: this strategy enters on a transition, so "the
    rules agree" is not a reason it would enter, and printing that would leave
    the reader expecting an entry that cannot come.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")
    css = (root / "styles.css").read_text(encoding="utf-8")

    assert "leg-card-flat-state" in js and ".leg-card-flat-state" in css
    assert "leg-card-flat-note" in js and ".leg-card-flat-note" in css
    # The reason names the transition rule, not just "the rules agree".
    assert "enters on the next flip, not on the state" in js
    # And the disagreement case is distinguished from it.
    assert "disagree, so there is no signal to act on" in js
    # The flat card must not carry the four-column level grid at all.
    flat_block = js.split("if (!row) {", 1)[1].split("return `", 2)[1]
    assert "leg-card-levels" not in flat_block


def test_the_exit_level_is_stated_once_per_book() -> None:
    """It belongs to the signal, so both legs share it.

    Printing it inside each leg card showed the same number twice and implied
    each leg had its own. It is hoisted to the verdict it came from, and derived
    once - two copies of one rule is how the two drift apart.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")

    # One derivation, used by both the split tile and the single-leg tile.
    assert js.count("Math.max(...singleLevels)") == 1
    assert "const flipLevels" not in js
    assert "singleFlip" in js
    assert "Both legs would act at" in js


def test_the_tile_footer_is_words_not_the_raw_verdict_string() -> None:
    """The last line of a tile is read by a person.

    The verdict ships "close=… psar=… ST bullish confirm BUY 2/2 SELL 0/2",
    which is fine in a tooltip and unreadable as the visible summary. Both the
    split and single-leg tiles show a sentence and keep the raw string in the
    title attribute.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot" / "web"
    js = (root / "app.js").read_text(encoding="utf-8")

    assert "singleFooter" in js
    assert "legFooter" in js
    # The raw detail still has to be reachable for diagnosis.
    assert 'class="strategy-detail${err ? \' is-bad\' : \'\'}" title="${escapeHtml(detail)}"' in js
    # An error is never replaced by the friendly sentence - it is the thing to fix.
    assert "${escapeHtml(err ? detail : legFooter)}" in js
    assert "${escapeHtml(err ? detail : singleFooter)}" in js


# ---------------------------------------------------------------------------
# Read-path performance
#
# These guard the shape of the hot paths, not a timing. A latency assertion
# would be flaky on a loaded CI box; what is worth pinning is that reads take no
# write lock and that the schema is not recreated per call, because those are
# what made a poll cost milliseconds instead of microseconds.
# ---------------------------------------------------------------------------
def test_reads_do_not_take_a_write_lock(monkeypatch, tmp_path) -> None:
    """A read must not serialise against the engine's writes.

    ``session()`` opens with ``BEGIN IMMEDIATE``, which takes the write lock. It
    is right for a write and wrong for a read: every dashboard poll was queuing
    behind the engine for a lock it never needed. In WAL mode readers and writers
    do not block each other, so the read path here takes no transaction at all.
    """
    import database as db

    use_test_database(monkeypatch, tmp_path)
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="book-a")

    statements: list[str] = []
    conn = db._cached_connection()

    class Recording:
        """A proxy around the connection that notes every statement.

        A sqlite3.Connection's attributes are read-only, so patching `execute`
        on the real object is not possible; wrapping it is.
        """

        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args, **kwargs):
            statements.append(str(sql).strip().upper())
            return conn.execute(sql, *args, **kwargs)

    monkeypatch.setattr(db, "_cached_connection", lambda path=None: Recording())
    db.get_open_positions(mode=PAPER)
    db.get_settings()
    monkeypatch.undo()

    assert statements, "nothing was recorded - the spy is not on the read path"
    assert not any(stmt.startswith("BEGIN") for stmt in statements), (
        "a read opened a transaction: " + ", ".join(statements[:3])
    )


def test_the_schema_is_created_once_per_file(monkeypatch, tmp_path) -> None:
    """Re-running the schema per read cost more than every query it guarded.

    It is only ever needed once per database file, so it is gated on a per-file
    flag. The flag must be set only after success - a failed creation remembered
    as done would leave every later read hitting a missing table.
    """
    import database as db

    use_test_database(monkeypatch, tmp_path)
    db.close_connections()

    runs: list[int] = []
    real_migrate = db._migrate

    def counting_migrate(conn):
        runs.append(1)
        return real_migrate(conn)

    monkeypatch.setattr(db, "_migrate", counting_migrate)
    db.init_db()
    first = len(runs)
    for _ in range(20):
        db.init_db()

    assert first >= 1
    assert len(runs) == first, "the schema was recreated on a repeat init_db"


def test_a_fresh_database_file_still_gets_its_schema(monkeypatch, tmp_path) -> None:
    """The per-file cache must be keyed by path, not global.

    The index book keeps its own database per strategy. A process-wide "already
    initialised" flag would leave every one of those empty and every read of them
    failing on a missing table.
    """
    import database as db

    monkeypatch.setattr(
        db, "settings",
        SimpleNamespace(db_path=str(tmp_path / "a.db"), virtual_balance=1_000_000.0),
    )
    db.close_connections()
    db.init_db()
    db.open_position("NIFTY26NOVFUT", 65, 25_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="a")
    assert len(db.get_open_positions(mode=PAPER)) == 1

    # A second file in the same process must get its own schema.
    monkeypatch.setattr(
        db, "settings",
        SimpleNamespace(db_path=str(tmp_path / "b.db"), virtual_balance=500_000.0),
    )
    db.init_db()
    assert db.get_open_positions(mode=PAPER) == []
    db.open_position("NIFTY26NOVFUT", 65, 26_000.0, mode=PAPER,
                     source="ENGINE", strategy_id="b")
    assert len(db.get_open_positions(mode=PAPER)) == 1
    db.close_connections()


def test_refresh_history_does_not_copy_the_series() -> None:
    """The 5 s tick must not duplicate every bar to return a number.

    ``refresh_history`` ends by reporting a count, and it did that through
    ``bar_count()`` -> ``candles()``, which builds a fresh list of the whole
    series. The engine calls it once per book per tick whether or not a bar
    closed, and then throws the count away - so it was paying a full copy every
    5 seconds for nothing. The feed now answers with a length.
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "bot").joinpath("bar_source.py")
    text = src.read_text(encoding="utf-8")

    assert "interval_count" in text
    # The cheaper counter is what the refresh path asks for.
    assert 'getattr(self.feed, "interval_count", None)' in text
    # And refresh_history itself must not fall back to the copying count.
    refresh = text.split("def refresh_history", 1)[1].split("def candles", 1)[0]
    assert "bar_count()" not in refresh, (
        "refresh_history copies the whole series just to return a count"
    )

    feed = (Path(__file__).resolve().parent.parent / "bot").joinpath("gift_nifty.py")
    feed_text = feed.read_text(encoding="utf-8")
    assert "def interval_count(" in feed_text


# ---------------------------------------------------------------------------
# Image and import footprint
# ---------------------------------------------------------------------------
def test_pandas_is_not_imported_for_the_constants() -> None:
    """Importing the strategy module must not pull in the numeric stack.

    ``config`` imports ``strategy`` for its constants alone, and pandas plus
    numpy cost ~58 MB resident between them. A process that only reads settings
    was paying for the whole indicator library. The import is deferred to the
    first indicator call, which happens once per closed bar.
    """
    import subprocess
    import sys
    from pathlib import Path

    bot_dir = Path(__file__).resolve().parent.parent / "bot"
    code = (
        "import sys; sys.path.insert(0, %r);"
        "import config, strategy;"
        "print('pandas' in sys.modules, 'numpy' in sys.modules)"
        % str(bot_dir)
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False False", (
        "importing config/strategy loaded pandas or numpy: " + out.stdout.strip()
    )


def test_the_indicator_stack_still_loads_on_first_use() -> None:
    """Deferring the import must not break the indicators themselves.

    The lazy binding is a module-level global set on first call. If it were set
    before the import succeeded, or not at all, every indicator would fail with
    ``NoneType has no attribute`` - and only on the first bar of the day.
    """
    import strategy

    strategy._load_numeric_stack()
    assert strategy.pd is not None
    assert strategy.ta is not None

    # A real evaluation, not just the binding.
    bars = []
    price = 25_000.0
    for i in range(120):
        open_ = price
        price += 12.0 if i % 3 else -9.0
        bars.append({
            "time": f"2026-11-19T{i // 4:02d}:{(i % 4) * 15:02d}:00+05:30",
            "open": open_, "high": max(open_, price) + 5,
            "low": min(open_, price) - 5, "close": price,
        })
    verdict = strategy.evaluate(bars, {"exit": "flip"})
    assert verdict.signal in {strategy.BULL, strategy.BEAR, strategy.NO_SIGNAL}


def test_the_runtime_image_does_not_install_test_tooling() -> None:
    """pytest belongs in requirements-dev.txt, not the shipped image.

    It was in requirements.txt, so every production image carried pytest and its
    plugins - files the bot never imports, and an import scan that has to walk
    them.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    runtime = (root / "requirements.txt").read_text(encoding="utf-8")
    dev = (root / "requirements-dev.txt").read_text(encoding="utf-8")

    # Check the dependency lines, not the prose: the comment above explains why
    # pytest is absent, and a bare substring match would fail on that.
    runtime_deps = [
        line.strip() for line in runtime.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(dep.startswith("pytest") for dep in runtime_deps), (
        "pytest is a runtime dependency again: " + ", ".join(runtime_deps)
    )
    assert any(line.strip().startswith("pytest") for line in dev.splitlines())
    # The dev file must build on the runtime one, or the two drift apart.
    assert "-r requirements.txt" in dev


def test_the_dockerfile_trims_in_the_stage_that_creates_the_bytes() -> None:
    """Deleting files in a later layer does not shrink the image.

    It hides them behind a whiteout and the bytes stay in the layer that created
    them. Measured: trimming after the COPY saved 12 MB of a 416 MB image;
    trimming inside the install layer saves the real 126 MB.
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(
        encoding="utf-8"
    )

    # The trim must be part of the same RUN as the install.
    install_block = dockerfile.split("RUN pip install", 1)[1].split("\n\n", 1)[0]
    assert "rm -rf" in install_block, (
        "the trim is not in the install layer, so it saves nothing"
    )
    # And the packages it removes must be ones the bot never imports.
    assert "pygments" in install_block
    assert "watchfiles" in install_block
    # Multi-stage: the compiler toolchain must not reach the runtime image.
    assert dockerfile.count("FROM ") >= 2
    assert "build-essential" in dockerfile
    assert "COPY --from=builder" in dockerfile


def test_every_runtime_dependency_is_pinned_exactly() -> None:
    """An unpinned dependency is a build that changes without a commit.

    The image is built from this file, so a floating version means two builds of
    the same commit can install different code - and the one that matters is the
    one placing real orders.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("requirements.txt", "requirements-dev.txt"):
        text = (root / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            assert re.match(r"^[A-Za-z0-9_.\-]+(\[[^\]]+\])?==[0-9]", line), (
                f"{name}: {line!r} is not pinned to an exact version"
            )


def test_autobahn_stays_at_the_version_kiteconnect_pins() -> None:
    """It is the one dependency that cannot be upgraded.

    kiteconnect declares ``autobahn[twisted]==19.11.2``. Bumping it - or letting
    a resolver pick a newer one - breaks the broker SDK, and the SDK is what
    carries the live tick feed. This is recorded as a test because "why is this
    one old?" is a question someone will ask again.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    runtime = (root / "requirements.txt").read_text(encoding="utf-8")

    # Not pinned here at all: it is transitive, and pinning it would fight the
    # SDK's own constraint. Checked against the dependency lines, not the prose -
    # the comment above explains the constraint and would match a substring test.
    deps = [
        line.strip().lower() for line in runtime.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(dep.startswith("autobahn") for dep in deps), (
        "autobahn is pinned in requirements.txt; kiteconnect owns that constraint"
    )
    # The reason is documented where the next person will look.
    assert "autobahn" in runtime.lower()


def test_a_flat_exit_re_enters_on_the_next_confirmed_signal(monkeypatch, tmp_path) -> None:
    """The 15-minute rule: out on the disagreement, back in on the confirmation.

    ``flip`` holds through a disagreeing bar and only closes when the pair turns
    the *other* way, so on a 15-minute series a losing leg is ridden for as long
    as the disagreement lasts. ``flat`` closes on the first disagreeing bar.

    Re-entry is not a second rule. A bar that confirms a direction after a
    NO_SIGNAL bar is a *transition*, so the owed-entry latch fires on it and the
    position is out for exactly as long as the pair disagrees. This test walks
    the whole cycle, because the exit alone is only half the behaviour and the
    half that is easy to get wrong is the way back in.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        # 1. BULL confirms -> entry.
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
        # 2. The pair disagrees -> flat exit under `flat`.
        _verdict(strategy.NO_SIGNAL, strategy.BULL),
        # 3. Still disagreeing -> nothing to do, and nothing to re-enter.
        _verdict(strategy.NO_SIGNAL, strategy.NO_SIGNAL),
        # 4. BULL confirms again -> a fresh transition, so re-entry.
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
    ])
    monkeypatch.setattr(engine, "strategy_params", lambda: {"exit": "flat"})
    eng.run.params = {"exit": "flat"}

    feed.n = 1
    first = eng.evaluate_now()
    assert first["action"] == "BUY", first
    assert len(db.get_open_positions(mode=PAPER)) == 1

    feed.n = 2
    second = eng.evaluate_now()
    assert second["action"] == "FLAT", second
    assert db.get_open_positions(mode=PAPER) == [], "the flat bar must close the leg"

    feed.n = 3
    third = eng.evaluate_now()
    assert third["action"] is None, third
    assert db.get_open_positions(mode=PAPER) == [], "a still-flat bar must not re-enter"

    feed.n = 4
    fourth = eng.evaluate_now()
    assert fourth["action"] == "BUY", fourth
    assert len(db.get_open_positions(mode=PAPER)) == 1, (
        "the confirming bar is a transition and must re-enter"
    )


def test_a_flat_exit_does_not_reverse_on_the_exiting_bar(monkeypatch, tmp_path) -> None:
    """Under ``flat`` the exiting bar carries NO_SIGNAL, so there is no direction.

    ``flip`` reverses on the same bar because the bar that flips the pair *is* a
    fresh transition into the new direction. ``flat`` exits on a disagreement,
    which has no direction to enter - reversing there would open a trade the
    strategy never signalled.
    """
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL),
        _verdict(strategy.NO_SIGNAL, strategy.BULL),
    ])
    monkeypatch.setattr(engine, "strategy_params", lambda: {"exit": "flat"})
    eng.run.params = {"exit": "flat"}

    feed.n = 1
    eng.evaluate_now()
    feed.n = 2
    result = eng.evaluate_now()

    assert result["action"] == "FLAT"
    assert db.get_open_positions(mode=PAPER) == [], (
        "a flat exit must leave the book flat, not reversed"
    )


def test_a_flat_exit_closes_the_index_leg_when_the_futures_leg_is_empty(
    monkeypatch, tmp_path
) -> None:
    """A two-leg book must not be left holding one leg after an exit.

    ``exit_on_signal`` returns early when the *futures* book holds nothing, and
    the caller only settles the index leg inside the ``exit_report is not None``
    branch. So a book whose futures leg never filled - an out-of-session entry, a
    rejected order, a restart between the two legs - kept its index position
    forever: the exit rule fired, the futures book had nothing to close, and the
    index book was never told.

    Observed live: gift-15m held LONG 65 on the index with no futures leg, and
    the flat exit did nothing.
    """
    import gift_book

    _isolate_gift_book(monkeypatch, tmp_path)
    book = gift_book.get_gift_book()
    # The index leg is open; the futures leg is not.
    book.execute("BUY", 25_000.0)
    assert book.net_units() == 65

    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.NO_SIGNAL, strategy.BULL),
    ])
    monkeypatch.setattr(engine, "strategy_params", lambda: {"exit": "flat"})
    eng.run.params = {"exit": "flat"}
    # Put the real settlement back: _fresh_signal_env stubs it out.
    monkeypatch.setattr(
        eng, "_settle_gift_book",
        engine.StrategyEngine._settle_gift_book.__get__(eng),
    )

    feed.n = 1
    result = eng.evaluate_now()

    assert result["action"] == "FLAT", result
    assert book.net_units() == 0, (
        "the index leg was left open after a flat exit with no futures position"
    )


def test_a_lagging_history_refresh_does_not_discard_a_tick_closed_bar() -> None:
    """Kite's history lags the tick feed, and replacing the series lost the bar.

    ``interval_history`` assigned the fetched rows over the whole secondary
    series. Kite's 15-minute history trails the ticks by seconds to minutes, so a
    bar the ticks had already closed was thrown away and an older series put in
    its place. The engine keys off the newest bar's timestamp, so it then saw the
    bar it had already evaluated and skipped - the signal sat unacted-on until
    Kite caught up.

    Observed live: a 15-minute bar closed at 21:00 and the order was placed at
    21:04. Four minutes, on a strategy whose whole point is acting on the close.
    """
    import gift_nifty

    feed = gift_nifty.GiftNiftyFeed.__new__(gift_nifty.GiftNiftyFeed)
    feed._lock = __import__("threading").RLock()
    feed._extra_builders = {"15minute": SimpleNamespace(max_history=2000)}
    feed._extra_history = {"15minute": [
        {"time": "2026-11-19 20:30:00", "open": 1, "high": 2, "low": 1, "close": 2},
        # Closed by the ticks at 21:00. Kite has not published it yet.
        {"time": "2026-11-19 20:45:00", "open": 2, "high": 3, "low": 2, "close": 3},
    ]}

    # Kite's history stops one bar earlier.
    feed.interval_history("15minute", [
        {"time": "2026-11-19 20:30:00", "open": 1, "high": 9, "low": 1, "close": 9},
    ])

    series = feed._extra_history["15minute"]
    times = [row["time"] for row in series]
    assert "2026-11-19 20:45:00" in times, (
        "the tick-closed bar was discarded by a lagging history refresh"
    )
    # Kite still wins where it has a row: its OHLC replaces the tick-built one.
    assert series[0]["close"] == 9
    # And the series stays ordered, because the engine reads the last element.
    assert times == sorted(times)


def test_a_history_refresh_does_not_duplicate_a_bar() -> None:
    """Merging must not append a bar Kite and the ticks both produced."""
    import gift_nifty

    feed = gift_nifty.GiftNiftyFeed.__new__(gift_nifty.GiftNiftyFeed)
    feed._lock = __import__("threading").RLock()
    feed._extra_builders = {"15minute": SimpleNamespace(max_history=2000)}
    feed._extra_history = {"15minute": [
        {"time": "2026-11-19 20:30:00", "open": 1, "high": 2, "low": 1, "close": 2},
    ]}

    feed.interval_history("15minute", [
        {"time": "2026-11-19 20:30:00", "open": 1, "high": 2, "low": 1, "close": 2},
    ])

    assert len(feed._extra_history["15minute"]) == 1


# ---------------------------------------------------------------------------
# Per-book signal identity
#
# Three books write one ledger. A signal id is only unique within the book that
# generated it, and both halves of that were wrong: the id was built from the
# engine's active-run bar, and the duplicate guard scanned every book's rows.
# ---------------------------------------------------------------------------
def test_the_duplicate_guard_is_scoped_to_the_book(monkeypatch, tmp_path) -> None:
    """One book's signal must not suppress another book's order.

    ``_already_processed`` scanned every accepted row regardless of strategy, so
    two books that produced the same signal id - the same action on the same bar,
    which is the normal case for two books on one series - collided. The second
    book's order was rejected as a duplicate of the first's and it silently never
    traded.
    """
    import order_manager

    use_test_database(monkeypatch, tmp_path)
    db.record_signal(
        "BUY", '{"signal_id": "GIFT-BUY-2026-11-19 20:45:00"}', True,
        "opened", strategy_id="gift-15m",
    )

    manager = order_manager.OrderManager.__new__(order_manager.OrderManager)
    shared = "GIFT-BUY-2026-11-19 20:45:00"

    # The book that produced it has processed it.
    assert manager._already_processed(shared, PAPER, "gift-15m") is True
    # A different book has not, and must be allowed to trade.
    assert manager._already_processed(shared, PAPER, "gift-60m") is False
    # Unscoped still means "any book", which is what a caller with no book wants.
    assert manager._already_processed(shared, PAPER) is True


def test_the_signal_id_names_the_book_and_its_own_bar() -> None:
    """The id must be built from the book's bar, not the engine's.

    ``_last_bar_at`` tracks the *active* run only. A 60-minute book's order was
    therefore stamped with the 15-minute book's bar, so the id described a bar
    that book never evaluated - and the duplicate guard compared it against the
    wrong row.
    """
    import inspect

    import engine

    source = inspect.getsource(engine.StrategyEngine.execute)
    assert 'f"GIFT-{action}-{book}-' in source, (
        "the signal id does not name the book"
    )
    assert "run.state.last_bar_at" in source, (
        "the signal id is not built from the book's own bar"
    )
    # The engine-level field must not be the primary source any more.
    assert 'f"GIFT-{action}-{self._last_bar_at}"' not in source


def test_panic_flattens_the_index_books_too(monkeypatch, tmp_path) -> None:
    """An emergency stop must not leave an index position open.

    ``/api/panic`` squares off through the order manager, which owns the
    *futures* ledger. The index book is a separate database, so the operator's
    square-off could not reach it - and an emergency stop that leaves a position
    open is the one state it must never leave behind.
    """
    from fastapi.testclient import TestClient

    import bot as bot_app
    import gift_book
    import order_manager

    use_test_database(monkeypatch, tmp_path)
    stub = SimpleNamespace(
        gift_book=True, gift_book_db_path=str(tmp_path / "gift.db"),
        gift_book_units=65, gift_book_balance=200_000.0,
        tick_size=0.05, slippage_ticks=1.0, lot_size=65, default_lots=1,
    )
    monkeypatch.setattr(gift_book, "settings", stub)
    monkeypatch.setattr(order_manager, "settings", stub)
    gift_book.reset_gift_books()
    try:
        book = gift_book.get_gift_book("gift-15m")
        book.execute("BUY", 25_000.0)
        book.mark(25_050.0)
        assert book.net_units() == 65

        monkeypatch.setattr(bot_app, "LOG_FILE", tmp_path / "bot.log")
        monkeypatch.setattr(
            bot_app, "get_order_manager",
            lambda: SimpleNamespace(
                square_off=lambda *a, **k: SimpleNamespace(
                    ok=True, reason="squared off", to_dict=lambda: {"ok": True},
                ),
                pause=lambda reason: None,
                paused=True,
            ),
        )
        # Patched on `gift_book`, not on `bot`: `api_panic` imports `gift_books`
        # inside the function, so a `bot_app.gift_books` attribute is never read.
        # The old patch therefore did nothing at all, and the test only passed
        # where `.env` configured a strategy whose id happened to match the book.
        monkeypatch.setattr(gift_book, "gift_books", lambda: [book])

        payload = TestClient(bot_app.app, base_url="http://localhost").post(
            "/api/panic", json={"confirm": True, "reason": "PANIC"}
        ).json()

        assert book.net_units() == 0, (
            "PANIC left the index leg open - the emergency stop did not reach it"
        )
        assert "gift-15m" in payload["index_books"]
    finally:
        gift_book.reset_gift_books()


def test_flatten_is_a_no_op_when_the_index_book_is_flat(monkeypatch, tmp_path) -> None:
    """Flattening an empty book must not invent a trade."""
    import gift_book
    import order_manager

    use_test_database(monkeypatch, tmp_path)
    stub = SimpleNamespace(
        gift_book=True, gift_book_db_path=str(tmp_path / "gift.db"),
        gift_book_units=65, gift_book_balance=200_000.0,
        tick_size=0.05, slippage_ticks=1.0, lot_size=65, default_lots=1,
    )
    monkeypatch.setattr(gift_book, "settings", stub)
    monkeypatch.setattr(order_manager, "settings", stub)
    gift_book.reset_gift_books()
    try:
        book = gift_book.get_gift_book("gift-15m")
        assert book.flatten("PANIC") is None
        assert book.net_units() == 0
    finally:
        gift_book.reset_gift_books()


# ---------------------------------------------------------------------------
# Alert delivery and message shape
# ---------------------------------------------------------------------------
def test_watchdog_delivers_on_a_channel_that_has_no_webhook_url(monkeypatch) -> None:
    """Telegram is addressed by token and chat id, so ALERT_WEBHOOK_URL is empty.

    The dispatch gate used to be `if self.webhook_url`, which is the wrong
    question: it asks whether a URL is set, not whether the configured channel
    can deliver. With ALERT_CHANNEL=telegram the URL is deliberately empty, so
    every watchdog alert was logged as ALERT and then dropped - the operator saw
    nothing on the phone while the log claimed the alert had fired.
    """
    import dataclasses
    import notifier
    import watchdog
    from config import settings

    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=True,
        alert_channel="telegram",
        alert_webhook_url="",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="42",
    ))
    monkeypatch.setattr(notifier, "_post_json",
                        lambda url, body, timeout=8.0: sent.append((url, body)) or True)

    wd = watchdog.Watchdog(
        webhook_url="",
        cooldown_seconds=0,
        sender=lambda url, payload: notifier.send(payload),
    )
    alerts = wd.check(
        market_open=True,
        feed={"connected": True, "tick_age_seconds": 400.0},
        engine={},
    )
    assert [a.kind for a in alerts] == ["STALE_FEED"]

    for _ in range(watchdog.FAILURES_BEFORE_ALERT):
        wd.run_once(
            market_open=True,
            feed={"connected": True, "tick_age_seconds": 400.0},
            engine={},
        )

    assert len(sent) == 1, "the alert was logged but never delivered"
    url, body = sent[0]
    assert url.endswith("/sendMessage")
    assert "STALE_FEED" in body["text"]


def test_watchdog_does_not_claim_delivery_when_alerting_is_off(monkeypatch) -> None:
    """A disabled channel must not be counted as a sent alert.

    `alerts_sent` is what the dashboard reports. Counting a message that was
    never delivered makes the operator believe the channel works.
    """
    import dataclasses
    import notifier
    import watchdog
    from config import settings

    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=False,
        alert_channel="telegram",
        alert_webhook_url="",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="42",
    ))

    wd = watchdog.Watchdog(webhook_url="", cooldown_seconds=0)
    for _ in range(watchdog.FAILURES_BEFORE_ALERT):
        wd.run_once(
            market_open=True,
            feed={"connected": True, "tick_age_seconds": 400.0},
            engine={},
        )
    assert wd.state.alerts_delivered == 0
    assert wd.state.alerts_sent == 1, "the watchdog did fire; it just could not deliver"


def test_an_alert_message_carries_its_structured_context() -> None:
    """A message must say which book, which instrument, and what to do.

    "GIFT feed connected but no tick for 240s" does not say which strategy is
    affected or whether it is real money, so the operator had to open the
    dashboard to find out whether it mattered.
    """
    import notifier

    text = notifier.describe({
        "kind": "STALE_FEED",
        "severity": "critical",
        "detail": "The GIFT Nifty feed is connected but has delivered no tick for 240s.",
        "fields": {
            "strategy": "gift-15m",
            "venue": "LIVE",
            "symbol": "NIFTY26NOVFUT",
            "interval": "15minute",
            "last_tick": "2026-11-20T11:54:00+05:30",
            "action_required": "Check the Kite websocket.",
        },
        "at_ist": "2026-11-20T11:58:25+05:30",
    })

    assert text.startswith("\U0001F6A8 STALE_FEED")
    for expected in ("gift-15m", "LIVE", "NIFTY26NOVFUT", "15minute",
                     "Check the Kite websocket."):
        assert expected in text, f"{expected!r} missing from the alert:\n{text}"
    # Timestamps are rendered for a human, not as raw ISO with an offset.
    assert "2026-11-20 11:54:00 IST" in text
    assert "+05:30" not in text


def test_an_alert_without_fields_still_renders() -> None:
    """Older call sites pass only kind/detail/mode; they must not break."""
    import notifier

    text = notifier.describe({
        "kind": "STOP_OUT",
        "detail": "hit the stop",
        "severity": "warning",
        "mode": "PAPER",
        "symbol": "NIFTY26NOVFUT",
    })
    assert "STOP_OUT" in text
    assert "hit the stop" in text
    assert "PAPER" in text
    assert "NIFTY26NOVFUT" in text


def test_a_trade_alert_names_the_book_and_the_order(monkeypatch, tmp_path) -> None:
    """A fill without the strategy and the order id is not actionable.

    "BUY NIFTY26NOVFUT 65 @ 24180.50" does not say which of the three books took
    it, or whether it was real money.
    """
    use_test_database(monkeypatch, tmp_path)
    db.delete_settings(["alert_enabled"])
    import dataclasses
    import engine as engine_mod
    import notifier
    from config import settings

    sent: list[dict] = []
    monkeypatch.setattr(notifier, "settings", dataclasses.replace(
        settings,
        alert_enabled=True,
        alert_channel="telegram",
        alert_telegram_bot_token="TOK",
        alert_telegram_chat_id="42",
    ))
    monkeypatch.setattr(notifier, "_post_json",
                        lambda url, body, timeout=8.0: sent.append(body) or True)

    engine_mod._trade_alert(
        "TRADE_OPENED",
        "BUY NIFTY26NOVFUT 65 @ 24180.50",
        severity="info",
        symbol="NIFTY26NOVFUT",
        fields={
            "strategy": "gift-15m",
            "venue": "LIVE",
            "action": "BUY",
            "quantity": 65,
            "price": "24180.50",
            "order_id": "251120000012345",
        },
    )

    assert len(sent) == 1
    text = sent[0]["text"]
    for expected in ("gift-15m", "LIVE", "251120000012345", "24180.50"):
        assert expected in text, f"{expected!r} missing from the trade alert:\n{text}"


def test_a_second_book_enters_on_its_own_fresh_signal(monkeypatch, tmp_path) -> None:
    """A non-active book must read its own owed-entry latch, not the engine's.

    Observed live: gift-60m logged "Fresh BULL signal on bar 11:30; entry owed"
    and then never entered. The latch was written to the run (correctly), but the
    entry gate read ``self._pending_signal`` - the engine-level mirror, which is
    only kept in step for ``self.run`` (``runs[0]``). For every other book it
    stays None, so the gate always answered "not a new signal" and the strategy
    silently never traded.

    This is the difference between a book that is configured and a book that
    works: the 60-minute strategy looked healthy on the dashboard - ready, bars
    loaded, signal BULL - while being unable to place a single order.
    """
    import bar_source

    _isolate_gift_book(monkeypatch, tmp_path)
    eng, feed, client = _fresh_signal_env(monkeypatch, tmp_path, [
        _verdict(strategy.BULL, strategy.NO_SIGNAL, close=25_000.0),
        _verdict(strategy.BULL, strategy.NO_SIGNAL, close=25_000.0),
    ])

    class OtherSource:
        seconds = 3600

        def refresh_history(self):
            return None

        def candles(self):
            return [{"time": "2026-11-16T09:15:00+05:30", "open": 1, "high": 1,
                     "low": 1, "close": 1}]

        def status(self):
            return {"bars": 1}

    monkeypatch.setattr(feed, "status", lambda: {"bars": feed.n}, raising=False)

    other = engine.StrategyRun("second-book", OtherSource(), params={}, lots=1)
    eng.runs.append(other)

    # The active book consumes the first verdict; the second book gets the second.
    feed.n = 4
    eng.evaluate_run(eng.run)
    result = eng.evaluate_run(other)

    assert result.get("action") == engine.BUY, (
        f"the second book did not enter on its own fresh signal: {result}"
    )


# ---------------------------------------------------------------------------
# Higher-timeframe bars must not be discarded as "forming"
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def _utc_local_time():
    """Run with the process in UTC, which is what the container uses.

    `_is_forming` used `astimezone`, and on a naive datetime that assumes the
    value is in *system local* time. On a developer machine set to IST the bug is
    invisible - the offset happens to match - so a test written without this
    passes against the broken code. The container is UTC, which is where the lag
    was observed.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_a_closed_60_minute_bar_is_not_reported_as_forming() -> None:
    """A bar timestamp is naive IST, and must be labelled, not converted.

    `_is_forming` used `closed_at.astimezone(now.tzinfo)`. `astimezone` on a
    naive datetime assumes the value is in system local time. The container runs
    UTC, so every bar was pushed 5h30m into the future: the 15:15 bar was judged
    to close at 20:45 and popped off the series as "forming".

    The result was a whole-bar lag, not a display error. Live, the 60-minute
    NIFTY book sat on its 14:15 bar from 15:15 onwards - the 15:15, 16:15 and
    17:15 bars were fetched and discarded on every refresh - so a flip that
    closed at 15:15 was never evaluated.
    """
    from bar_source import NiftyBarSource

    source = NiftyBarSource("60minute")
    # 16:20 IST: the 15:15 bucket closed at 16:15, so it is complete.
    now = datetime(2026, 11, 20, 16, 20, tzinfo=db.IST)

    with _utc_local_time():
        assert source._is_forming("2026-11-20 15:15:00", now) is False, (
            "a bar that closed five minutes ago was reported as still forming"
        )
        assert source._is_forming("2026-11-20 14:15:00", now) is False
        # The bucket that opened at 16:15 is genuinely still forming.
        assert source._is_forming("2026-11-20 16:15:00", now) is True


def test_a_closed_15_minute_bar_is_not_reported_as_forming() -> None:
    """The same rule at the shorter timeframe, on a boundary minute."""
    from bar_source import NiftyBarSource

    source = NiftyBarSource("15minute")
    now = datetime(2026, 11, 20, 16, 20, tzinfo=db.IST)

    with _utc_local_time():
        assert source._is_forming("2026-11-20 16:00:00", now) is False
        assert source._is_forming("2026-11-20 16:15:00", now) is True


def test_the_newest_closed_bar_survives_a_refresh(monkeypatch) -> None:
    """The regression, end to end: the bar that just closed must be kept.

    Asserting on `_is_forming` alone would not catch a caller that pops the bar
    for another reason, so this drives `refresh_history` and checks the series.
    """
    import bar_source
    from bar_source import NiftyBarSource

    class _Row(dict):
        pass

    def _row(hour: int, minute: int, close: float) -> _Row:
        return _Row(date=datetime(2026, 11, 20, hour, minute), open=close, high=close,
                    low=close, close=close, volume=0.0)

    rows = [
        _row(13, 15, 25500.0),
        _row(14, 15, 25600.0),
        _row(15, 15, 25700.0),   # closed at 16:15
        _row(16, 15, 25750.0),   # still forming at 16:20
    ]

    class _Client:
        def historical_data(self, **kwargs):
            return rows

    source = NiftyBarSource("60minute", client=_Client())
    frozen = datetime(2026, 11, 20, 16, 20, tzinfo=db.IST)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(bar_source, "datetime", _FixedDatetime)
    with _utc_local_time():
        source.refresh_history(force=True)

        times = [bar["time"] for bar in source.candles()]
    assert "2026-11-20 15:15:00" in times, (
        f"the bar that closed at 16:15 was dropped from the series: {times}"
    )
    assert "2026-11-20 16:15:00" not in times, "the forming bar must not be kept"
