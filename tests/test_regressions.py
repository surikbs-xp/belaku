from __future__ import annotations

import sys
from datetime import date, datetime
from types import SimpleNamespace

import config
import bot as bot_app
import database as db
import engine
import gift_nifty
import kite_auth
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


def test_atr_matches_pine_seeding() -> None:
    assert strategy.atr(
        [10.0, 12.0, 14.0],
        [8.0, 9.0, 11.0],
        [9.0, 11.0, 13.0],
        2,
    ) == [None, 2.5, 2.75]


def test_psar_initializes_direction_from_first_close_change() -> None:
    highs = [10.0, 9.0, 8.0]
    lows = [8.0, 7.0, 6.0]
    closes = [9.0, 8.0, 7.0]

    assert strategy.psar(highs, lows, closes)[1] == 10.0


def test_supertrend_starts_bearish_when_first_atr_becomes_available() -> None:
    highs = [10.0, 20.0]
    lows = [9.0, 10.0]
    closes = [9.5, 20.0]

    line, direction = strategy.supertrend(highs, lows, closes, factor=0.1, length=2)

    assert direction == [0, 1]
    assert line[1] == 15.575


def test_strategy_signals_after_supertrend_history_without_rsi_or_tsv() -> None:
    candles = [
        {
            "open": 100.0 + index,
            "high": 101.0 + index,
            "low": 99.0 + index,
            "close": 100.0 + index,
            "volume": 100.0,
        }
        for index in range(20)
    ]

    assert engine.required_bars() == 12
    with pytest.raises(strategy.InsufficientData, match="need 12 closed candles"):
        strategy.evaluate(candles[:11])

    verdict = strategy.evaluate(candles[:12])
    assert set(verdict.conditions) == {"buy_supertrend", "buy_psar", "sell_supertrend", "sell_psar"}
    assert set(verdict.values) == {
        "psar",
        "supertrend_bull",
        "supertrend_bear",
        "st_10_1",
        "st_11_2",
        "st_12_3",
    }


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
        for index in range(11)
    ]
    feed._handle_ticks(None, [{"last_price": 112.0}])
    monkeypatch.setattr(engine, "get_feed", lambda: feed)
    monkeypatch.setattr(engine, "market_status", lambda: {"is_open": True})
    monkeypatch.setattr(
        engine,
        "get_order_manager",
        lambda: SimpleNamespace(current_symbol=lambda: "NIFTY", paused=False),
    )
    monkeypatch.setattr(engine, "get_execution_mode", lambda: PAPER)

    status = engine.StrategyEngine().status()

    assert status["current_bar_in_progress"]
    assert status["current_values"] is not None
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


def test_after_hours_paper_engine_pnl_uses_gift_mark(monkeypatch, tmp_path) -> None:
    use_test_database(monkeypatch, tmp_path)
    db.open_position(
        "NIFTY26NOVFUT", 65, 100.0, mode=PAPER, source="ENGINE"
    )

    class FakeClient:
        mode = PAPER

        def get_ltp(self, _symbol):
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
    position = bot_app._enrich_positions(db.get_open_positions())[0]

    assert portfolio["unrealized_pnl"] == -325.0
    assert portfolio["total_pnl"] == -325.0
    assert portfolio["equity"] == 999_675.0
    assert position["mark_price"] == 95.0
    assert position["unrealized_pnl"] == -325.0


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

        def get_ltp(self, _symbol):
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

        def get_ltp(self, symbol):
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

        def get_ltp(self, symbol):
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
