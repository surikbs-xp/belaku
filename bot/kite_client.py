"""Zerodha Kite Connect wrapper.

In PAPER mode the trading calls are mocked (they return synthetic order ids and
complete statuses) while *market data* still comes from Kite's REST quotes API
when credentials are valid. If quotes are unavailable, the last cached price is
used so the bot keeps marking positions.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Any, Optional

from config import LIVE, PAPER, settings

try:  # The SDK is mandatory for LIVE mode but optional for paper trading.
    from kiteconnect import KiteConnect
    from kiteconnect.exceptions import KiteException, NetworkException, TokenException

    SDK_AVAILABLE = True
except Exception:  # pragma: no cover - environment without the SDK
    KiteConnect = None  # type: ignore[assignment]

    class KiteException(Exception):  # type: ignore[no-redef]
        pass

    class TokenException(KiteException):  # type: ignore[no-redef]
        pass

    class NetworkException(KiteException):  # type: ignore[no-redef]
        pass

    SDK_AVAILABLE = False

log = logging.getLogger("kite_client")

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

# NSE F&O symbols: NIFTY + 2-digit year + month code + "FUT" (e.g. NIFTY26FUTFUT)
_MONTH_CODES = {m: c for m, c in zip(range(1, 13), "ABCDEFGHIJKL")}
EXPIRY_WEEKDAY = 1  # Tuesday: NSE's monthly F&O expiry day since Sep 2025

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)


# ---------------------------------------------------------------------------
# Market clock
# ---------------------------------------------------------------------------
def market_status(now: Optional[datetime] = None) -> dict[str, Any]:
    """NSE F&O session: Mon-Fri 09:15-15:30 IST (exchange holidays are not handled)."""
    now = now or datetime.now(IST)
    open_time = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    is_weekday = now.weekday() < 5
    is_open = is_weekday and open_time <= now <= close_time
    if is_open:
        reason = "Market open"
    elif not is_weekday:
        reason = "Weekend - market closed"
    elif now < open_time:
        reason = "Pre-market - opens 09:15 IST"
    else:
        reason = "Post-market - closed at 15:30 IST"
    return {
        "is_open": is_open,
        "now_ist": now.isoformat(timespec="seconds"),
        "session_open": open_time.isoformat(timespec="seconds"),
        "session_close": close_time.isoformat(timespec="seconds"),
        "reason": reason,
    }


def is_market_open(now: Optional[datetime] = None) -> bool:
    return bool(market_status(now)["is_open"])


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------
def _last_weekday_of_month(year: int, month: int, weekday: int) -> int:
    nxt = datetime(year + (month == 12), (month % 12) + 1, 1)
    day = (nxt - timedelta(days=1)).day
    while datetime(year, month, day).weekday() != weekday:
        day -= 1
    return day


def nearest_monthly_expiry(now: Optional[datetime] = None) -> datetime:
    """Expiry of the contract that is currently front-month.

    The post-expiry cutoff is the 15:30 close of expiry day, so a signal sent
    after the last Tuesday's close already refers to the next month.
    """
    now = now or datetime.now(IST)
    year, month = now.year, now.month
    expiry = datetime(
        year, month, _last_weekday_of_month(year, month, EXPIRY_WEEKDAY), 15, 30, tzinfo=IST
    )
    if expiry < now:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        expiry = datetime(
            year, month, _last_weekday_of_month(year, month, EXPIRY_WEEKDAY), 15, 30, tzinfo=IST
        )
    return expiry


def _expiry_after(expiry: datetime) -> datetime:
    year, month = (expiry.year + 1, 1) if expiry.month == 12 else (expiry.year, expiry.month + 1)
    return datetime(year, month, _last_weekday_of_month(year, month, EXPIRY_WEEKDAY), 15, 30, tzinfo=IST)


def future_symbol(now: Optional[datetime] = None, months_ahead: int = 0) -> str:
    """Trading symbol for the Nifty futures contract (0 = current month)."""
    expiry = nearest_monthly_expiry(now)
    for _ in range(max(0, months_ahead)):
        expiry = _expiry_after(expiry)
    return f"NIFTY{expiry:%y}{_MONTH_CODES[expiry.month]}FUT"


# ---------------------------------------------------------------------------
# Client wrapper
# ---------------------------------------------------------------------------
@dataclass
class KiteClient:
    """Thin, mode-aware wrapper around ``KiteConnect``."""

    mode: str = PAPER
    api_key: str = field(default_factory=lambda: settings.api_key)
    api_secret: str = field(default_factory=lambda: settings.api_secret)
    access_token: str = field(
        default_factory=lambda: os.getenv("KITE_ACCESS_TOKEN", settings.access_token).strip()
    )
    timeout: float = field(default_factory=lambda: settings.http_timeout_seconds)

    def __post_init__(self) -> None:
        self._kite: Optional[Any] = None
        self._lock = threading.Lock()
        self._instruments: list[dict[str, Any]] = []
        self._instruments_fetched_at = 0.0
        self._last_prices: dict[str, float] = {}
        self._last_quote_at: Optional[datetime] = None
        self._auth_error: Optional[str] = None
        self._init_sdk()

    # -- SDK bootstrap ---------------------------------------------------
    def _init_sdk(self) -> None:
        self._kite = None
        self._auth_error = None
        if not SDK_AVAILABLE:
            self._auth_error = "kiteconnect SDK not installed"
            return
        if not (self.api_key and self.access_token):
            self._auth_error = "Missing KITE_API_KEY / KITE_ACCESS_TOKEN"
            return
        try:
            kite = KiteConnect(api_key=self.api_key, timeout=self.timeout)
            kite.set_access_token(self.access_token)
            self._kite = kite
        except Exception as exc:  # pragma: no cover - defensive
            self._auth_error = f"KiteConnect init failed: {exc}"

    @property
    def kite(self) -> Any:
        if self._kite is None:
            raise RuntimeError(self._auth_error or "KiteConnect is not initialised")
        return self._kite

    @property
    def is_paper(self) -> bool:
        return self.mode.upper() != LIVE

    @property
    def sdk_ready(self) -> bool:
        return self._kite is not None

    # -- Auth / profile --------------------------------------------------
    def login_url(self) -> Optional[str]:
        return self.kite.login_url() if self.sdk_ready else None

    def profile(self) -> dict[str, Any]:
        """Return the live Kite profile, or a synthetic one when unauthenticated."""
        if self.sdk_ready:
            try:
                data = self.kite.profile()
                return {"ok": True, "mode": self.mode, **data}
            except TokenException as exc:
                self._auth_error = f"Access token invalid/expired: {exc}"
            except (KiteException, NetworkException) as exc:
                self._auth_error = f"Kite API error: {exc}"
            except Exception as exc:
                self._auth_error = f"Unexpected auth error: {exc}"
        return {
            "ok": False,
            "mode": self.mode,
            "user_id": "PAPER",
            "user_name": "Paper Trader",
            "error": self._auth_error,
        }

    def auth_status(self) -> dict[str, Any]:
        if not self.sdk_ready:
            return {"authenticated": False, "detail": self._auth_error or "Not authenticated"}
        profile = self.profile()
        if profile.get("ok"):
            return {"authenticated": True, "detail": profile.get("user_id", "")}
        return {"authenticated": False, "detail": profile.get("error", "Unknown error")}

    # -- Instruments -----------------------------------------------------
    def _load_instruments(self) -> list[dict[str, Any]]:
        if not self.sdk_ready:
            return []
        with self._lock:
            stale = time.time() - self._instruments_fetched_at > settings.instrument_refresh_seconds
            if self._instruments and not stale:
                return self._instruments
            try:
                instruments = self.kite.instruments("NFO")
            except Exception as exc:
                log.warning("Instrument dump failed: %s", exc)
                return self._instruments
            self._instruments = [i for i in instruments if i.get("name") == "NIFTY"]
            self._instruments_fetched_at = time.time()
            return self._instruments

    def resolve_instrument_token(self, symbol: str) -> Optional[int]:
        for inst in self._load_instruments():
            if inst.get("tradingsymbol") == symbol:
                return int(inst["instrument_token"])
        return None

    def instrument_for_symbol(self, symbol: str) -> Optional[dict[str, Any]]:
        return next(
            (inst for inst in self._load_instruments() if inst.get("tradingsymbol") == symbol),
            None,
        )

    def nifty_future_instruments(self) -> list[dict[str, Any]]:
        """Return all NIFTY futures in Kite's NFO instrument dump."""
        return [
            inst for inst in self._load_instruments()
            if inst.get("instrument_type") == "FUT"
        ]

    def nifty_future_contracts(self, now: Optional[datetime] = None) -> list[dict[str, Any]]:
        """Return unexpired NIFTY futures in expiry order from Kite's instrument dump."""
        now = now or datetime.now(IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        contracts: list[dict[str, Any]] = []
        for inst in self.nifty_future_instruments():
            raw_expiry = inst.get("expiry")
            if isinstance(raw_expiry, datetime):
                expiry = raw_expiry.date()
            elif isinstance(raw_expiry, date):
                expiry = raw_expiry
            else:
                try:
                    expiry = date.fromisoformat(str(raw_expiry)[:10])
                except ValueError:
                    continue
            if expiry < now.date() or (
                expiry == now.date()
                and now.time().replace(tzinfo=None) > datetime_time(*MARKET_CLOSE)
            ):
                continue
            contracts.append({**inst, "_expiry_date": expiry})
        return sorted(contracts, key=lambda inst: inst["_expiry_date"])

    def front_month_contract(
        self, now: Optional[datetime] = None, months_ahead: int = 0
    ) -> Optional[dict[str, Any]]:
        contracts = self.nifty_future_contracts(now)
        if 0 <= months_ahead < len(contracts):
            return contracts[months_ahead]
        return None

    # -- Market data -----------------------------------------------------
    def get_quote(self, symbol: str) -> Optional[dict[str, Any]]:
        """Live LTP/OHLC for ``symbol``; ``None`` when unavailable."""
        if not self.sdk_ready:
            return None
        key = f"NFO:{symbol}"
        data: dict[str, Any] = {}
        for attempt in range(3):
            try:
                data = self.kite.quote([key])
                break
            except TokenException as exc:
                self._auth_error = f"Access token invalid/expired: {exc}"
                return None
            except (KiteException, NetworkException) as exc:
                self._auth_error = str(exc)
                if attempt == 2:
                    log.warning("Quote failed for %s: %s", symbol, exc)
                    return None
                time.sleep(0.2 * (attempt + 1))
        payload = data.get(key)
        if not payload or payload.get("last_price") is None:
            return None
        price = float(payload["last_price"])
        self._last_prices[symbol] = price
        self._last_quote_at = datetime.now(IST)
        ohlc = payload.get("ohlc", {}) or {}
        return {
            "symbol": symbol,
            "last_price": price,
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "close": ohlc.get("close"),
            "volume": payload.get("volume"),
            "timestamp": payload.get("last_trade_time") or payload.get("timestamp"),
            "source": "KITE",
        }

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str = "5minute",
    ) -> list[dict[str, Any]]:
        """Historical OHLC bars for ``instrument_token``; empty when unavailable.

        Kite *does* serve NSEIX (GIFT Nifty) history, including the overnight
        session, so this is the authoritative candle source for the strategy.
        """
        if not self.sdk_ready:
            return []
        for attempt in range(3):
            try:
                rows = self.kite.historical_data(
                    instrument_token=instrument_token,
                    from_date=from_date,
                    to_date=to_date,
                    interval=interval,
                )
                return list(rows or [])
            except TokenException as exc:
                self._auth_error = f"Access token invalid/expired: {exc}"
                return []
            except (KiteException, NetworkException) as exc:
                self._auth_error = str(exc)
                if attempt == 2:
                    log.warning("Historical data failed for %s: %s", instrument_token, exc)
                    return []
                time.sleep(0.2 * (attempt + 1))
        return []

    def get_ltp(self, symbol: str) -> Optional[float]:
        quote = self.get_quote(symbol)
        if quote:
            return quote["last_price"]
        return self._last_prices.get(symbol)

    def latest_known_price(self, symbol: str) -> Optional[float]:
        return self._last_prices.get(symbol)

    def remember_tick(self, symbol: str, price: float) -> None:
        """Cache the last known price for ``symbol``."""
        self._last_prices[symbol] = float(price)

    @property
    def last_quote_at(self) -> Optional[datetime]:
        return self._last_quote_at

    def data_source(self) -> str:
        return "KITE" if self._last_quote_at is not None else "CACHE"

    # -- Trading ---------------------------------------------------------
    def margins(self) -> Optional[dict[str, Any]]:
        if not self.sdk_ready:
            return None
        try:
            return self.kite.margins()
        except Exception as exc:
            log.warning("Margins lookup failed: %s", exc)
            return None

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        price: Optional[float] = None,
        order_type: str = "MARKET",
        product: str = "NRML",
        tag: str = "niftybot",
    ) -> dict[str, Any]:
        """Place a real order (LIVE) or return a mocked acknowledgement (PAPER)."""
        if self.is_paper:
            order_id = f"PAPER-{datetime.now(IST):%Y%m%d%H%M%S}-{random.randint(1000, 9999)}"
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "average_price": price,
                "mode": PAPER,
                "mocked": True,
                "message": "Simulated order (paper mode)",
            }
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=symbol,
                transaction_type=getattr(self.kite, f"TRANSACTION_TYPE_{transaction_type.upper()}"),
                quantity=int(quantity),
                product=getattr(self.kite, f"PRODUCT_{product.upper()}"),
                order_type=getattr(self.kite, f"ORDER_TYPE_{order_type.upper()}"),
                price=price if order_type.upper() == "LIMIT" else None,
                validity=self.kite.VALIDITY_DAY,
                tag=tag[:20],
            )
        except NetworkException as exc:
            log.error("Order submission outcome is uncertain for %s: %s", symbol, exc)
            return {
                "order_id": None,
                "status": "PENDING",
                "average_price": None,
                "filled_quantity": 0,
                "mode": LIVE,
                "mocked": False,
                "message": f"Order submission outcome is uncertain: {exc}",
            }
        except Exception as exc:
            log.error("Order rejected for %s: %s", symbol, exc)
            return {
                "order_id": None,
                "status": "REJECTED",
                "average_price": None,
                "mode": LIVE,
                "mocked": False,
                "message": str(exc),
            }

        filled = self._wait_for_fill(order_id)
        filled["mode"] = LIVE
        filled["mocked"] = False
        return filled

    def _wait_for_fill(self, order_id: str, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        last: dict[str, Any] = {
            "order_id": order_id,
            "status": "OPEN",
            "average_price": None,
            "filled_quantity": 0,
            "message": "Order accepted",
        }

        def update_history() -> bool:
            nonlocal last
            try:
                history = self.kite.order_history(order_id)
            except Exception as exc:
                last["message"] = f"Fill check failed: {exc}"
                return False
            if history:
                latest = history[-1]
                last["status"] = str(latest.get("status", last["status"])).upper().replace(" ", "_")
                if latest.get("average_price"):
                    last["average_price"] = float(latest["average_price"])
                last["filled_quantity"] = int(latest.get("filled_quantity") or 0)
                last["message"] = latest.get("status_message", last["message"])
                if last["status"] in {"COMPLETE", "REJECTED", "CANCELLED"}:
                    return True
            return False

        while time.time() < deadline:
            if update_history():
                return last
            time.sleep(0.5)
        if last["status"] in {"OPEN", "TRIGGER_PENDING"}:
            cancel_error: Optional[Exception] = None
            try:
                self.kite.cancel_order(
                    variety=self.kite.VARIETY_REGULAR,
                    order_id=order_id,
                )
            except Exception as exc:
                cancel_error = exc
            settle_deadline = time.time() + 3.0
            while time.time() < settle_deadline:
                if update_history():
                    return last
                time.sleep(0.25)
            last["status"] = "PENDING"
            last["message"] = (
                f"Order remains unresolved after cancellation: {cancel_error}"
                if cancel_error
                else "Order status is unresolved after cancellation"
            )
        return last

    def positions(self) -> list[dict[str, Any]]:
        if self.is_paper:
            return []
        if not self.sdk_ready:
            raise RuntimeError(self._auth_error or "KiteConnect is not initialised")
        return self.kite.positions().get("net", [])

    def available_cash(self) -> Optional[float]:
        data = self.margins()
        if not data:
            return None
        equity = data.get("equity", {}) or {}
        return (equity.get("available") or {}).get("live_balance")


_default_client: Optional[KiteClient] = None
_default_lock = threading.Lock()


def get_client() -> KiteClient:
    """Shared client for this process (tick feed, engine, API, dashboard)."""
    global _default_client
    with _default_lock:
        if _default_client is None:
            _default_client = KiteClient(mode=settings.execution_mode)
        return _default_client


def reset_client() -> KiteClient:
    global _default_client
    with _default_lock:
        _default_client = KiteClient(mode=settings.execution_mode)
        return _default_client


__all__ = [
    "KiteClient",
    "SDK_AVAILABLE",
    "get_client",
    "reset_client",
    "is_market_open",
    "market_status",
    "future_symbol",
    "nearest_monthly_expiry",
    "IST",
]
