"""Where closed bars come from, independent of which strategy reads them.

The engine used to assume exactly one answer to "what is the signal series": the
GIFT Nifty feed. That feed is gone, so the assumption is made explicit here as a
small protocol a source satisfies.

Keeping the source behind one interface is what lets :class:`strategy.StrategyVerdict`
and the engine's decision logic stay unchanged: every source yields the same
``{time, open, high, low, close}`` dicts, oldest first, newest bar last.

:class:`NiftyBarSource` reads Kite history only - there is no tick subscription
for the index. It therefore has no forming bar and no live path, which is
exactly right for an NSE-hours strategy: a bar appears when it closes.

Nothing here places orders. A source produces candles; the engine and the order
manager decide what to do with them.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Optional, Protocol, runtime_checkable

import database as db
import kite_client
from config import settings

log = logging.getLogger("bar_source")

# Kite history is authoritative; refresh it at most this often unless forced.
HISTORY_REFRESH_SECONDS = 30.0


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
        return 900

# NIFTY 50 spot on NSE. Not tradeable, but it is the index the futures track, so
# its bars are the signal series for a NIFTY-based strategy.
NIFTY_SPOT_TOKEN = 256265
NIFTY_SPOT_KEY = "NSE:NIFTY 50"

# Enough closed bars for the indicators to be warm with room to spare. The
# longest SuperTrend wants 15; 400 covers a 100-bar moving average filter as
# well, which a NIFTY strategy uses.
HISTORY_BARS = 400


@runtime_checkable
class BarSource(Protocol):
    """Closed bars, oldest first.

    ``refresh_history`` must never raise: it runs on the engine loop, and a feed
    that can throw would take down every strategy sharing that loop. A failure is
    reported through the return value and the source's own ``status()``.
    """

    key: str
    seconds: int

    def refresh_history(self, force: bool = False) -> int: ...
    def candles(self) -> list[dict[str, Any]]: ...
    def bar_count(self) -> int: ...
    def status(self) -> dict[str, Any]: ...



class NiftyBarSource:
    """Closed NIFTY 50 bars from Kite history.

    History only, deliberately. There is no tick subscription for the index, and
    a strategy that trades NSE hours needs nothing more: the bar appears when the
    exchange closes it.

    The series is cached per ``refresh_seconds`` so two strategies - or the
    engine polling every few seconds - do not turn into one API call each. A
    failed refresh keeps the previous series rather than emptying it, because an
    empty list would read as "warming up" and silently stop the strategy.
    """

    def __init__(
        self,
        interval: Optional[str] = None,
        *,
        token: int = NIFTY_SPOT_TOKEN,
        key: str = NIFTY_SPOT_KEY,
        bars: int = HISTORY_BARS,
        client: Any = None,
    ) -> None:
        self.interval = interval or settings.candle_interval
        self.key = key
        self._token = token
        self._bars = max(50, int(bars))
        self._client = client
        self._lock = threading.RLock()
        self._candles: list[dict[str, Any]] = []
        self._at: Optional[datetime] = None
        self._failures = 0
        self._retry_at: Optional[datetime] = None
        self._last_error: Optional[str] = None

    @property
    def seconds(self) -> int:
        return interval_to_seconds(self.interval)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = kite_client.get_client()
        return self._client

    def refresh_history(self, force: bool = False) -> int:
        """Fetch a window big enough for ``self._bars`` closed bars."""
        now = datetime.now(db.IST)
        with self._lock:
            if not force and self._at is not None:
                if (now - self._at).total_seconds() < HISTORY_REFRESH_SECONDS:
                    return len(self._candles)
            if not force and self._retry_at is not None and now < self._retry_at:
                return len(self._candles)

        # Ask for more calendar time than bars needed: NSE trades ~6.25 h a day,
        # so a 60-minute series needs about 2.6 days of calendar per 16 bars.
        span_seconds = self.seconds * self._bars * 4
        from_date = now - timedelta(seconds=span_seconds)

        try:
            rows = self.client.historical_data(
                instrument_token=self._token,
                from_date=from_date,
                to_date=now,
                interval=self.interval,
            )
        except Exception as exc:  # pragma: no cover - defensive
            rows = []
            self._last_error = str(exc)

        if not rows:
            with self._lock:
                self._failures += 1
                self._at = now
                self._last_error = self._last_error or "Kite returned no NIFTY history"
                # Back off exponentially, so a bad token costs one attempt per
                # few minutes instead of one per engine poll.
                self._retry_at = now + timedelta(
                    seconds=min(300, 30 * (2 ** (self._failures - 1)))
                )
                kept = len(self._candles)
            if self._failures == 1 or self._failures % 10 == 0:
                log.warning(
                    "No NIFTY history (%s); keeping %d bars", self._last_error, kept
                )
            return kept

        series = [
            {
                "time": row["date"].replace(tzinfo=None).isoformat(sep=" "),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
            for row in rows
            if row.get("date") is not None
        ]
        series.sort(key=lambda candle: candle["time"])
        # The newest bar may still be forming; Kite includes it. Dropping it is
        # what keeps "closed bar" true, and it matches the GIFT feed's contract.
        if series and self._is_forming(series[-1]["time"], now):
            series.pop()

        with self._lock:
            # Only the tail is kept: the indicators need a fixed window, and an
            # unbounded list would grow for the life of the process.
            self._candles = series[-self._bars:]
            self._at = now
            self._failures = 0
            self._retry_at = None
            self._last_error = None
            return len(self._candles)

    def _is_forming(self, bar_at: str, now: datetime) -> bool:
        """True when ``bar_at``'s bucket has not finished yet.

        A bar timestamp is its open, so it is complete only once
        ``bar_at + interval`` has passed.

        The bar timestamps this series carries are naive and mean IST - Kite
        returns exchange time and :meth:`refresh_history` strips the offset with
        ``replace(tzinfo=None)``. They must therefore be *labelled* IST, not
        converted into it: ``astimezone`` on a naive datetime assumes the value is
        in system local time, which in the container is UTC, so every bar was
        pushed 5h30m into the future. A bar that closed at 15:15 was judged to
        close at 20:45 and reported as still forming, so the newest closed bar was
        popped off the series on every refresh.

        The effect was a whole-bar lag, not a display error. The 60-minute NIFTY
        book sat on the 14:15 bar from 15:15 until the process was restarted - the
        15:15, 16:15 and 17:15 bars were each fetched and then discarded - so a
        flip that closed at 15:15 was not seen at all. That is the delayed flip.
        """
        try:
            opened = datetime.fromisoformat(bar_at)
        except ValueError:
            return False
        closed_at = opened + timedelta(seconds=self.seconds)
        if closed_at.tzinfo is None:
            # Naive means IST here, not system local time. Labelling is a
            # conversion-free operation; `astimezone` would shift the value.
            closed_at = closed_at.replace(tzinfo=db.IST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=db.IST)
        return closed_at > now

    def candles(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._candles)

    def bar_count(self) -> int:
        with self._lock:
            return len(self._candles)

    def start(self) -> bool:
        """No socket to open; prime the series instead."""
        self.refresh_history(force=True)
        return self.bar_count() > 0

    def stop(self, timeout: float = 5.0) -> None:  # noqa: ARG002 - protocol parity
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "source": type(self).__name__,
                "symbol": self.key,
                "interval": self.interval,
                "seconds": self.seconds,
                "bars": len(self._candles),
                "last_bar_at": self._candles[-1]["time"] if self._candles else None,
                "last_refresh_at": self._at.isoformat() if self._at else None,
                "failures": self._failures,
                "error": self._last_error,
                "tradeable": False,
            }


def build_source(kind: str, interval: Optional[str] = None) -> BarSource:
    """A source by name, for the config to construct.

    An unknown name raises rather than silently defaulting: a typo in a strategy
    config should fail at startup, not trade the wrong instrument.
    """
    name = (kind or "").strip().lower()
    if name in {"nifty", "nifty50", "nse:nifty 50"}:
        return NiftyBarSource(interval)
    raise ValueError(f"Unknown bar source {kind!r}; expected 'nifty'")


__all__ = [
    "BarSource",
    "NiftyBarSource",
    "build_source",
    "interval_to_seconds",
    "HISTORY_REFRESH_SECONDS",
    "NIFTY_SPOT_TOKEN",
    "NIFTY_SPOT_KEY",
]
