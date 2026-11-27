"""GIFT Nifty market data: authoritative Kite history plus a live tick feed.

GIFT Nifty (NSEIX, NSE IFSC) is the pre-market proxy for Nifty 50 and trades
~20 hours a day. It is quote-only on Kite (it cannot be traded), but Kite *does*
serve its historical candles, including the overnight session::

    kite.historical_data(291849, ..., interval="5minute")

That history is the **authoritative** candle source for the strategy. Building
candles locally from ticks was measured against it and disagreed on 21 of 42
bars, because ticks are throttled and dropped, so the local OHLC was wrong and
every recursive indicator (PSAR, SuperTrend) inherited the error.

So this module:

* loads closed candles from Kite history (the source of truth),
* keeps the in-progress bucket separate and updates it from live ticks,
* falls back to tick-built candles only when history is unavailable, and
* persists bars to SQLite so a restart has data before the first API call.

Only ticks during a session advance the forming candle. The 20-hour GIFT session
and the 09:15-15:30 NSE F&O session are different windows, and an out-of-hours
tick must not be glued onto the last in-hours bar - that would corrupt the OHLC.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import database as db
from config import settings
from kite_client import IST, MARKET_OPEN, contract_expiry, future_symbol, get_client

log = logging.getLogger("gift_nifty")

# NSEIX index symbol for GIFT Nifty. Kite exposes it as a non-tradeable index.
GIFT_EXCHANGE = "NSEIX"
GIFT_SYMBOL = "GIFT NIFTY"
GIFT_KEY = f"{GIFT_EXCHANGE}:{GIFT_SYMBOL}"

# Fallback token if the instrument dump is unavailable (observed stable since
# the 2024-04 fix for the mis-segregated NSEIX row).
GIFT_FALLBACK_TOKEN = 291849
VOLUME_BASIS = "tick_count_proxy"

# A tick-built bar is only trustworthy if the feed watched it from its opening.
# Such bars are used only when Kite history is unavailable.
PARTIAL_NONE = 0
PARTIAL_GAP = 1  # ticks missing inside the bar (joined late / feed outage)
PARTIAL_CUT = 2  # cut off by a shutdown; resumable if restarted in-bucket
JOIN_GRACE_SECONDS = 10.0

# Kite history is authoritative; refresh it at most this often unless forced.
HISTORY_REFRESH_SECONDS = 30.0
# After a failed history fetch, wait before trying again. Doubles per failure up
# to the cap, so a dead token costs one attempt per few minutes instead of one
# per refresh interval.
HISTORY_RETRY_BASE_SECONDS = 30.0
HISTORY_RETRY_MAX_SECONDS = 300.0
# How much history to request, so recursive indicators are fully converged.
HISTORY_LOOKBACK_DAYS = 5


def _merge_forming(
    current: Optional["Candle"], forming: "Candle"
) -> "Candle":
    """Fold a Kite forming-bar snapshot into the live tick-built bar.

    The exchange knows the bar's true open and the range it has traded, but the
    tick feed knows how many ticks were actually observed. Keeping the larger
    range and the newest close on one object is what stops a bar from closing at
    a single price after ``refresh_history`` has replaced it.

    ``ticks`` is carried across deliberately: it records that the bucket was
    watched from its opening, which is what qualifies the bar for the indicator
    series when Kite history is unavailable.
    """
    if current is None or current.start != forming.start:
        return forming
    merged = Candle(
        start=current.start,
        open=current.open,
        high=max(current.high, forming.high),
        low=min(current.low, forming.low),
        close=forming.close,
        volume=max(current.volume, forming.volume),
        ticks=current.ticks,
        partial=current.partial,
    )
    return merged


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
    partial: int = PARTIAL_NONE

    def to_row(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(timespec="seconds"),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "partial": self.partial,
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
        return 900


def bucket_start(moment: datetime, seconds: int) -> datetime:
    """Floor ``moment`` to the start of its candle bucket.

    Buckets are aligned to the **NSE session open**, not to the UTC epoch. The
    epoch is a multiple of every intraday interval, so flooring to it puts the
    boundaries on UTC-aligned wall-clock times: on 60-minute bars that is 08:30,
    09:30, 10:30 IST, and on 15-minute bars it happens to coincide with 09:15
    only because 09:15 is a quarter-hour.

    That is not how a chart draws them. TradingView and Kite both anchor
    intraday bars to the session open, so a 60-minute chart shows 09:15-10:15,
    10:15-11:15 and so on. Flooring to the epoch instead produced bars covering
    08:30-09:30, 09:30-10:30 - a different window for every bar, and one fewer
    bar per day. The indicators were then computed on a series the chart never
    shows, so SuperTrend and PSAR sat at levels that could not be reconciled
    against the chart no matter how correct the arithmetic was.

    Anchoring to the session open makes the bot's bars the same bars the chart
    draws. Outside the session the anchor still applies, so an overnight bar is
    a clean multiple of the interval from 09:15 rather than from the epoch.
    """
    epoch = int(moment.timestamp())
    anchor = int(
        moment.replace(
            hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0
        ).timestamp()
    )
    return datetime.fromtimestamp(
        anchor + ((epoch - anchor) // seconds) * seconds, tz=IST
    )


@dataclass
class CandleBuilder:
    """Aggregates ticks into fixed-interval OHLCV bars.

    ``on_close`` fires with a finished bar. A bar closes when a tick arrives in
    a later bucket; the final bar of a session is flushed by :meth:`flush`.

    ``interval`` is the Kite-style name of ``seconds``, carried here rather than
    read from the feed: one feed can build several timeframes from the same
    ticks, and a bar must be persisted under the timeframe it was built at or a
    15-minute and a 60-minute bucket that share a start time collide.
    """

    seconds: int = 900
    interval: str = ""
    on_close: Optional[Callable[[Candle], None]] = None
    current: Optional[Candle] = None
    closed: list[Candle] = field(default_factory=list)
    max_history: int = 2000
    outage: bool = False

    def mark_outage(self) -> None:
        """Record that ticks may have been missed (feed disconnect)."""
        self.outage = True
        if self.current is not None and self.current.partial == PARTIAL_NONE:
            self.current.partial = PARTIAL_GAP

    def _opened_late(self, moment: datetime, start: datetime) -> bool:
        return (moment - start).total_seconds() > JOIN_GRACE_SECONDS

    def add_tick(self, price: float, moment: Optional[datetime] = None, volume: float = 0.0) -> Optional[Candle]:
        """Feed one tick. Returns the bar that closed, if any."""
        if price is None or price <= 0:
            return None
        moment = moment or datetime.now(IST)
        start = bucket_start(moment, self.seconds)

        if self.current is None:
            self.current = Candle(
                start=start, open=price, high=price, low=price, close=price,
                volume=volume, ticks=1,
                partial=PARTIAL_GAP if self._opened_late(moment, start) else PARTIAL_NONE,
            )
            self.outage = False
            return None

        if start > self.current.start:
            finished = self.current
            self._store(finished)
            late = self.outage and self._opened_late(moment, start)
            self.outage = False
            self.current = Candle(
                start=start, open=price, high=price, low=price, close=price,
                volume=volume, ticks=1,
                partial=PARTIAL_GAP if late else PARTIAL_NONE,
            )
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
        if (
            finished.partial == PARTIAL_NONE
            and datetime.now(IST) < finished.start + timedelta(seconds=self.seconds)
        ):
            finished.partial = PARTIAL_CUT
        self._store(finished)
        self.current = None
        if self.on_close:
            self.on_close(finished)
        return finished

    def _store(self, candle: Candle) -> None:
        # A bar that closed without a single tick of its own cannot be trusted as
        # a price: ``refresh_history`` seeds ``current`` from Kite's partial row,
        # and ``add_tick`` stores it unchanged when the bucket rolls over. A flat
        # bar like that is what priced an entry ~84 points off the market, so
        # flag it partial and keep it out of the indicator series.
        if candle.ticks <= 0 and candle.partial == PARTIAL_NONE:
            candle.partial = PARTIAL_GAP
        self.closed.append(candle)
        if len(self.closed) > self.max_history:
            del self.closed[:-self.max_history]


def _reactor_running() -> bool:
    """Whether Twisted's reactor is running. It is process-global and one-shot."""
    try:
        from twisted.internet import reactor

        return bool(reactor.running)
    except Exception:  # pragma: no cover - SDK without Twisted
        return False


class GiftNiftyFeed:
    """Owns the GIFT Nifty tick subscription and the candle series.

    Thread-safe: ticks arrive on the KiteTicker websocket thread while the
    strategy engine reads the candle list from the engine thread.
    """

    def __init__(self, interval: Optional[str] = None) -> None:
        self.interval = interval or settings.gift_candle_interval
        self.seconds = interval_to_seconds(self.interval)
        self._lock = threading.RLock()
        self._builder = CandleBuilder(seconds=self.seconds, interval=self.interval)
        # A second timeframe built from the same ticks. The websocket is
        # process-global and one-shot (Twisted's reactor cannot restart), so a
        # GIFT book on a different interval cannot open its own feed - it would
        # either raise ReactorNotRestartable or double-subscribe the token.
        # Instead the one feed aggregates every tick into each requested
        # timeframe. Keyed by interval name; empty until a book asks for one.
        self._extra_builders: dict[str, CandleBuilder] = {}
        self._extra_history: dict[str, list[dict[str, Any]]] = {}
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
        # The execution future rides the same websocket as GIFT Nifty, because
        # Twisted's reactor is process-global and one-shot: a second ticker
        # cannot be created without raising ReactorNotRestartable on the first.
        # Its ticks must NOT reach the candle builder, which is the signal
        # source - see _handle_ticks.
        self.exec_token: Optional[int] = None
        self.exec_symbol = ""
        self.exec_last_price: Optional[float] = None
        self.exec_last_tick_at: Optional[datetime] = None
        self.exec_ticks = 0
        self.exec_source = "none"
        self._history: list[dict[str, Any]] = []
        self._resume_candle: Optional[Candle] = None
        self._history_loaded = False
        self._loaded_count = 0
        self._history_source = "none"
        self._history_at: Optional[datetime] = None
        # Fingerprint of the last Kite history payload, so an unchanged window
        # is not rebuilt into fresh Candle objects on every refresh.
        self._history_newest: Optional[datetime] = None
        self._history_rows = 0
        # Exponential backoff after a failed history fetch.
        self._history_failures = 0
        self._history_retry_at: Optional[datetime] = None
        self._history_error: Optional[str] = None
        # Set whenever a bar closes, so the engine can evaluate immediately
        # instead of waiting for its next poll. This is the difference between a
        # signal being acted on within milliseconds of the bar closing and being
        # acted on up to one poll interval later. It is a plain Event rather than
        # a queue because only the *fact* of a close matters: the engine reads
        # the series itself, so a coalesced wake-up loses nothing.
        self.bar_closed = threading.Event()
        # Set when a bar closes while Kite history is authoritative. The
        # tick-built bar is discarded in that case (Kite's OHLC is the source of
        # truth), so the new bar only appears after a history fetch - and that
        # fetch is normally throttled to HISTORY_REFRESH_SECONDS. Without this
        # marker the signal waited up to 30 s for a refresh that the close had
        # already made necessary. The engine clears it after forcing the fetch.
        self.history_due = threading.Event()

    # -- history ---------------------------------------------------------
    def load_persisted(self) -> int:
        """Seed the series from SQLite so a restart has bars before any API call."""
        with self._lock:
            if self._history_loaded:
                return self._loaded_count
        rows = db.get_gift_candles(limit=self._builder.max_history, interval=self.interval)
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
                partial=int(row.get("partial") or PARTIAL_NONE),
            )
        rows = [r for r in rows if not r.get("partial")]
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
            if rows:
                self._history_source = "sqlite"
        if self._loaded_count:
            log.info(
                "Loaded %d persisted GIFT Nifty candles (warmup carry-over)",
                self._loaded_count,
            )
        return self._loaded_count

    def refresh_history(self, force: bool = False) -> int:
        """Replace the closed-candle series with Kite's authoritative history.

        Kite serves NSEIX history, so this is the source of truth for the
        indicators. Returns the number of closed bars now held.
        """
        now = datetime.now(IST)
        # A bar close makes the fetch due immediately, whatever the throttle
        # says: the series is missing a bar the market has already finished.
        if self.history_due.is_set():
            force = True
        with self._lock:
            if (
                not force
                and self._history_at is not None
                and (now - self._history_at).total_seconds() < HISTORY_REFRESH_SECONDS
            ):
                return len(self._history)
            if not force and self._history_retry_at is not None and now < self._history_retry_at:
                return len(self._history)
            token = self.token
        if token is None:
            token = self.resolve_token()
        if token is None:
            with self._lock:
                self._history_error = "GIFT Nifty token unresolved"
            return self.bar_count()

        rows = get_client().historical_data(
            instrument_token=token,
            from_date=now - timedelta(days=HISTORY_LOOKBACK_DAYS),
            to_date=now,
            interval=self.interval,
        )
        if not rows:
            with self._lock:
                self._history_at = now
                self._history_error = "Kite returned no GIFT Nifty history"
                # Back off so a dead token or a closed exchange does not retry
                # (with its own retries and sleeps) every refresh interval.
                self._history_failures += 1
                self._history_retry_at = now + timedelta(
                    seconds=min(
                        HISTORY_RETRY_MAX_SECONDS,
                        HISTORY_RETRY_BASE_SECONDS * (2 ** (self._history_failures - 1)),
                    )
                )
                failures = self._history_failures
            # Log the first failure and then only every tenth, so a long outage
            # does not fill the log with the same line.
            if failures == 1 or failures % 10 == 0:
                log.warning(
                    "Kite returned no GIFT Nifty history (attempt %d); keeping %d bars",
                    failures,
                    self.bar_count(),
                )
            return self.bar_count()

        current_start = bucket_start(now, self.seconds)
        # Kite returns the whole lookback window every time. When the newest
        # closed bucket and the bar count are unchanged, the series is identical
        # to what is already held, so skip rebuilding ~1200 Candle objects.
        newest_closed = None
        for row in reversed(rows):
            moment = row["date"]
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=IST)
            start = bucket_start(moment, self.seconds)
            if start < current_start:
                newest_closed = start
                break
        with self._lock:
            unchanged = (
                newest_closed is not None
                and newest_closed == self._history_newest
                and len(rows) == self._history_rows
            )
            if unchanged:
                self._history_at = now
                self._history_error = None
                self.history_due.clear()
                return len(self._history)

        closed: list[dict[str, Any]] = []
        forming: Optional[Candle] = None
        for row in rows:
            moment = row["date"]
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=IST)
            start = bucket_start(moment, self.seconds)
            candle = Candle(
                start=start,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row.get("volume") or 0.0),
            )
            # A Kite bar is only closed once its own span has elapsed. Kite stamps
            # bars at an offset (the 16:01->16:06 bucket is stamped 16:01) while
            # ``bucket_start`` floors that to 16:00, so comparing floored starts
            # alone calls a bar that is still forming "closed". A forming bar
            # carries only the OHLC of its first tick, which is how a flat
            # 25399.00 bar replaced a true 103-point range (25399 -> 25296) and
            # booked an entry 84 points above the market.
            if moment + timedelta(seconds=self.seconds) > now:
                forming = candle
                continue
            closed.append(candle.to_strategy_candle())

        with self._lock:
            self._history = closed[-self._builder.max_history:]
            self._history_at = now
            self._history_error = None
            self._history_source = "kite"
            self._loaded_count = len(self._history)
            self._history_loaded = True
            self._history_newest = newest_closed
            self._history_rows = len(rows)
            self._history_failures = 0
            self._history_retry_at = None
            if forming is not None:
                # Kite's newest row is the bucket still in progress. Merge it into
                # whatever the tick feed has already accumulated for that bucket
                # instead of replacing it: ``add_tick`` folds new ticks into
                # ``current`` and closes it as-is without recomputing high/low, so
                # assigning a bare snapshot here silently discards every tick seen
                # so far and closes the bar at a single price.
                self._builder.current = _merge_forming(self._builder.current, forming)
                self._resume_candle = None
        self.history_due.clear()
        self._refresh_extra_history(now)
        log.info(
            "GIFT Nifty history refreshed from Kite: %d closed bars, forming=%s",
            len(closed), forming.start.strftime("%H:%M") if forming else "none",
        )
        return len(closed)

    def _refresh_extra_history(self, now: Optional[datetime] = None) -> None:
        """Seed each secondary timeframe from Kite, like the primary series.

        Without this a 15-minute book would run on tick-built bars while its
        60-minute twin ran on exchange OHLC - a difference in data quality
        masquerading as a difference in timeframe. Best effort: a failed fetch
        leaves the tick-built series in place rather than emptying it, because an
        empty list reads as "warming up" and would stop the book.
        """
        with self._lock:
            intervals = list(self._extra_builders)
        for interval in intervals:
            self._seed_extra_history(interval, now)

    def _seed_extra_history(
        self, interval: str, now: Optional[datetime] = None
    ) -> None:
        """Fetch one secondary timeframe's history and hand it to the builder."""
        now = now or datetime.now(IST)
        token = self.token
        if token is None:
            return
        try:
            rows = get_client().historical_data(
                instrument_token=token,
                from_date=now - timedelta(days=HISTORY_LOOKBACK_DAYS),
                to_date=now,
                interval=interval,
            )
        except Exception as exc:  # pragma: no cover - network
            log.warning("No %s GIFT history: %s", interval, exc)
            return
        if not rows:
            return
        seconds = interval_to_seconds(interval)
        closed: list[dict[str, Any]] = []
        for row in rows:
            moment = row["date"]
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=IST)
            start = bucket_start(moment, seconds)
            if start + timedelta(seconds=seconds) > now:
                continue
            closed.append({
                "time": start.replace(tzinfo=None).isoformat(sep=" "),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
        if closed:
            self.interval_history(interval, closed)

    def _persist(self, candle: Candle, interval: Optional[str] = None) -> None:
        """Store one bar under the timeframe it was built at.

        ``interval`` defaults to the feed's own, so the single-timeframe case is
        unchanged. A second builder passes its own: the key is (start, interval),
        and two timeframes share a start time at least once an hour.
        """
        try:
            db.record_gift_candle(
                start=candle.start.isoformat(timespec="seconds"),
                open_=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                ticks=candle.ticks,
                partial=candle.partial,
                interval=interval or self.interval,
            )
        except Exception as exc:  # pragma: no cover - best effort
            log.warning("Failed to persist GIFT candle: %s", exc)

    # -- candles ---------------------------------------------------------
    def _on_bar_close(self, candle: Candle, interval: Optional[str] = None) -> None:
        # Wake the engine first, before the SQLite write below. The write is
        # fast but it is still I/O on the tick thread, and the signal must not
        # wait behind it.
        self.bar_closed.set()
        self._persist(candle, interval)
        with self._lock:
            authoritative = self._history_source == "kite"
        if authoritative:
            # Kite history is the source of truth; the next refresh replaces
            # this bar with the exchange's own OHLC. Mark it due so the engine
            # fetches now rather than waiting out the refresh throttle.
            self.history_due.set()
            log.info(
                "GIFT bar closed %s O=%.2f H=%.2f L=%.2f C=%.2f ticks=%d "
                "(awaiting Kite history confirmation)",
                candle.start.strftime("%H:%M"), candle.open, candle.high,
                candle.low, candle.close, candle.ticks,
            )
            return
        if candle.partial:
            log.info(
                "GIFT bar %s excluded from indicators: incomplete tick coverage "
                "(ticks=%d)", candle.start.strftime("%H:%M"), candle.ticks,
            )
            return
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

    # -- additional timeframes -------------------------------------------
    def ensure_interval(self, interval: str) -> None:
        """Start building ``interval`` from the same ticks, if not already.

        Idempotent, because every poll of every book calls it. A book on the
        feed's own interval needs nothing - it reads ``candles()``.
        """
        if not interval or interval == self.interval:
            return
        with self._lock:
            if interval in self._extra_builders:
                return
            seconds = interval_to_seconds(interval)
            builder = CandleBuilder(seconds=seconds, interval=interval)
            builder.on_close = lambda bar, _i=interval: self._on_extra_close(_i, bar)
            self._extra_builders[interval] = builder
            self._extra_history[interval] = []
        log.info("GIFT feed now also building %s bars from the same ticks", interval)
        # Seed it now rather than waiting for the next primary refresh. That
        # refresh is throttled to HISTORY_REFRESH_SECONDS, so a timeframe
        # registered just after one had already run would sit empty until the
        # throttle expired - and an empty series reads as "warming up", so the
        # book looked broken while its ticks were being aggregated correctly.
        self._seed_extra_history(interval)

    def interval_count(self, interval: Optional[str] = None) -> int:
        """How many closed bars ``interval`` holds, without copying them.

        The engine asks for this once per book per tick. ``interval_candles``
        would answer it, but it returns a fresh list of every bar - a full copy
        every 5 s for a number, on a path that runs whether or not a bar closed.
        """
        if not interval or interval == self.interval:
            return len(self._history)
        with self._lock:
            return len(self._extra_history.get(interval, []))

    def interval_candles(self, interval: Optional[str] = None) -> list[dict[str, Any]]:
        """Closed candles at ``interval`` (default: this feed's own)."""
        if not interval or interval == self.interval:
            return self.candles()
        with self._lock:
            return list(self._extra_history.get(interval, []))

    def interval_history(self, interval: str, rows: list[dict[str, Any]]) -> None:
        """Seed a secondary series with exchange history.

        The twin's bars come from Kite like the primary's, so both books run on
        exchange OHLC rather than on tick-built approximations of differing
        quality.

        Merged, not replaced. Kite's history lags the tick feed by seconds to
        minutes, so a straight assignment threw away a bar the ticks had already
        closed and put an older series in its place. The engine keys off the
        newest bar's timestamp, so it then saw the same bar it had already
        evaluated and skipped - the signal sat unacted-on until Kite caught up.
        Observed live: a 15-minute bar closed at 21:00 and the order was placed
        at 21:04, four minutes later.

        The merge keeps every bar the ticks produced and lets Kite's OHLC win
        where the two overlap, which is the same rule the primary series uses.
        """
        with self._lock:
            builder = self._extra_builders.get(interval)
            if builder is None:
                return
            existing = self._extra_history.get(interval) or []
            # Kite's rows are authoritative for the bars it has; anything newer
            # than its newest row came from ticks and must survive.
            by_time = {row["time"]: row for row in existing}
            for row in rows:
                by_time[row["time"]] = row
            merged = [by_time[key] for key in sorted(by_time)]
            self._extra_history[interval] = merged[-builder.max_history:]

    def _on_extra_close(self, interval: str, candle: Candle) -> None:
        """Persist and append a closed bar from a secondary timeframe."""
        self.bar_closed.set()
        self._persist(candle, interval)
        if candle.partial:
            return
        with self._lock:
            series = self._extra_history.setdefault(interval, [])
            series.append(candle.to_strategy_candle())
            builder = self._extra_builders.get(interval)
            limit = builder.max_history if builder else 2000
            if len(series) > limit:
                del series[:-limit]

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
                token = tick.get("instrument_token")
                # Route by token. Without this an execution-future tick would be
                # fed into the GIFT candle builder and silently corrupt the
                # signal series - the two prices differ by the basis, so the
                # candles would be wrong rather than obviously broken.
                #
                # A tick with no token is treated as GIFT: every existing caller
                # and test passes bare ``{"last_price": ...}``, and the GIFT
                # series is the one that must never be disturbed.
                if self.exec_token is not None and token == self.exec_token:
                    self.exec_last_price = float(price)
                    self.exec_last_tick_at = datetime.now(IST)
                    self.exec_ticks += 1
                    self.exec_source = "tick"
                    continue
                self.last_price = float(price)
                self.last_tick_at = datetime.now(IST)
                self.error = None
                self.tick_count += 1
                self.session_ticks += 1
                self._builder.on_close = self._on_bar_close
                if self._resume_candle is not None and self._history_source != "kite":
                    tick_start = bucket_start(self.last_tick_at, self.seconds)
                    if tick_start == self._resume_candle.start:
                        if self._resume_candle.partial == PARTIAL_CUT:
                            self._resume_candle.partial = PARTIAL_NONE
                        self._builder.current = self._resume_candle
                        self._resume_candle = None
                    elif tick_start > self._resume_candle.start:
                        # The bar was cut off by the shutdown and not resumed
                        # in time, so its OHLC is incomplete.
                        self._resume_candle = None
                self._builder.add_tick(float(price), self.last_tick_at, volume=1.0)
                for interval, builder in self._extra_builders.items():
                    builder.on_close = lambda bar, _i=interval: self._on_extra_close(_i, bar)
                    builder.add_tick(float(price), self.last_tick_at, volume=1.0)

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
            return
        # The execution future shares this socket (see set_exec_token). After a
        # reconnect the subscription is gone, so it is re-armed here rather than
        # once at startup.
        if self.exec_token is not None:
            self._arm_exec_future(self.exec_symbol)

    def _handle_close(self, _ws: Any, code: Any, reason: Any) -> None:
        self.connected = False
        with self._lock:
            self._builder.mark_outage()
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
                    # threaded=True, not False. The SDK runs Twisted's reactor
                    # inside this thread either way, but with the default it
                    # also calls reactor.run() with signal handlers enabled:
                    #
                    #   ValueError: signal only works in main thread
                    #     of the main interpreter
                    #
                    # which is raised from Twisted's _signals.install() and
                    # aborts the feed before the websocket ever opens. The SDK's
                    # threaded path exists precisely to suppress that
                    # ("Signals are not allowed in non main thread by twisted
                    # so suppress it") — it sets installSignalHandlers=False and
                    # runs the reactor on its own daemon thread.
                    ticker.connect(threaded=True)
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
        # Deliberately NOT calling ticker.stop() here. Twisted's reactor is
        # process-global and one-shot: stopping it raises
        # ReactorNotRestartable on the next connect(), permanently killing the
        # feed for the life of the process. close() shuts the websocket and the
        # SDK's retry loop; leaving the reactor itself running is what lets
        # start() after a token refresh, or the dashboard's Reconnect button,
        # actually work.
        with self._lock:
            self._builder.on_close = self._on_bar_close
            self._builder.flush()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self.connected = False
        log.info("GIFT Nifty feed stopped")

    def _ticker_running(self) -> bool:
        """Whether the ticker is live, rather than whether our thread is.

        Two things make the wrapper thread the wrong signal. ``threaded=True``
        hands the reactor to a thread the SDK owns, so ``connect()`` returns and
        our thread exits while the feed is perfectly healthy. And on a *restart*
        the SDK finds the reactor already running and starts no new thread at
        all. The reactor is therefore the real liveness signal, with the SDK
        thread and then the wrapper thread as fallbacks.

        ``_stop`` is checked first because the reactor outlives an explicit
        ``stop()``: it is process-global and one-shot, so it keeps running after
        we close the websocket, and the reactor alone would report a feed we
        deliberately shut down as live.
        """
        if self._stop.is_set():
            return False
        sdk_thread = getattr(self._ticker, "websocket_thread", None)
        if sdk_thread is not None and sdk_thread.is_alive():
            return True
        if _reactor_running():
            return True
        return bool(self._thread and self._thread.is_alive())

    # -- execution future ------------------------------------------------
    @staticmethod
    def _resolve_exec_token(symbol: str) -> Optional[int]:
        """Instrument token for the execution future, or None."""
        if not symbol:
            return None
        try:
            return get_client().resolve_instrument_token(symbol)
        except Exception as exc:  # pragma: no cover - network
            log.warning("Could not resolve %s token: %s", symbol, exc)
            return None

    def _arm_exec_future(self, symbol: str) -> None:
        """Subscribe the execution future so its ticks arrive on this socket."""
        ticker = self._ticker
        if ticker is None or self.exec_token is None:
            return
        try:
            ticker.subscribe([self.exec_token])
            # LTP mode is enough: only the price is needed, not depth or OHLC.
            ticker.set_mode(ticker.MODE_LTP, [self.exec_token])
            log.info(
                "%s subscribed for execution ticks (token %s)", symbol, self.exec_token
            )
        except Exception as exc:
            # Not fatal: set_exec_token() falls back to REST polling.
            log.warning("Could not subscribe %s: %s", symbol, exc)
            self.exec_source = "unsubscribed"

    def set_exec_token(self, symbol: str, token: Optional[int]) -> None:
        """Point the execution ticker at ``symbol``.

        Called when the front-month contract rolls and when a token is first
        resolved. Passing a different symbol unsubscribes the old token, so a
        roll does not leave both contracts streaming.
        """
        symbol = (symbol or "").strip().upper()
        with self._lock:
            if symbol == self.exec_symbol and token == self.exec_token:
                return
            previous = self.exec_token
            self.exec_symbol = symbol
            self.exec_token = token
            self.exec_ticks = 0
        ticker = self._ticker
        if ticker is not None and previous is not None:
            try:
                ticker.unsubscribe([previous])
            except Exception as exc:
                log.warning("Could not unsubscribe token %s: %s", previous, exc)
        if token is None:
            log.warning("No instrument token for %s; falling back to quote polling", symbol)
            self.exec_source = "poll"

    def refresh_exec_quote(self, symbol: Optional[str] = None) -> None:
        """Poll the execution future's price. Fallback when ticks are absent.

        Called from the mark loop, so it runs at ``ENGINE_INTERVAL_SECONDS``
        (5s by default) rather than at the dashboard's 1.5s poll rate. The Kite
        quote endpoint is rate limited, so this deliberately does not run per
        status request.
        """
        target = (symbol or self.exec_symbol or self.current_exec_symbol()).strip().upper()
        if not target:
            return
        price = get_client().get_ltp(target)
        if price is None:
            return
        with self._lock:
            self.exec_last_price = float(price)
            self.exec_last_tick_at = datetime.now(IST)
            if self.exec_source != "tick":
                self.exec_source = "poll"

    def current_exec_symbol(self) -> str:
        """The front-month future the order manager would trade right now."""
        try:
            from order_manager import get_order_manager

            return get_order_manager().current_symbol()
        except Exception:  # pragma: no cover - defensive
            return future_symbol()

    def exec_quote(self) -> dict[str, Any]:
        """Execution future price for the dashboard, with its provenance."""
        with self._lock:
            age = (
                (datetime.now(IST) - self.exec_last_tick_at).total_seconds()
                if self.exec_last_tick_at
                else None
            )
            return {
                "symbol": self.exec_symbol or self.current_exec_symbol(),
                "price": self.exec_last_price,
                "last_tick_at": self.exec_last_tick_at.isoformat(timespec="seconds")
                if self.exec_last_tick_at
                else None,
                "age_seconds": round(age, 1) if age is not None else None,
                "ticks": self.exec_ticks,
                "source": self.exec_source,
                "subscribed": self.exec_token is not None,
            }

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
                # Liveness is the SDK's reactor, not the wrapper thread:
                # connect() returns as soon as it hands the reactor off, so
                # `self._thread.is_alive()` goes False almost immediately while
                # the feed is perfectly healthy. Reporting that as "not running"
                # made the dashboard show a stale "Reconnecting…" state for a
                # feed that was ticking fine. See _ticker_running().
                "running": self._ticker_running(),
                "starting": self._ticker_running() and not self.connected,
                "interval": self.interval,
                "interval_seconds": self.seconds,
                "volume_basis": VOLUME_BASIS,
                "bars": len(self._history),
                "history_source": self._history_source,
                "history_at": self._history_at.isoformat(timespec="seconds")
                if self._history_at
                else None,
                "history_error": self._history_error,
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
                # Execution future, shown beside GIFT Nifty on the dashboard.
                "exec": {
                    "symbol": self.exec_symbol or self.current_exec_symbol(),
                    "price": self.exec_last_price,
                    # Derived from the symbol, not from a client call: this method
                    # is polled every 1.5s and must not touch the network.
                    "expiry": str(
                        contract_expiry(self.exec_symbol or self.current_exec_symbol()) or ""
                    )
                    or None,
                    "last_tick_at": self.exec_last_tick_at.isoformat(timespec="seconds")
                    if self.exec_last_tick_at
                    else None,
                    "age_seconds": round(
                        (datetime.now(IST) - self.exec_last_tick_at).total_seconds(), 1
                    )
                    if self.exec_last_tick_at
                    else None,
                    "ticks": self.exec_ticks,
                    "source": self.exec_source,
                    "subscribed": self.exec_token is not None,
                },
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
