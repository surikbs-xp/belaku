"""Risk limits: the kill-switch, the daily loss cap and the portfolio budget.

The strategy owns its own exit. A position is held for as long as SuperTrend and
Parabolic SAR agree, and closed on the first bar they stop agreeing - so there is
no per-trade stop-loss or profit target *in this module*. Those live in
:mod:`stops`, which the engine applies per book.

What remains here is the set of limits that are about the *portfolio* rather than
about an individual trade:

* the open-lot budget, shared by every book (see :mod:`portfolio`);
* the operator's own kill-switch (``OrderManager.pause``);
* the **daily realised-loss cap**, which stops new entries once the day's
  settled loss reaches ``MAX_DAILY_LOSS``.

The daily cap blocks *entries* only. It never flattens an open position: doing so
would turn a bad day into a realised one at the worst possible moment, and the
position already has its own protective stop. ``MAX_DAILY_LOSS=0`` disables it.
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


def daily_loss_limit() -> float:
    """The configured daily realised-loss cap in rupees. 0 disables it."""
    try:
        return float(getattr(settings, "max_daily_loss", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def daily_loss_state(
    mode: str, strategy_id: Optional[str] = None
) -> dict[str, Any]:
    """Today's realised PnL against the cap, for the gate and the dashboard.

    ``breached`` is only ever True when a positive cap is configured *and* the
    day's realised loss has reached it. A zero cap reports ``enabled: False``
    rather than "breached by 0", so a disabled limit cannot read as a tripped
    one.
    """
    limit = daily_loss_limit()
    try:
        realised = db.daily_realised_pnl(mode, strategy_id=strategy_id)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not read today's realised PnL: %s", exc)
        realised = 0.0
    loss = max(0.0, -realised)
    enabled = limit > 0
    return {
        "enabled": enabled,
        "limit": limit,
        "realised_pnl": round(realised, 2),
        "loss": round(loss, 2),
        "remaining": round(max(0.0, limit - loss), 2) if enabled else None,
        "breached": bool(enabled and loss >= limit),
        "mode": mode,
        "strategy_id": strategy_id,
    }


def daily_loss_breach(
    mode: str, strategy_id: Optional[str] = None
) -> Optional[Breach]:
    """A :class:`Breach` when the daily cap has been hit, else ``None``."""
    state = daily_loss_state(mode, strategy_id=strategy_id)
    if not state["breached"]:
        return None
    return Breach(
        kind="DAILY_LOSS",
        detail=(
            f"Daily loss limit reached: Rs {state['loss']:,.0f} lost today "
            f"(limit Rs {state['limit']:,.0f}). New entries are blocked until "
            "the next session."
        ),
    )


def limits() -> dict[str, Any]:
    """Configured limits, for the dashboard and the readiness report."""
    return {
        "max_open_lots": settings.max_open_lots,
        "max_daily_loss": daily_loss_limit(),
        "exit": "SIGNAL+STOP",
    }
