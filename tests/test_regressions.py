from __future__ import annotations

import json
import logging
import sys
from datetime import date, datetime
from types import SimpleNamespace

import config
import bot as bot_app
import database as db
import engine
import gift_nifty
import kite_auth
import kite_client
import order_manager
import pytest
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


def test_pandas_ta_indicators_warm_up_from_real_candles() -> None:
    candles = [
        {
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
        }
        for index in range(14)
    ]

    assert engine.required_bars() == 14
    with pytest.raises(strategy.InsufficientData, match="need 14 closed candles"):
        strategy.evaluate(candles[:13])

    verdict = strategy.evaluate(candles)
    assert set(verdict.conditions) == {
        "buy_supertrend",
        "buy_psar",
        "sell_supertrend",
        "sell_psar",
    }
    assert set(verdict.values) == {
        "psar",
        "supertrend_bull",
        "supertrend_bear",
        "supertrend_total",
        "supertrend_config",
        "st_11_1",
        "st_12_2",
        "st_13_3",
    }
    assert isinstance(verdict.values["psar"], float)
    assert all(
        isinstance(verdict.values[key], float)
        for key in ("st_11_1", "st_12_2", "st_13_3")
    )
    assert verdict.values["supertrend_bull"] == 3
    assert verdict.values["supertrend_bear"] == 0
    assert verdict.values["supertrend_total"] == 3


def test_supertrend_config_is_parsed_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_SUPERTRENDS", "9:1.5, 21:4")
    assert config.supertrend_config() == ((1.5, 9), (4.0, 21))
    assert config.strategy_params()["supertrends"] == ((1.5, 9), (4.0, 21))
    assert config.strategy_summary()["supertrend_label"] == "9×1.5 · 21×4"


def test_supertrend_config_falls_back_when_the_spec_is_unusable(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_SUPERTRENDS", "nonsense, 0:1, 12:-2")
    assert config.supertrend_config() == strategy.DEFAULT_SUPERTRENDS


def test_configured_supertrends_drive_the_vote_and_warmup() -> None:
    candles = [
        {"high": 101.0 + index, "low": 99.0 + index, "close": 100.0 + index}
        for index in range(22)
    ]
    params = {"supertrends": ((1.0, 9), (2.0, 21))}

    assert strategy.required_bars(params["supertrends"]) == 22
    with pytest.raises(strategy.InsufficientData, match="need 22 closed candles"):
        strategy.evaluate(candles[:21], params)

    verdict = strategy.evaluate(candles, params)
    assert verdict.values["supertrend_total"] == 2
    assert verdict.values["supertrend_bull"] == 2
    assert {"st_9_1", "st_21_2"} <= set(verdict.values)
    assert "st_11_1" not in verdict.values
    assert verdict.detail.endswith("ST bull=2/2 bear=0/2")


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

    for factor, length in strategy.SUPERTRENDS:
        line, directions = strategy.supertrend(highs, lows, closes, factor, length)
        expected_line, expected_direction = _reference_supertrend(
            highs, lows, closes, length, factor
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


def test_engine_status_reports_forming_bar_indicators_without_signaling(monkeypatch) -> None:
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
    feed._handle_ticks(None, [{"last_price": 116.0}])
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": True})
    monkeypatch.setattr(
        engine,
        "get_order_manager",
        lambda: SimpleNamespace(current_symbol=lambda: "NIFTY", paused=False),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)

    status = engine.StrategyEngine().status()
    snapshot, _ = feed.candle_snapshot()

    assert status["current_bar_in_progress"]
    assert status["current_values"] is not None
    assert status["current_values"] == strategy.evaluate(snapshot).values
    assert status["current_bar_at"] == snapshot[-1]["time"]
    assert status["previous_values"] == strategy.evaluate(feed.candles()).values
    assert status["previous_bar_at"] == feed.candles()[-1]["time"]
    assert status["previous_values"]["psar"] != status["current_values"]["psar"]
    assert status["last_signal"] == strategy.NEUTRAL


def test_engine_after_hours_paper_order_uses_closed_candle_price(monkeypatch) -> None:
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

        def process_signal(self, payload, source):
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
    assert captured["payload"]["paper_after_hours"] is True
    assert captured["payload"]["price"] == 24_418.5
    assert captured["payload"]["timestamp"] == signal_time.isoformat()


def test_engine_waits_for_a_transition_after_startup(monkeypatch) -> None:
    class FakeFeed:
        seconds = 300

        def __init__(self):
            self.rows = [{"time": "2026-11-05T22:10:00+05:30"}]

        def candles(self):
            return self.rows

    feed = FakeFeed()
    verdicts = iter([
        strategy.StrategyVerdict(strategy.BULL, 100.0),
        strategy.StrategyVerdict(strategy.BULL, 101.0),
        strategy.StrategyVerdict(strategy.BEAR, 99.0),
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
    monkeypatch.setattr(db, "get_open_positions", lambda mode=None: [])
    runner = engine.StrategyEngine()
    monkeypatch.setattr(
        runner,
        "execute",
        lambda action, detail, **kwargs: actions.append(action) or {"ok": True},
    )

    assert runner.evaluate_now()["reason"] == "existing signal primed; waiting for the next transition"
    feed.rows.append({"time": "2026-11-05T22:15:00+05:30"})
    assert runner.evaluate_now()["reason"] == "no signal change"
    feed.rows.append({"time": "2026-11-05T22:20:00+05:30"})
    assert runner.evaluate_now()["action"] == "SELL"
    assert actions == ["SELL"]


def test_engine_logs_closed_gift_candle_and_indicator_values(monkeypatch, caplog) -> None:
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
            strategy.NEUTRAL,
            100.5,
            values={"psar": 98.75, "st_11_1": 99.25},
        ),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)
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
    assert '"st_11_1": 99.25' in message


def test_engine_reverses_opposing_paper_position_on_startup(monkeypatch) -> None:
    class FakeFeed:
        seconds = 300

        def candles(self):
            return [{"time": "2026-11-05T22:50:00+05:30"}]

    feed = FakeFeed()
    verdict = strategy.StrategyVerdict(strategy.BEAR, 95.0)
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
    monkeypatch.setattr(
        db,
        "get_open_positions",
        lambda mode=None: [{
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

    assert result["action"] == "SELL"
    assert captured == [("SELL", 95.0)]


def test_after_hours_paper_engine_pnl_uses_gift_mark(monkeypatch, tmp_path) -> None:
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

    assert portfolio["unrealized_pnl"] == -325.0
    assert portfolio["total_pnl"] == -325.0
    assert portfolio["equity"] == 999_675.0
    assert len(metrics["open_positions"]) == 1
    assert position["mark_price"] == 95.0
    assert position["unrealized_pnl"] == -325.0
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
    assert result.price == 24_418.55
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
    result = manager._execute_directional({}, "BUY", "TEST", PAPER, "test")

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

    result = OrderManager(FakeClient())._execute_directional({}, "BUY", "TEST", PAPER, "test")

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

        def positions(self):
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
        return strategy.StrategyVerdict(strategy.NEUTRAL, 100.5, values={"psar": 99.0})

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
