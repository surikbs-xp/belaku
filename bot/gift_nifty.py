"""GIFT Nifty market data: instrument resolution, tick subscription, candle building.

GIFT Nifty (NSEIX, NSE IFSC) is the pre-market proxy for Nifty 50 and trades
~20 hours a day. It is **quote-only** on Kite:

* ``kite.quote("NSEIX:GIFT NIFTY")`` works (index token 291849);
* ``kite.historical_data()`` does **not** serve NSEIX, so there is no history to
  warm the indicators from.

Consequence, stated plainly: this module must build its own candles forward from
live ticks. Until ``strategy`` has enough bars (12 for SuperTrend warmup), it
refuses to emit signals rather than trading on cold indicators. Bars are
persisted to SQLite so a restart resumes the warmup instead of starting over.

Only ticks during a session advance the candle. The 20-hour GIFT session and the
09:15-15:30 NSE F&O session are different windows, and an out-of-hours tick must
not be glued onto the last in-hours bar - that would corrupt the OHLC.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import database as db
from config import settings
from kite_client import IST, get_client

log = logging.getLogger("gift_nifty")

# NSEIX index symbol for GIFT Nifty. Kite exposes it as a non-tradeable index.
GIFT_EXCHANGE = "NSEIX"
GIFT_SYMBOL = "GIFT NIFTY"
GIFT_KEY = f"{GIFT_EXCHANGE}:{GIFT_SYMBOL}"

# Fallback token if the instrument dump is unavailable (observed stable since
# the 2024-04 fix for the mis-segregated NSEIX row).
GIFT_FALLBACK_TOKEN = 291849
VOLUME_BASIS = "tick_count_proxy"


@dataclass
class Candle:
    """One GIFT Nifty bar; volume is the feed's tick-count proxy."""

    start: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    ticks: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(timespec="seconds"),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    def to_strategy_candle(self) -> dict[str, Any]:
        """Shape expected by :func:`strategy.evaluate`."""
        return {
            "time": self.start.isoformat(timespec="seconds"),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "ticks": self.ticks,
        }


def interval_to_seconds(interval: str) -> int:
    """Map a Kite-style interval string to seconds (``5minute`` -> 300)."""
    text = (interval or "").strip().lower().replace(" ", "")
    units = {
        "minute": 60,
        "minutes": 60,
        "min": 60,
        "hour": 3600,
        "hours": 3600,
        "day": 86400,
    }
    for unit, factor in units.items():
        if text.endswith(unit):
            head = text[: -len(unit)]
            try:
                return max(60, int(head or "1") * factor)
            except ValueError:
                break
    try:
        return max(60, int(text) * 60)
    except ValueError:
        return 300


def bucket_start(moment: datetime, seconds: int) -> datetime:
    """Floor ``moment`` to the start of its candle bucket."""
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=IST)


@dataclass
class CandleBuilder:
    """Aggregates ticks into fixed-interval OHLCV bars.

    ``on_close`` fires with a finished bar. A bar closes when a tick arrives in
    a later bucket; the final bar of a session is flushed by :meth:`flush`.
    """

    seconds: int = 300
    on_close: Optional[Callable[[Candle], None]] = None
    current: Optional[Candle] = None
    closed: list[Candle] = field(default_factory=list)
    max_history: int = 2000

    def add_tick(self, price: float, moment: Optional[datetime] = None, volume: float = 0.0) -> Optional[Candle]:
        """Feed one tick. Returns the bar that closed, if any."""
        if price is None or price <= 0:
            return None
        moment = moment or datetime.now(IST)
        start = bucket_start(moment, self.seconds)

        if self.current is None:
            self.current = Candle(start=start, open=price, high=price, low=price, close=price,
                                  volume=volume, ticks=1)
            return None

        if start > self.current.start:
            finished = self.current
            self._store(finished)
            self.current = Candle(start=start, open=price, high=price, low=price, close=price,
                                  volume=volume, ticks=1)
            if self.on_close:
                self.on_close(finished)
            return finished

        if start < self.current.start:
            # Late tick for an already-closed bucket: ignore rather than
            # corrupting the current bar.
            return None

        bar = self.current
        bar.high = max(bar.high, price)
        bar.low = min(bar.low, price)
        bar.close = price
        bar.volume += volume
        bar.ticks += 1
        return None

    def flush(self) -> Optional[Candle]:
        """Close the in-progress bar (session end / shutdown)."""
        if self.current is None:
            return None
        finished = self.current
        self._store(finished)
        self.current = None
        if self.on_close:
            self.on_close(finished)
        return finished

    def _store(self, candle: Candle) -> None:
        self.closed.append(candle)
        if len(self.closed) > self.max_history:
            del self.closed[:-self.max_history]


class GiftNiftyFeed:
    """Owns the GIFT Nifty tick subscription and the candle series.

    Thread-safe: ticks arrive on the KiteTicker websocket thread while the
    strategy engine reads the candle list from the engine thread.
    """

    def __init__(self, interval: Optional[str] = None) -> None:
        self.interval = interval or settings.gift_candle_interval
        self.seconds = interval_to_seconds(self.interval)
        self._lock = threading.RLock()
        self._builder = CandleBuilder(seconds=self.seconds)
        self._ticker: Any = None
        self._thread: Any = None
        self._stop = threading.Event()
        self.token: Optional[int] = None
        self.connected = False
        self.last_tick_at: Optional[datetime] = None
        self.last_price: Optional[float] = None
        self.tick_count = 0
        self.error: Optional[str] = None
        self.session_ticks = 0
        self._history: list[dict[str, Any]] = []
        self._resume_candle: Optional[Candle] = None
        self._history_loaded = False
        self._loaded_count = 0

    # -- persistence -----------------------------------------------------
    def load_persisted(self) -> int:
        """Seed the series from SQLite so warmup survives a restart."""
        with self._lock:
            if self._history_loaded:
                return self._loaded_count
        rows = db.get_gift_candles(limit=self._builder.max_history)
        current_start = bucket_start(datetime.now(IST), self.seconds)
        resume_candle: Optional[Candle] = None
        if rows and datetime.fromisoformat(rows[-1]["start"]) == current_start:
            row = rows.pop()
            resume_candle = Candle(
                start=current_start,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                ticks=int(row["ticks"]),
            )
        with self._lock:
            if self._history_loaded:
                return self._loaded_count
            self._history = [
                {
                    "time": r["start"],
                    "open": r["open"],
                    "high": r["high"],
                    "low": r["low"],
                    "close": r["close"],
                    "volume": r["volume"],
                }
                for r in rows
            ]
            self._resume_candle = resume_candle
            self._history_loaded = True
            self._loaded_count = len(rows) + (1 if resume_candle else 0)
        if self._loaded_count:
            log.info(
                "Loaded %d persisted GIFT Nifty candles (warmup carry-over)",
                self._loaded_count,
            )
        return self._loaded_count

    def _persist(self, candle: Candle) -> None:
        try:
            db.record_gift_candle(
                start=candle.start.isoformat(timespec="seconds"),
                open_=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                ticks=candle.ticks,
            )
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("Failed to persist GIFT candle: %s", exc)

    # -- candles ---------------------------------------------------------
    def _on_bar_close(self, candle: Candle) -> None:
        self._persist(candle)
        with self._lock:
            self._history.append(candle.to_strategy_candle())
            if len(self._history) > self._builder.max_history:
                del self._history[:-self._builder.max_history]
        log.info(
            "GIFT bar closed %s O=%.2f H=%.2f L=%.2f C=%.2f ticks=%d",
            candle.start.strftime("%H:%M"), candle.open, candle.high, candle.low,
            candle.close, candle.ticks,
        )

    def candles(self) -> list[dict[str, Any]]:
        """Chronological closed candles (oldest first)."""
        with self._lock:
            return list(self._history)

    def candle_snapshot(self) -> tuple[list[dict[str, Any]], bool]:
        """Return closed candles plus the current candle, if one is forming."""
        with self._lock:
            candles = list(self._history)
            current = self._builder.current
            if current is not None:
                candles.append(current.to_strategy_candle())
            return candles, current is not None

    def bar_count(self) -> int:
        with self._lock:
            return len(self._history)

    @property
    def in_progress(self) -> Optional[dict[str, Any]]:
        with self._lock:
            bar = self._builder.current
            return bar.to_strategy_candle() if bar else None

    # -- ticks -----------------------------------------------------------
    def _handle_ticks(self, _ws: Any, ticks: list[dict[str, Any]]) -> None:
        with self._lock:
            self.connected = True
            for tick in ticks:
                price = tick.get("last_price")
                if price is None:
                    continue
                self.last_price = float(price)
                self.last_tick_at = datetime.now(IST)
                self.error = None
                self.tick_count += 1
                self.session_ticks += 1
                self._builder.on_close = self._on_bar_close
                if self._resume_candle is not None:
                    tick_start = bucket_start(self.last_tick_at, self.seconds)
                    if tick_start == self._resume_candle.start:
                        self._builder.current = self._resume_candle
                        self._resume_candle = None
                    elif tick_start > self._resume_candle.start:
                        self._history.append(self._resume_candle.to_strategy_candle())
                        if len(self._history) > self._builder.max_history:
                            del self._history[:-self._builder.max_history]
                        self._resume_candle = None
                self._builder.add_tick(float(price), self.last_tick_at, volume=1.0)

    def _handle_connect(self, ws: Any, _response: Any) -> None:
        """Subscribe the GIFT Nifty index once the socket is up.

        The mode constant lives on the KiteTicker class, not on the instance
        passed to this callback, so read it from the class.
        """
        log.info("GIFT Nifty ticker connected; subscribing token %s", self.token)
        self.connected = False
        if self.token is None:
            self.error = "connected without a resolved instrument token"
            log.error("%s", self.error)
            return
        try:
            mode = getattr(ws, "MODE_LTP", None) or getattr(type(ws), "MODE_LTP", None) or "ltp"
            ws.subscribe([self.token])
            ws.set_mode(mode, [self.token])
            self.connected = True
            self.error = None
        except Exception as exc:
            self.connected = False
            self.error = f"subscribe failed: {exc}"
            log.error("GIFT Nifty subscribe failed: %s", exc)

    def _handle_close(self, _ws: Any, code: Any, reason: Any) -> None:
        self.connected = False
        self.error = f"websocket closed ({code}): {reason}"
        log.warning("GIFT Nifty ticker closed (%s): %s", code, reason)

    def _handle_error(self, _ws: Any, code: Any, reason: Any) -> None:
        self.connected = False
        self.error = f"{code}: {reason}"
        log.error("GIFT Nifty ticker error %s: %s", code, reason)

    def _instrument_dump(self) -> Optional[list[dict[str, Any]]]:
        """Fetch the NSEIX instrument list, authenticated or not."""
        client = get_client()
        kite = None
        if client.sdk_ready:
            kite = client.kite
        else:
            # Public endpoint: an API key alone is enough to list instruments.
            try:
                from kite_client import KiteConnect, SDK_AVAILABLE
            except Exception:  # pragma: no cover
                SDK_AVAILABLE = False
            if not SDK_AVAILABLE or not settings.api_key:
                self.error = "KITE_API_KEY is required to resolve the GIFT Nifty token"
                return None
            try:
                kite = KiteConnect(api_key=settings.api_key)
            except Exception as exc:
                self.error = f"could not build KiteConnect: {exc}"
                return None
        try:
            return list(kite.instruments(GIFT_EXCHANGE))
        except Exception as exc:
            self.error = f"instrument dump failed: {exc}"
            log.warning("NSEIX instrument dump failed: %s", exc)
            return None

    def resolve_token(self) -> Optional[int]:
        """Look up the NSEIX GIFT NIFTY instrument token.

        ``instruments()`` is a public endpoint: it needs only the API key, not
        an access token. Resolving through the shared client would fail before
        login, which is precisely when the token is needed - so the SDK is
        built here when the client is not yet authenticated.
        """
        instruments = self._instrument_dump()
        if instruments is None:
            return None
        for inst in instruments:
            symbol = str(inst.get("tradingsymbol") or "").upper()
            name = str(inst.get("name") or "").upper()
            if symbol in {"GIFT NIFTY", "GIFTNIFTY"} or name == "GIFT NIFTY":
                self.token = int(inst["instrument_token"])
                log.info("Resolved %s -> token %s", GIFT_KEY, self.token)
                return self.token
        self.error = f"{GIFT_KEY} not found in the NSEIX instrument dump"
        log.warning("%s", self.error)
        return None

    def start(self) -> bool:
        """Start the websocket feed. Returns False when it cannot start."""
        if self._thread is not None and self._thread.is_alive():
            return True
        client = get_client()
        if not (client.sdk_ready and client.access_token):
            self.error = "GIFT Nifty feed needs KITE_API_KEY and KITE_ACCESS_TOKEN"
            log.warning("%s", self.error)
            return False
        self.error = None
        self.connected = False
        if self.token is None and self.resolve_token() is None:
            # Fall back to the known index token so a dump outage is not fatal.
            self.token = GIFT_FALLBACK_TOKEN
            log.warning("Using fallback GIFT Nifty token %s", self.token)

        try:
            from kiteconnect import KiteTicker
        except Exception as exc:  # pragma: no cover
            self.error = f"kiteconnect ticker unavailable: {exc}"
            return False

        self._stop.clear()
        try:
            ticker = KiteTicker(
                api_key=client.api_key,
                access_token=client.access_token,
                reconnect=True,
            )
            ticker.on_ticks = self._handle_ticks
            ticker.on_connect = self._handle_connect
            ticker.on_close = self._handle_close
            ticker.on_error = self._handle_error
            self._ticker = ticker

            def _run() -> None:
                try:
                    ticker.connect(threaded=False)
                except Exception as exc:  # pragma: no cover - network
                    self.error = f"ticker thread died: {exc}"
                    self.connected = False
                    log.exception("GIFT Nifty ticker thread died")

            self._thread = threading.Thread(target=_run, name="gift-ticks", daemon=True)
            self._thread.start()
        except Exception as exc:
            self._ticker = None
            self._thread = None
            self.connected = False
            self.error = f"could not start GIFT Nifty ticker: {exc}"
            log.exception("Could not start GIFT Nifty ticker")
            return False
        log.info("GIFT Nifty feed starting (%s, %ss bars)", GIFT_KEY, self.seconds)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            if self._ticker is not None:
                self._ticker.close()
        except Exception:
            pass
        with self._lock:
            self._builder.on_close = self._on_bar_close
            self._builder.flush()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.connected = False
        log.info("GIFT Nifty feed stopped")

    def status(self) -> dict[str, Any]:
        with self._lock:
            age = (
                (datetime.now(IST) - self.last_tick_at).total_seconds()
                if self.last_tick_at
                else None
            )
            latest_candle = None
            if self._builder.current is not None:
                latest_candle = self._builder.current.to_strategy_candle()
            elif self._history:
                latest_candle = self._history[-1]
            return {
                "symbol": GIFT_KEY,
                "token": self.token,
                "connected": self.connected,
                "running": bool(self._thread and self._thread.is_alive()),
                "starting": bool(self._thread and self._thread.is_alive() and not self.connected),
                "interval": self.interval,
                "interval_seconds": self.seconds,
                "volume_basis": VOLUME_BASIS,
                "bars": len(self._history),
                "in_progress": self._builder.current is not None,
                "last_price": self.last_price,
                "last_tick_at": self.last_tick_at.isoformat(timespec="seconds")
                if self.last_tick_at
                else None,
                "last_candle_close": latest_candle["close"] if latest_candle else None,
                "last_candle_at": latest_candle["time"] if latest_candle else None,
                "tick_age_seconds": round(age, 1) if age is not None else None,
                "ticks": self.tick_count,
                "error": self.error,
            }


_feed: Optional[GiftNiftyFeed] = None
_feed_lock = threading.Lock()


def get_feed() -> GiftNiftyFeed:
    """Shared GIFT Nifty feed for this process."""
    global _feed
    with _feed_lock:
        if _feed is None:
            _feed = GiftNiftyFeed()
        return _feed


def reset_feed() -> GiftNiftyFeed:
    global _feed
    with _feed_lock:
        if _feed is not None:
            _feed.stop()
        _feed = GiftNiftyFeed()
        return _feed


__all__ = [
    "Candle",
    "CandleBuilder",
    "GiftNiftyFeed",
    "GIFT_EXCHANGE",
    "GIFT_KEY",
    "GIFT_SYMBOL",
    "bucket_start",
    "get_feed",
    "interval_to_seconds",
    "reset_feed",
]
