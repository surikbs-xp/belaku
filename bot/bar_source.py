"""Where closed bars come from, independent of which strategy reads them.

The engine used to assume exactly one answer to "what is the signal series": the
GIFT Nifty feed. A second strategy needs a second series, so the assumption is
made explicit here as a small protocol that both sources satisfy.

Keeping the sources behind one interface is what lets :class:`strategy.StrategyVerdict`
and the engine's decision logic stay unchanged: every source yields the same
``{time, open, high, low, close}`` dicts, oldest first, newest bar last.

The two implementations differ in one way that matters, and it is a real
difference rather than an accident:

* :class:`GiftBarSource` wraps the live tick feed, which also carries a websocket
  and a forming bar. It reads ~20 hours a day.
* :class:`NiftyBarSource` reads Kite history only - there is no tick subscription
  for the index. It therefore has no forming bar and no live path, which is
  exactly right for an NSE-hours strategy: a bar appears when it closes.

Nothing here places orders. A source produces candles; the engine and the order
manager decide what to do with them.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time as datetime_time, timedelta
from typing import Any, Optional, Protocol, runtime_checkable

import database as db
import kite_client
from config import settings
from gift_nifty import HISTORY_REFRESH_SECONDS, interval_to_seconds

log = logging.getLogger("bar_source")

# NIFTY 50 spot on NSE. Not tradeable, but it is the index the futures track, so
# its bars are the signal series for a NIFTY-based strategy.
NIFTY_SPOT_TOKEN = 256265
NIFTY_SPOT_KEY = "NSE:NIFTY 50"

# Enough closed bars for the indicators to be warm with room to spare. The
# longest SuperTrend wants 15; 400 covers a 100-bar moving average filter as
# well, which a NIFTY strategy uses.
HISTORY_BARS = 400

# The NSE F&O session, as the signal series is allowed to see it. Imported from
# the market clock rather than restated, so the gate and the order path can never
# disagree about when the exchange is open.
SESSION_OPEN = datetime_time(*kite_client.MARKET_OPEN)
SESSION_CLOSE = datetime_time(*kite_client.MARKET_CLOSE)


def bar_in_session(bar_at: Any, seconds: int) -> bool:
    """Whether the bar timestamped ``bar_at`` overlaps the NSE session.

    A bar timestamp is its *open*, so the test is an overlap rather than a
    containment, and both ends need it:

    * The **09:00** bar on 15-minute bars opens before the exchange does but
      closes at exactly 09:15 - it is the first in-session close of the day, and
      the engine's morning decision is specified against it. A close-only test
      keeps it; an open-only test would drop it.
    * The **15:15** bar on 60-minute bars opens in session but its bucket runs to
      16:15, because the exchange shuts at 15:30 and the bar is a partial. A
      close-only test would drop the last bar of the day; an open-only test keeps
      it.

    So a bar is kept when it opens before the close *and* closes at or after the
    open. That keeps both edge bars and still drops everything overnight.

    This is the gate that keeps the signal series to the hours the futures leg
    can actually trade. GIFT Nifty quotes ~20 hours a day, but the NFO future
    does not, so an overnight bar can only ever produce a signal that has to be
    latched until 09:15 - by which time the market has usually moved past the
    level the signal was taken at. Measured over three years of 60-minute GIFT
    bars, half of all fresh signals fired out of session and the median drift
    between the signal bar's close and the first in-session fill was 60 points,
    against a 50-point stop on the 15-minute book.

    A timestamp that cannot be parsed is kept rather than dropped: a bar the
    series cannot place in time is a bug to surface, not a bar to silently
    discard.
    """
    try:
        opened = datetime.fromisoformat(str(bar_at))
    except (TypeError, ValueError):
        return True
    opened_at = opened.time()
    closed_at = (opened + timedelta(seconds=seconds)).time()
    return opened_at < SESSION_CLOSE and closed_at >= SESSION_OPEN


def session_bars(candles: list[dict[str, Any]], seconds: int) -> list[dict[str, Any]]:
    """``candles`` restricted to the bars that overlap the NSE session."""
    return [c for c in candles if bar_in_session(c.get("time"), seconds)]


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


class GiftBarSource:
    """The existing GIFT Nifty feed, behind the protocol.

    A thin adapter rather than a rewrite: the feed owns a websocket, a tick
    builder and a persisted candle series, and none of that changes. This class
    exists so the engine can hold it beside another source without caring which
    is which.
    """

    def __init__(self, feed: Any = None, interval: Optional[str] = None) -> None:
        self._feed = feed
        # The timeframe this book reads. None means the feed's own, which is the
        # single-timeframe case every deployment had before. A book asking for a
        # different one gets it built from the same ticks by the same feed - the
        # websocket is process-global and cannot be opened twice.
        self.interval = interval

    @property
    def feed(self) -> Any:
        """The GIFT feed, resolved on every read.

        Deliberately not cached. ``gift_nifty.get_feed`` is the process
        singleton and can be reset (a reconnect does exactly that), so holding
        the first instance would pin this source to a feed that has been
        replaced - and the strategy would quietly read a stopped series.
        """
        if self._feed is not None:
            return self._feed
        from gift_nifty import get_feed

        return get_feed()

    @property
    def key(self) -> str:
        return "NSEIX:GIFT NIFTY"

    @property
    def seconds(self) -> int:
        self._ensure_interval()
        # The book's own interval wins only when the feed declares one and it
        # differs. A feed with no `interval` at all is a tick-only stub, and its
        # `seconds` is the only statement of bar length available - reading the
        # configured name there would report a length the stub is not building.
        feed_interval = getattr(self.feed, "interval", None)
        if self.interval and feed_interval and self.interval != feed_interval:
            from gift_nifty import interval_to_seconds

            return interval_to_seconds(self.interval)
        return int(self.feed.seconds)

    def _ensure_interval(self) -> None:
        """Ask the feed to build this timeframe if it is not the feed's own.

        Called from ``candles`` as well as from ``seconds``: every reader of this
        book must find the series being built, and ``seconds`` is what the engine
        asks before it evaluates. Leaving it only in ``candles`` meant a book
        could report a 900s bar length while the feed had not yet been told to
        build it, so the first evaluations read an empty series.
        """
        if not self.interval:
            return
        ensure = getattr(self.feed, "ensure_interval", None)
        if ensure is not None:
            ensure(self.interval)

    def refresh_history(self, force: bool = False) -> int:
        """Ask the feed to refresh. Never raises - see the protocol note.

        The feed's own signature varies (a tick-only stub may take no arguments
        and expose no count), so both are called defensively: a refresh that
        cannot run must degrade to the series already in hand rather than take
        the engine loop down.
        """
        feed = self.feed
        try:
            try:
                feed.refresh_history(force=force)
            except TypeError:
                feed.refresh_history()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("GIFT history refresh failed: %s", exc)
        # The count the protocol promises, without candles()' full copy. The
        # engine calls this once per book per tick - every 5 s, whether or not a
        # bar closed - and it used to return bar_count(), which duplicated the
        # whole series for a value the engine then discarded.
        counter = getattr(self.feed, "interval_count", None)
        if counter is not None:
            try:
                return int(counter(self.interval))
            except Exception:  # pragma: no cover - defensive
                pass
        return len(self.candles())

    def candles(self) -> list[dict[str, Any]]:
        self._ensure_interval()
        reader = getattr(self.feed, "interval_candles", None)
        if reader is not None:
            series = list(reader(self.interval) or [])
        else:
            series = list(self.feed.candles() or [])
        # The feed builds bars around the clock; the strategy may only see the
        # ones that close while the futures leg can trade. See
        # :func:`bar_in_session` for why this is a signal gate and not
        # merely an execution one.
        return session_bars(series, self.seconds)

    def bar_count(self) -> int:
        return len(self.candles())

    def start(self) -> bool:
        return bool(self.feed.start())

    def stop(self, timeout: float = 5.0) -> None:
        self.feed.stop(timeout=timeout)

    def status(self) -> dict[str, Any]:
        state = self.feed.status()
        state["source"] = type(self).__name__
        state["symbol"] = self.key
        # The feed reports its *own* timeframe, which is not this book's when the
        # book asked for a different one. Reporting it verbatim made a 15-minute
        # book show "60minute" on the dashboard - the one field a reader uses to
        # confirm which series they are looking at.
        if self.interval:
            state["interval"] = self.interval
            state["interval_seconds"] = self.seconds
        return state


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
        self.interval = interval or settings.gift_candle_interval
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
            series = list(self._candles)
        # NIFTY spot history is already NSE-hours only, so this is normally a
        # no-op - but the gate belongs on the source, not on an assumption about
        # what the exchange happened to return.
        return session_bars(series, self.seconds)

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
    if name in {"gift", "gift_nifty", "nseix:gift nifty"}:
        return GiftBarSource()
    if name in {"nifty", "nifty50", "nse:nifty 50"}:
        return NiftyBarSource(interval)
    raise ValueError(f"Unknown bar source {kind!r}; expected 'gift' or 'nifty'")


__all__ = [
    "BarSource",
    "GiftBarSource",
    "NiftyBarSource",
    "build_source",
    "NIFTY_SPOT_TOKEN",
    "NIFTY_SPOT_KEY",
]
