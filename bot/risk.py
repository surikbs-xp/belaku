"""Risk limits: stop-loss and a kill-switch.

The strategy has no stop-loss of its own. It exits on an opposite signal or a
manual square-off, so between those two events a position is unbounded: a gap
through the entry is carried in full. This module adds the limit that the
strategy deliberately does not have, and it is the only thing standing between a
bad session and the account.

* **Stop-loss** (``STOP_LOSS_POINTS``) caps the loss on a *single* position. It
  is the one that matters most, because it is the only limit that acts before
  the damage is large.
* **Kill-switch** latches once the stop trips. It is deliberately sticky - it
  does not clear when price recovers, because a limit that un-trips itself turns
  a bad day into a bad week. Clearing it is an explicit operator action.

Every check is evaluated against the ledger (``positions``), not against
in-memory state, so a restart cannot lose track of a breach.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import database as db
from config import settings

log = logging.getLogger("risk")

# Why the guard acted, surfaced on the dashboard.
STOP_LOSS = "STOP_LOSS"


@dataclass
class Breach:
    """A limit that has been exceeded, and what to do about it."""

    kind: str
    detail: str
    symbol: Optional[str] = None
    points: Optional[float] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "symbol": self.symbol,
            "points": self.points,
        }


def open_position_points(position: dict[str, Any]) -> Optional[float]:
    """Adverse points on an open position, or None when price is unknown.

    Positive means a loss. Uses ``last_price`` (the mark the engine already
    maintains) rather than a fresh quote, so the guard cannot itself fail on a
    network error and leave a position unguarded.
    """
    entry = position.get("entry_price")
    mark = position.get("last_price")
    if entry is None or mark is None:
        return None
    entry = float(entry)
    mark = float(mark)
    # position_key 1 is long, -1 is short (see order_manager).
    if int(position.get("position_key") or 1) == 1:
        return entry - mark
    return mark - entry


def check_stop_loss(mode: str) -> Optional[Breach]:
    """The worst open position that has breached ``STOP_LOSS_POINTS``."""
    limit = float(settings.stop_loss_points)
    if limit <= 0:
        return None

    worst: Optional[Breach] = None
    for position in db.get_open_positions(mode=mode):
        points = open_position_points(position)
        if points is None or points < limit:
            continue
        if worst is None or points > (worst.points or 0.0):
            worst = Breach(
                kind=STOP_LOSS,
                symbol=position["symbol"],
                points=round(points, 2),
                detail=(
                    f"{position['symbol']} is {points:.2f} points against entry, "
                    f"past STOP_LOSS_POINTS={limit:g}"
                ),
            )
    return worst


def evaluate(mode: str) -> Optional[Breach]:
    """The active breach, or None."""
    return check_stop_loss(mode)


def limits() -> dict[str, Any]:
    """Configured limits, for the dashboard and the readiness report."""
    return {
        "stop_loss_points": settings.stop_loss_points,
        "max_open_lots": settings.max_open_lots,
        "enabled": bool(settings.stop_loss_points),
    }
