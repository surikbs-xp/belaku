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
from market_calendar import get_calendar

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

# NSE F&O symbols: NIFTY + 2-digit year + month + "FUT", e.g. NIFTY26NOVFUT.
# Three-letter month names, not the older single-letter codes (NIFTY26KFUT):
# Kite's NFO dump only contains the long form, so the short form resolved to
# nothing and PAPER mode traded a symbol that does not exist.
_MONTH_NAMES = {m: name for m, name in zip(range(1, 13), (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
))}
EXPIRY_WEEKDAY = 1  # Tuesday: NSE's monthly F&O expiry day since Sep 2025

# How many days before expiry the engine starts using the next month's contract.
#
# Rolling early is a deliberate trade-off. The front month is usually the more
# liquid contract, so leaving it early costs some spread - but a position opened
# in the last few days of a contract has to be closed or carried through an
# expiry, and an expiry is a settlement event rather than a trade. Five days
# leaves room for a signal to arrive and the position to be established in the
# contract that will still exist next week.
#
# The window only decides which contract the engine *names*. An existing position
# moves when a signal flips it (see ``OrderManager._execute_directional``), so a
# quiet market can carry a position all the way to the close - which is why a leg
# whose contract has stopped trading blocks new entries until it is squared off.
ROLLOVER_DAYS_BEFORE_EXPIRY = 5

MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

# Dashboard reads that hit the Kite API. The UI polls every 1.5s, so these are
# cached to keep a poll free of network I/O. Order paths pass max_age=0 where a
# fresh answer matters.
PROFILE_CACHE_SECONDS = 15.0
MARGINS_CACHE_SECONDS = 10.0
QUOTE_CACHE_SECONDS = 2.0
# The latency probe is a deliberate extra round-trip, so it runs far less often
# than the poll that displays it. A minute is frequent enough to notice a region
# change or a degraded path, and rare enough to be invisible in the API budget.
LATENCY_CACHE_SECONDS = 60.0
# Enough samples for a stable median without keeping a long history in memory.
LATENCY_SAMPLE_COUNT = 20


# ---------------------------------------------------------------------------
# Market clock
# ---------------------------------------------------------------------------
def market_status(now: Optional[datetime] = None) -> dict[str, Any]:
    """NSE F&O session: Mon-Fri 09:15-15:30 IST, minus exchange holidays.

    The holiday list comes from :mod:`market_calendar`. When no calendar could be
    loaded the session is reported as unverified rather than assumed open, so a
    LIVE order is never placed on a date the bot cannot vouch for.
    """
    now = now or datetime.now(IST)
    open_time = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0)
    close_time = now.replace(hour=MARKET_CLOSE[0], minute=MARKET_CLOSE[1], second=0, microsecond=0)
    is_weekday = now.weekday() < 5

    calendar = get_calendar()
    calendar_known = calendar.available
    holiday = calendar.holiday_name(now.date())

    if not is_weekday:
        is_open, reason = False, "Weekend - market closed"
    elif holiday:
        is_open, reason = False, f"Holiday - {holiday}"
    elif not calendar_known:
        is_open, reason = False, "Session unverified - no NSE holiday calendar available"
    elif now < open_time:
        is_open, reason = False, "Pre-market - opens 09:15 IST"
    elif now > close_time:
        is_open, reason = False, "Post-market - closed at 15:30 IST"
    else:
        is_open, reason = True, "Market open"

    return {
        "is_open": is_open,
        "now_ist": now.isoformat(timespec="seconds"),
        "session_open": open_time.isoformat(timespec="seconds"),
        "session_close": close_time.isoformat(timespec="seconds"),
        "reason": reason,
        "holiday": holiday,
        "calendar_known": calendar_known,
        "calendar_source": calendar.status()["source"] if calendar_known else None,
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


def _expiry_before(expiry: datetime) -> datetime:
    year, month = (expiry.year - 1, 12) if expiry.month == 1 else (expiry.year, expiry.month - 1)
    return datetime(year, month, _last_weekday_of_month(year, month, EXPIRY_WEEKDAY), 15, 30, tzinfo=IST)


def _expiry_has_passed(expiry: date, now: Optional[datetime] = None) -> bool:
    """Whether the 15:30 close on ``expiry`` has passed.

    Expiry day is still a trading day, so the date alone is not enough: the
    contract is live until the close and only dead after it. One source of truth
    for that instant, shared by the rollover status and the entry block.
    """
    moment = now or datetime.now(IST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    if expiry < moment.date():
        return True
    return (
        expiry == moment.date()
        and moment.time().replace(tzinfo=None) > datetime_time(*MARKET_CLOSE)
    )


def contract_expiry(symbol: str) -> Optional[date]:
    """Expiry date encoded in a symbol like ``NIFTY26NOVFUT``, if it parses.

    The symbol carries the year and month but not the day, so the day is derived
    from the same rule ``future_symbol`` uses to build it - keeping one source of
    truth for "which Tuesday is expiry" instead of two that can drift.
    """
    try:
        body = str(symbol).strip().upper()
        if not (body.startswith("NIFTY") and body.endswith("FUT")):
            return None
        token = body[len("NIFTY") : -len("FUT")]
        month_name = token[-3:]
        months = {name: number for number, name in _MONTH_NAMES.items()}
        month = months.get(month_name)
        if month is None:
            return None
        year = 2000 + int(token[:-3])
    except (ValueError, IndexError):
        return None
    return date(year, month, _last_weekday_of_month(year, month, EXPIRY_WEEKDAY))


def expiry_label(symbol: str) -> Optional[str]:
    """Human form of a contract's expiry, e.g. ``Tue 24 Nov 2026``.

    The symbol encodes only the year and month, so the day comes from the same
    last-Tuesday rule that builds the symbol. A label derived any other way could
    disagree with the contract it is describing.
    """
    expiry = contract_expiry(symbol)
    if expiry is None:
        return None
    return f"{expiry:%a %d %b %Y}"


def rollover_at(now: Optional[datetime] = None) -> datetime:
    """Moment the front month stops trading: the last Tuesday's 15:30 close.

    NSE Nifty monthly futures expire on the last Tuesday of the month, and the
    contract stops trading at the 15:30 close. A position still open at that
    instant has no venue, so this is the deadline for rolling forward - the same
    timestamp ``nearest_monthly_expiry`` uses to decide which month is current.
    """
    return nearest_monthly_expiry(now)


def rollover_status(
    now: Optional[datetime] = None,
    open_symbol: Optional[str] = None,
) -> dict[str, Any]:
    """How close the front month is to expiry, for the dashboard and alerts.

    ``rolled`` means the contract named by ``open_symbol`` has *stopped trading* -
    the last Tuesday's 15:30 close has passed - which is the state in which an
    open position has no venue left and needs a human decision.

    It cannot be derived from ``nearest_monthly_expiry`` alone. That function
    rolls the calendar forward at the close, so the contract it returns is always
    still tradeable and a "has it expired" test against it is never true. The
    deadline is therefore the expiry of the contract that is *currently*
    front-month, which is the previous month's once the close has passed.

    ``open_symbol`` is what makes ``rolled`` meaningful. The engine always trades
    the current front month, so the only contract that can be expired is one
    carried over from before the close; without it there is nothing to be late
    for, and ``rolled`` is reported as False.
    """
    moment = now or datetime.now(IST)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST)
    # The contract the engine trades *now*, and the one it will trade next. The
    # first already accounts for the early rollover window, so inside the window
    # `current_symbol` is next month's and `expiry` is that contract's expiry.
    current_symbol = future_symbol(moment)
    next_symbol = future_symbol(moment, months_ahead=1)
    # The contract that is literally still trading, which is what "expiring" and
    # "in the rollover window" are measured against - not the one the engine has
    # already moved to.
    calendar_expiry = nearest_monthly_expiry(moment)
    days_to_calendar_expiry = (calendar_expiry.date() - moment.date()).days
    # The expiry of the contract named by `current_symbol`, which is what the
    # countdown points at. Inside the rollover window that is next month's.
    expiry = (
        calendar_expiry
        if contract_expiry(current_symbol) == calendar_expiry.date()
        else _expiry_after(calendar_expiry)
    )

    open_expiry = contract_expiry(open_symbol) if open_symbol else None
    # A contract stops trading at the 15:30 close on its expiry day, so the same
    # date is only expired once that instant has passed.
    open_expired = bool(open_expiry is not None and _expiry_has_passed(open_expiry, moment))
    return {
        "current_symbol": current_symbol,
        "next_symbol": next_symbol,
        "expiry": expiry.isoformat(timespec="seconds"),
        "expiry_date": expiry.date().isoformat(),
        "expiry_label": expiry_label(current_symbol),
        "next_expiry_label": expiry_label(next_symbol),
        "seconds_to_expiry": round((expiry - moment).total_seconds()),
        "days_to_expiry": (expiry.date() - moment.date()).days,
        "expiring_today": calendar_expiry.date() == moment.date(),
        # True once the engine has moved to the next month, which happens
        # ROLLOVER_DAYS_BEFORE_EXPIRY days before the old contract expires.
        "in_rollover_window": days_to_calendar_expiry <= ROLLOVER_DAYS_BEFORE_EXPIRY,
        "rollover_days": ROLLOVER_DAYS_BEFORE_EXPIRY,
        "calendar_symbol": f"NIFTY{calendar_expiry:%y}{_MONTH_NAMES[calendar_expiry.month]}FUT",
        "calendar_expiry_date": calendar_expiry.date().isoformat(),
        "days_to_calendar_expiry": days_to_calendar_expiry,
        "open_symbol": open_symbol or None,
        "open_expiry": open_expiry.isoformat() if open_expiry else None,
        "open_expiry_label": expiry_label(open_symbol) if open_symbol else None,
        "rolled": open_expired,
        # A leg whose contract has stopped trading has no venue left, so it is an
        # expiry to resolve by hand rather than something the bot can trade out
        # of. New entries are blocked until it is squared off: opening the next
        # month beside it would be two contracts at once, which is the double
        # exposure the order manager refuses anyway.
        "entries_blocked": open_expired,
    }


def future_symbol(
    now: Optional[datetime] = None,
    months_ahead: int = 0,
    *,
    rollover_days: int = ROLLOVER_DAYS_BEFORE_EXPIRY,
) -> str:
    """Trading symbol for the Nifty futures contract (0 = current month).

    Must match the symbols in Kite's NFO instrument dump exactly, because the
    result is looked up there to resolve an instrument token.

    ``rollover_days`` moves the switch earlier than the expiry itself: with the
    default of 5, the last five days of the contract's life already resolve to
    the *next* month. That is what makes "roll over on the next signal" work -
    the engine simply stops naming the expiring contract, so a signal inside the
    window opens the new month and the old position is closed by the normal flip
    path rather than being carried into an expiry.

    Pass ``rollover_days=0`` for the contract that is literally still trading
    (the expiry calendar itself), which is what the rollover status and the
    expiry gate need.
    """
    expiry = nearest_monthly_expiry(now)
    if rollover_days > 0:
        moment = now or datetime.now(IST)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=IST)
        # Compare whole days: the window is a date range, not a countdown, so a
        # signal at 09:20 and one at 15:20 on the same day agree.
        if (expiry.date() - moment.date()).days <= rollover_days:
            expiry = _expiry_after(expiry)
    for _ in range(max(0, months_ahead)):
        expiry = _expiry_after(expiry)
    return f"NIFTY{expiry:%y}{_MONTH_NAMES[expiry.month]}FUT"


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
        # When each symbol's cached price was last confirmed by the broker. The
        # stop check needs this: a price that is hours old must not be treated as
        # a live mark, or a position whose feed has died reads as "not breached"
        # and is silently left unprotected.
        self._price_at: dict[str, float] = {}
        self._last_quote_at: Optional[datetime] = None
        self._auth_error: Optional[str] = None
        self._session_cache: Optional[dict[str, Any]] = None
        self._session_checked_at = 0.0
        # Dashboard reads (profile, margins) are network round-trips that the
        # UI polls every 1.5s. Cache them briefly so a poll costs no I/O.
        self._profile_cache: Optional[dict[str, Any]] = None
        self._profile_checked_at = 0.0
        self._margins_cache: Optional[dict[str, Any]] = None
        self._margins_checked_at = 0.0
        # symbol -> (fetched_at, quote or None). A None entry is a cached failure.
        self._quote_cache: dict[str, tuple[float, Optional[dict[str, Any]]]] = {}
        # Broker round-trip samples, newest last. Bounded so a long-running
        # process cannot grow this without limit.
        self._latency_samples: list[float] = []
        self._latency_checked_at = 0.0
        self._latency_error: Optional[str] = None
        self._init_sdk()

    # -- SDK bootstrap ---------------------------------------------------
    def _init_sdk(self) -> None:
        self._kite = None
        self._auth_error = None
        # A new SDK/token invalidates every cached broker answer.
        self._profile_cache = None
        self._profile_checked_at = 0.0
        self._margins_cache = None
        self._margins_checked_at = 0.0
        self._quote_cache = {}
        self._session_cache = None
        self._session_checked_at = 0.0
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

    def profile(self, max_age: float = PROFILE_CACHE_SECONDS) -> dict[str, Any]:
        """Return the live Kite profile, or a synthetic one when unauthenticated.

        ``kite.profile()`` is a network round-trip and the dashboard polls it
        every 1.5s, so the result is cached for ``max_age`` seconds. Pass
        ``max_age=0`` to force a fresh check (used after a token change).
        """
        if self.sdk_ready and max_age > 0:
            with self._lock:
                cached = self._profile_cache
                checked_at = self._profile_checked_at
            if cached is not None and (time.time() - checked_at) < max_age:
                return cached

        result: dict[str, Any]
        if self.sdk_ready:
            try:
                data = self.kite.profile()
                result = {"ok": True, "mode": self.mode, **data}
            except TokenException as exc:
                self._auth_error = f"Access token invalid/expired: {exc}"
                result = self._profile_failure()
            except (KiteException, NetworkException) as exc:
                self._auth_error = f"Kite API error: {exc}"
                result = self._profile_failure()
            except Exception as exc:
                self._auth_error = f"Unexpected auth error: {exc}"
                result = self._profile_failure()
        else:
            result = self._profile_failure()

        with self._lock:
            self._profile_cache = result
            self._profile_checked_at = time.time()
        return result

    def _profile_failure(self) -> dict[str, Any]:
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

    def session_status(self, max_age: float = 30.0) -> dict[str, Any]:
        """Cached validity of the Kite session.

        ``profile()`` is a network round-trip, so an order path cannot call it on
        every signal. The result is cached briefly: long enough to keep order
        placement fast, short enough that a token expiring mid-session is caught
        within one engine cycle.
        """
        if not self.sdk_ready:
            return {"valid": False, "state": "unavailable", "error": self._auth_error}
        with self._lock:
            cached = self._session_cache
            checked_at = self._session_checked_at
        if cached is not None and (time.time() - checked_at) < max_age:
            return cached
        status = self.auth_status()
        result = {
            "valid": bool(status.get("authenticated")),
            "state": "valid" if status.get("authenticated") else "expired",
            "detail": status.get("detail"),
            "error": None if status.get("authenticated") else status.get("detail"),
        }
        with self._lock:
            self._session_cache = result
            self._session_checked_at = time.time()
        return result

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

    def front_month_symbol(
        self,
        now: Optional[datetime] = None,
        *,
        rollover_days: Optional[int] = None,
        months_ahead: int = 0,
    ) -> str:
        """Tradingsymbol of the contract LIVE should trade, from Kite's dump.

        Composed from ``future_symbol`` rather than picked by index so that LIVE
        and PAPER switch on the same rule: ``front_month_contract`` walks the
        dump in expiry order, which never rolls early, so using it to choose the
        contract made LIVE ride the front month to the cash close while PAPER had
        already moved on.

        The context is still pinned to the *calendar* front month, so the result
        is the calendar contract or the next one - never a third month. That
        matters because index 0 and index 1 of the dump coincide with the
        calendar months only while no historical expiry lingers as unexpired;
        deriving from the calendar makes the selection independent of that.

        Returns an empty string when the calendar month is not in the dump, which
        is how the caller detects a stale instrument cache rather than sending an
        order for a contract the broker does not list.
        """
        if rollover_days is None:
            rollover_days = ROLLOVER_DAYS_BEFORE_EXPIRY
        moment = now or datetime.now(IST)
        calendar_symbol = future_symbol(moment, rollover_days=0)
        available = {
            str(item.get("tradingsymbol"))
            for item in self.nifty_future_instruments()
            if item.get("tradingsymbol")
        }
        if calendar_symbol not in available:
            return ""
        return future_symbol(moment, months_ahead=months_ahead, rollover_days=rollover_days)

    # -- Market data -----------------------------------------------------
    def get_quote(self, symbol: str, max_age: float = QUOTE_CACHE_SECONDS) -> Optional[dict[str, Any]]:
        """Live LTP/OHLC for ``symbol``; ``None`` when unavailable.

        Cached for ``max_age`` seconds so the mark loop and the dashboard share
        one round-trip instead of each issuing its own. Failures are cached too:
        with an expired token every call would otherwise pay a doomed network
        round-trip (plus retries) on each mark tick. Pass ``max_age=0`` when an
        order needs the freshest price.
        """
        if not self.sdk_ready:
            return None
        if max_age > 0:
            with self._lock:
                cached = self._quote_cache.get(symbol)
            if cached is not None and (time.time() - cached[0]) < max_age:
                return cached[1]
        key = f"NFO:{symbol}"
        data: dict[str, Any] = {}
        for attempt in range(3):
            try:
                data = self.kite.quote([key])
                break
            except TokenException as exc:
                self._auth_error = f"Access token invalid/expired: {exc}"
                self._remember_quote_failure(symbol)
                return None
            except (KiteException, NetworkException) as exc:
                self._auth_error = str(exc)
                if attempt == 2:
                    log.warning("Quote failed for %s: %s", symbol, exc)
                    self._remember_quote_failure(symbol)
                    return None
                time.sleep(0.2 * (attempt + 1))
        payload = data.get(key)
        if not payload or payload.get("last_price") is None:
            self._remember_quote_failure(symbol)
            return None
        price = float(payload["last_price"])
        self._last_prices[symbol] = price
        self._price_at[symbol] = time.time()
        self._last_quote_at = datetime.now(IST)
        ohlc = payload.get("ohlc", {}) or {}
        # Depth is optional and, when present, drives the paper fill model. It is
        # passed through verbatim - the fill module owns its normalisation.
        depth = payload.get("depth")
        quote = {
            "symbol": symbol,
            "last_price": price,
            "open": ohlc.get("open"),
            "high": ohlc.get("high"),
            "low": ohlc.get("low"),
            "close": ohlc.get("close"),
            "volume": payload.get("volume"),
            "depth": depth if isinstance(depth, dict) else None,
            "timestamp": payload.get("last_trade_time") or payload.get("timestamp"),
            "source": "KITE",
        }
        with self._lock:
            self._quote_cache[symbol] = (time.time(), quote)
        return quote

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

    def _remember_quote_failure(self, symbol: str) -> None:
        """Cache a failed quote so a dead token is not retried on every tick."""
        with self._lock:
            self._quote_cache[symbol] = (time.time(), None)

    def get_ltp(self, symbol: str, max_age: float = QUOTE_CACHE_SECONDS) -> Optional[float]:
        quote = self.get_quote(symbol, max_age=max_age)
        if quote:
            return quote["last_price"]
        return self._last_prices.get(symbol)

    def latest_known_price(self, symbol: str) -> Optional[float]:
        """Last cached price for ``symbol``, or None if none was ever seen.

        Distinct from :meth:`get_ltp`, which goes to the quote endpoint first.
        This is the in-memory cache only, so it answers "what did the market last
        say about this contract" without a network call.
        """
        return self._last_prices.get(symbol)

    def remember_tick(self, symbol: str, price: float) -> None:
        """Cache the last known price for ``symbol``."""
        self._last_prices[symbol] = float(price)
        self._price_at[symbol] = time.time()

    def price_age(self, symbol: str) -> Optional[float]:
        """Seconds since ``symbol``'s cached price was last confirmed, or None.

        ``None`` means no price has ever been seen for the symbol. A large value
        means the cached price is stale and must not be used as a live mark.
        """
        stamp = self._price_at.get(symbol)
        if stamp is None:
            return None
        return max(0.0, time.time() - stamp)

    @property
    def last_quote_at(self) -> Optional[datetime]:
        return self._last_quote_at

    def data_source(self) -> str:
        return "KITE" if self._last_quote_at is not None else "CACHE"

    # -- Latency ---------------------------------------------------------
    def probe_latency(self, max_age: float = LATENCY_CACHE_SECONDS) -> dict[str, Any]:
        """Time one broker round-trip, cached so a 1.5s poll costs no I/O.

        ``kite.profile()`` is the cheapest authenticated call the SDK exposes,
        so it measures the path to Kite's API without pulling a quote or
        touching the order book. The number is wall-clock around the call: it
        includes TLS reuse, the broker's own processing and the return trip,
        which is what "how far is this box from Kite" actually means.

        Samples are kept so the dashboard can show a median rather than a
        single reading that a transient blip would misrepresent.
        """
        if not self.sdk_ready:
            return {"available": False, "reason": "Kite SDK not ready"}

        with self._lock:
            if max_age > 0 and (time.time() - self._latency_checked_at) < max_age:
                return self._latency_payload()

        started = time.perf_counter()
        try:
            self.kite.profile()
        except TokenException as exc:
            self._latency_error = f"Access token invalid/expired: {exc}"
            with self._lock:
                self._latency_checked_at = time.time()
            return self._latency_payload()
        except (KiteException, NetworkException) as exc:
            self._latency_error = f"Kite API error: {exc}"
            with self._lock:
                self._latency_checked_at = time.time()
            return self._latency_payload()
        except Exception as exc:  # pragma: no cover - defensive
            self._latency_error = f"Unexpected error: {exc}"
            with self._lock:
                self._latency_checked_at = time.time()
            return self._latency_payload()

        elapsed_ms = (time.perf_counter() - started) * 1000
        with self._lock:
            self._latency_error = None
            self._latency_samples.append(elapsed_ms)
            del self._latency_samples[:-LATENCY_SAMPLE_COUNT]
            self._latency_checked_at = time.time()
        return self._latency_payload()

    def _latency_payload(self) -> dict[str, Any]:
        """The latency block, built from whatever samples exist.

        Called with the lock held or from a path that already took it, so it
        reads the list directly rather than re-acquiring.
        """
        samples = list(self._latency_samples)
        if not samples:
            return {
                "available": False,
                "reason": self._latency_error or "no successful round-trip yet",
                "samples": 0,
            }
        ordered = sorted(samples)
        median = ordered[len(ordered) // 2]
        return {
            "available": True,
            "last_ms": round(samples[-1], 1),
            "median_ms": round(median, 1),
            "min_ms": round(ordered[0], 1),
            "max_ms": round(ordered[-1], 1),
            "samples": len(samples),
            "error": self._latency_error,
        }

    # -- Trading ---------------------------------------------------------
    def margins(self, max_age: float = MARGINS_CACHE_SECONDS) -> Optional[dict[str, Any]]:
        """Account margins, cached briefly because the dashboard polls them."""
        if not self.sdk_ready:
            return None
        if max_age > 0:
            with self._lock:
                cached = self._margins_cache
                checked_at = self._margins_checked_at
            if cached is not None and (time.time() - checked_at) < max_age:
                return cached
        try:
            data = self.kite.margins()
        except Exception as exc:
            log.warning("Margins lookup failed: %s", exc)
            return None
        with self._lock:
            self._margins_cache = data
            self._margins_checked_at = time.time()
        return data

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        price: Optional[float] = None,
        order_type: str = "MARKET",
        product: str = "NRML",
        tag: str = "niftybot",
        mode: Optional[str] = None,
    ) -> dict[str, Any]:
        """Place a real order (LIVE) or return a mocked acknowledgement (PAPER).

        ``mode`` is the *book's* mode, not the process's. This used to read only
        ``self.mode``, so once one book could be live while another stayed in
        paper, the paper book's order went to the broker for real: ``order_manager``
        had already decided the order was simulated and booked it as such, while
        this method submitted it. Defaulting to the client's mode keeps a caller
        that does not know about books working as before.
        """
        effective = (mode or self.mode).upper()
        if effective != LIVE:
            order_id = f"PAPER-{datetime.now(IST):%Y%m%d%H%M%S}-{random.randint(1000, 9999)}"
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "average_price": price,
                "mode": PAPER,
                "mocked": True,
                # The paper fill model runs against the visible order book when it
                # is available; the order path reads this back off the quote.
                "depth": (self.get_quote(symbol, max_age=0) or {}).get("depth"),
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
                # A market order with no protection band can fill far from the
                # touch in a thin book. Zerodha's own default is used when the
                # setting is unset, so this only overrides when configured.
                market_protection=(
                    int(settings.market_protection)
                    if settings.market_protection is not None
                    else None
                ),
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

        # Poll fast at first, then back off. A market order normally fills in
        # well under 200 ms, so a flat 0.5 s interval added up to half a second
        # of dead time to every entry - and this call blocks the engine thread,
        # so that delay also held up the stop check behind it.
        started = time.time()
        while time.time() < deadline:
            if update_history():
                return last
            elapsed = time.time() - started
            time.sleep(0.1 if elapsed < 2.0 else 0.5)
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

    def order_book(self, force: bool = False) -> list[dict[str, Any]]:
        """Every order the broker knows about today, newest first.

        This is the authority for "did my order actually fill?". The local
        ``orders`` table records what the bot *sent*; only this says what the
        exchange *did*. Reconciliation compares the two, so a timeout, a
        partial fill or a rejection that arrived after the bot stopped polling
        is discovered rather than assumed.

        ``force`` reads the broker even in PAPER, for the same reason
        :meth:`positions` does: the readiness gate and the startup reconcile run
        before the mode switch.

        Raises rather than returning an empty list when the broker cannot be
        read. An empty list means "no orders today", which is a very different
        fact from "I could not ask", and treating the second as the first is how
        a bot concludes it is flat when it is not.
        """
        if self.is_paper and not force:
            return []
        if not self.sdk_ready:
            raise RuntimeError(self._auth_error or "KiteConnect is not initialised")
        rows = self.kite.orders() or []
        return sorted(
            rows,
            key=lambda row: str(row.get("order_timestamp") or ""),
            reverse=True,
        )

    def order_status(self, order_id: str, force: bool = False) -> Optional[dict[str, Any]]:
        """The broker's latest state for one order, or ``None`` if unreadable.

        ``None`` is deliberately distinct from a status: a failed read must not
        be mistaken for "still open" or for "cancelled". The caller decides, and
        the safe decision on ``None`` is to leave the order unresolved.
        """
        if not order_id:
            return None
        try:
            history = self.kite.order_history(order_id)
        except Exception as exc:
            log.warning("Order history read failed for %s: %s", order_id, exc)
            return None
        if not history:
            return None
        latest = dict(history[-1])
        latest["status"] = str(latest.get("status") or "").upper().replace(" ", "_")
        return latest

    def modify_order(
        self,
        order_id: str,
        *,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        order_type: Optional[str] = None,
        trigger_price: Optional[float] = None,
        mode: Optional[str] = None,
    ) -> dict[str, Any]:
        """Modify a pending order. PAPER returns a simulated acknowledgement.

        Only the fields supplied are changed, so a caller can move a price
        without restating the quantity. A modification is verified by reading
        the order back: Kite accepts a modify request and can still reject it
        asynchronously, so the acknowledgement alone is not proof.
        """
        effective = (mode or self.mode).upper()
        if effective != LIVE:
            return {
                "order_id": order_id,
                "status": "COMPLETE",
                "mode": PAPER,
                "mocked": True,
                "message": "Simulated modify (paper mode)",
            }
        kwargs: dict[str, Any] = {"variety": self.kite.VARIETY_REGULAR, "order_id": order_id}
        if quantity is not None:
            kwargs["quantity"] = int(quantity)
        if price is not None:
            kwargs["price"] = float(price)
        if order_type is not None:
            kwargs["order_type"] = getattr(self.kite, f"ORDER_TYPE_{order_type.upper()}")
        if trigger_price is not None:
            kwargs["trigger_price"] = float(trigger_price)
        try:
            self.kite.modify_order(**kwargs)
        except Exception as exc:
            log.error("Order modify failed for %s: %s", order_id, exc)
            return {"order_id": order_id, "status": "REJECTED", "mode": LIVE,
                    "mocked": False, "message": str(exc)}
        verified = self.order_status(order_id, force=True) or {}
        return {
            "order_id": order_id,
            "status": verified.get("status") or "OPEN",
            "average_price": verified.get("average_price"),
            "filled_quantity": int(verified.get("filled_quantity") or 0),
            "mode": LIVE,
            "mocked": False,
            "message": verified.get("status_message") or "Modify accepted",
        }

    def cancel_order(self, order_id: str, mode: Optional[str] = None) -> dict[str, Any]:
        """Cancel a pending order and confirm the cancellation with the broker.

        The confirmation matters: a cancel request that the exchange rejects
        leaves the order live, and reporting success would let the bot believe
        it had removed an order that can still fill.
        """
        effective = (mode or self.mode).upper()
        if effective != LIVE:
            return {
                "order_id": order_id,
                "status": "CANCELLED",
                "mode": PAPER,
                "mocked": True,
                "message": "Simulated cancel (paper mode)",
            }
        try:
            self.kite.cancel_order(
                variety=self.kite.VARIETY_REGULAR, order_id=order_id
            )
        except Exception as exc:
            log.error("Order cancel failed for %s: %s", order_id, exc)
            return {"order_id": order_id, "status": "OPEN", "mode": LIVE,
                    "mocked": False, "message": f"Cancel failed: {exc}"}
        verified = self.order_status(order_id, force=True) or {}
        status = verified.get("status") or "CANCELLED"
        return {
            "order_id": order_id,
            "status": status,
            "filled_quantity": int(verified.get("filled_quantity") or 0),
            "average_price": verified.get("average_price"),
            "mode": LIVE,
            "mocked": False,
            "message": verified.get("status_message") or "Cancel accepted",
        }

    def positions(self, force: bool = False) -> list[dict[str, Any]]:
        """Broker net positions.

        ``force`` reads the broker even in PAPER. Reconciliation needs this: the
        LIVE readiness gate runs *before* the mode switch, so a check that
        short-circuits on ``is_paper`` can never pass and LIVE can never be
        armed. Only the reconciliation paths pass ``force``; nothing on the
        trading path does, so PAPER still never touches the broker's book.
        """
        if self.is_paper and not force:
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

    def required_margin(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        price: Optional[float] = None,
        product: str = "NRML",
        force: bool = False,
    ) -> Optional[float]:
        """Exchange margin for one prospective order, or ``None`` when unknown.

        Uses Kite's order-margin API, which accounts for SPAN, exposure and any
        margin benefit from existing positions. Returning ``None`` rather than a
        guessed figure is deliberate: the static ``LOT_MARGIN`` setting cannot
        know SPAN, and a wrong number is worse than an explicit "unknown".

        ``force=True`` reads the figure even while the bot is in PAPER. The
        readiness report runs *before* the switch - a gate that only works once
        you are already live is not a gate - and without this the margin check
        could never see a number, so it reported "unknown" on every account and
        the one precondition that protects real money was never verified.
        """
        if (self.is_paper and not force) or not self.sdk_ready:
            return None
        order: dict[str, Any] = {
            "exchange": self.kite.EXCHANGE_NFO,
            "tradingsymbol": symbol,
            "transaction_type": transaction_type.upper(),
            "variety": self.kite.VARIETY_REGULAR,
            "product": product.upper(),
            "order_type": "MARKET",
            "quantity": int(quantity),
            "readonly": True,
        }
        if price:
            order["price"] = float(price)
        try:
            result = self.kite.basket_order_margins([order], consider_positions=True)
        except TokenException as exc:
            self._auth_error = f"Access token invalid/expired: {exc}"
            return None
        except Exception as exc:
            log.warning("Margin check failed for %s: %s", symbol, exc)
            return None
        if not isinstance(result, dict):
            return None
        # The response nests the total; take the most specific figure present.
        for key in ("total", "final", "orders"):
            value = result.get(key)
            if isinstance(value, dict):
                for inner in ("total", "margin", "span", "exposure"):
                    if value.get(inner) is not None:
                        try:
                            return float(value[inner])
                        except (TypeError, ValueError):
                            continue
            elif value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return None

    def net_position(self, symbol: str, force: bool = False) -> Optional[int]:
        """Broker's net quantity for ``symbol``, or ``None`` when unreadable.

        A hard ``None`` (rather than 0) matters: assuming flat when the broker
        call failed is how a bot ends up doubling a real position.

        ``force`` reads the broker even in PAPER, for the LIVE readiness gate -
        see :meth:`positions`. Without it the gate always saw ``None`` and
        blocked arming LIVE, which is the one thing it exists to permit.
        """
        if self.is_paper and not force:
            return None
        try:
            rows = self.positions(force=force)
        except Exception as exc:
            log.warning("Position read failed for %s: %s", symbol, exc)
            return None
        total = 0
        found = False
        for row in rows:
            if str(row.get("tradingsymbol") or "") != symbol:
                continue
            found = True
            try:
                total += int(row.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
        return total if found else 0


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
    "contract_expiry",
    "expiry_label",
    "rollover_at",
    "rollover_status",
    "IST",
]
