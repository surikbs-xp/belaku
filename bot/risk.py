"""Risk limits: the kill-switch and the portfolio budget.

The strategy owns its own exit. A position is held for as long as SuperTrend and
Parabolic SAR agree, and closed on the first bar they stop agreeing - so there is
no stop-loss, no trailing stop, and no profit target in this module. Those were
removed deliberately: a fixed stop exits on a price the strategy never looked at,
which is a different strategy, not a safety net around this one.

The session loss cap was removed too. It blocked new entries once the day's
realised loss reached a limit, which sounds prudent but is a second, hidden
strategy: it changes when the bot trades based on a number the strategy never
looked at. The remaining limits are the ones that are about the *portfolio*
rather than about an individual trade - the open-lot budget and the operator's
own kill-switch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import database as db
from config import settings
from database import now_ist

log = logging.getLogger("risk")

@dataclass
class Breach:
    """A limit that has been exceeded, and what to do about it."""

    kind: str
    detail: str
    symbol: Optional[str] = None
    points: Optional[float] = None
    # The exact row that breached, when the limit names one. The daily cap does
    # not - it blocks entries rather than closing a position - so this is None
    # for every breach this module can currently produce.
    position_id: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "symbol": self.symbol,
            "points": self.points,
            "position_id": self.position_id,
        }


def limits() -> dict[str, Any]:
    """Configured limits, for the dashboard and the readiness report.

    There is no per-trade stop to report: the strategy's own signal is the exit.
    """
    return {
        "max_open_lots": settings.max_open_lots,
        "exit": "SIGNAL",
    }
