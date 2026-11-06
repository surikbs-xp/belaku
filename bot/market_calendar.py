"""NSE trading-holiday calendar.

The market clock in :mod:`kite_client` only knows the weekday and the 09:15-15:30
session, so on a trading holiday it would report the market open and the LIVE
engine would fire orders that the exchange rejects. This module supplies the
official closure dates so that cannot happen.

Source is NSE's own holiday master (``/api/holiday-master?type=trading``), which
returns every segment including ``FO``. It is fetched once a day at most, cached
in memory and on disk, and the cache is kept indefinitely: the payload always
contains the full calendar year, so a stale cache is still correct for which
dates are holidays even after the API becomes unreachable.

Any failure is non-fatal. When no calendar is available the engine reports that
it cannot vouch for the session instead of silently assuming the market is open.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

NSE_HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
NSE_REFERER = "https://www.nseindia.com/resources/exchange-communication-holidays"
# NSE serves the JSON only to browser-shaped requests.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_REFERER,
}
# The F&O segment is what this bot trades; it is the most inclusive relevant list.
SEGMENT = "FO"
REFRESH_SECONDS = 24 * 60 * 60
FETCH_TIMEOUT = 10.0

_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d")


def default_cache_path() -> Path:
    """Cache location, overridable so the container can point it at its volume."""
    override = os.getenv("HOLIDAY_CACHE_PATH")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent / "data" / "nse_holidays.json"


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _extract_holidays(payload: Any) -> dict[str, str]:
    """Map ``YYYY-MM-DD`` to the exchange's description for the F&O segment."""
    if not isinstance(payload, dict):
        return {}
    rows = payload.get(SEGMENT)
    if not isinstance(rows, list):
        return {}
    holidays: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        day = _parse_date(row.get("tradingDate"))
        if day is None:
            continue
        holidays[day.isoformat()] = str(row.get("description") or "Exchange holiday").strip()
    return holidays


class HolidayCalendar:
    """Trading holidays, refreshed from NSE at most once per day."""

    def __init__(self, cache_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        self.cache_path = Path(cache_path) if cache_path else default_cache_path()
        self._holidays: dict[str, str] = {}
        self._fetched_at: Optional[datetime] = None
        self._source = "none"
        self._error: Optional[str] = None
        self._load_cache()
        self.refresh()

    # -- state ----------------------------------------------------------
    @property
    def holidays(self) -> dict[str, str]:
        with self._lock:
            return dict(self._holidays)

    def holiday_name(self, day: date) -> Optional[str]:
        with self._lock:
            return self._holidays.get(day.isoformat())

    def is_holiday(self, day: date) -> bool:
        return self.holiday_name(day) is not None

    @property
    def available(self) -> bool:
        """False when no calendar has ever been loaded."""
        with self._lock:
            return bool(self._holidays)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "available": bool(self._holidays),
                "source": self._source,
                "count": len(self._holidays),
                "fetched_at": self._fetched_at.isoformat(timespec="seconds") if self._fetched_at else None,
                "error": self._error,
                "cache_path": str(self.cache_path),
            }

    # -- loading --------------------------------------------------------
    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        holidays = raw.get("holidays") if isinstance(raw, dict) else None
        if isinstance(holidays, dict) and holidays:
            with self._lock:
                self._holidays = {str(k): str(v) for k, v in holidays.items()}
                self._source = "cache"

    def _write_cache(self, holidays: dict[str, str]) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(
                    {"source": NSE_HOLIDAY_URL, "fetched_at": datetime.now(IST).isoformat(), "holidays": holidays},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("Cannot write holiday cache %s: %s", self.cache_path, exc)

    def refresh(self, force: bool = False) -> dict[str, Any]:
        """Fetch the calendar if it is missing or a day old. Never raises."""
        with self._lock:
            fresh = (
                self._fetched_at is not None
                and (datetime.now(IST) - self._fetched_at) < timedelta(seconds=REFRESH_SECONDS)
            )
            if fresh and not force:
                return self.status()

        request = urllib.request.Request(NSE_HOLIDAY_URL, headers=_BROWSER_HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                if self._holidays:
                    self._source = "cache"
            log.warning("NSE holiday refresh failed (%s); using cached calendar", exc)
            return self.status()

        holidays = _extract_holidays(payload)
        if not holidays:
            with self._lock:
                self._error = "NSE returned no F&O holidays"
            log.warning("NSE holiday payload had no F&O entries; keeping the previous calendar")
            return self.status()

        with self._lock:
            self._holidays = holidays
            self._fetched_at = datetime.now(IST)
            self._source = "nse"
            self._error = None
        self._write_cache(holidays)
        log.info("NSE holiday calendar loaded: %d F&O closure dates", len(holidays))
        return self.status()


_calendar: Optional[HolidayCalendar] = None
_calendar_lock = threading.Lock()


def get_calendar() -> HolidayCalendar:
    global _calendar
    with _calendar_lock:
        if _calendar is None:
            _calendar = HolidayCalendar()
        return _calendar


def reset_calendar() -> None:
    """Drop the singleton. Used by tests and by a manual refresh."""
    global _calendar
    with _calendar_lock:
        _calendar = None


def holiday_name(day: Optional[date] = None) -> Optional[str]:
    return get_calendar().holiday_name(day or datetime.now(IST).date())


def is_trading_holiday(day: Optional[date] = None) -> bool:
    return get_calendar().is_holiday(day or datetime.now(IST).date())


__all__ = [
    "HolidayCalendar",
    "get_calendar",
    "holiday_name",
    "is_trading_holiday",
    "reset_calendar",
]
