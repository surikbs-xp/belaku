"""Realistic fill simulation for the Nifty futures paper leg.

PAPER mode mocks *orders*, not market data. But the fill it books used to be
naive: the future's last traded price shifted by one fixed tick
(:func:`order_manager.apply_slippage`), with no regard for the order book. A real
market order does not fill at "LTP minus a tick"; it walks the book:

* a **BUY** lifts the *ask* side (best ask first, then worse levels),
* a **SELL** hits the *bid* side (best bid first, then worse levels),

and the average fill is the volume-weighted price across every level the order
consumed. For NIFTY futures - deep, with hundreds of lots at the touch - a one to
four lot order almost always fills entirely at the best level; this module models
exactly that, and only diverges from a single-touch fill when the visible book is
too thin to cover the order.

Kite's ``quote`` payload includes market depth (``depth.buy`` / ``depth.sell``,
five levels each). When it is present it is the authority; when it is missing the
module falls back to the same adverse-tick approximation the rest of the paper
books use, so a quote without depth behaves exactly as it did before.

This module is used only for the *futures* paper leg. The GIFT Nifty index book
fills at the completed bar's close (an index has no order book), and the shadow
paper mirror grades itself against the broker's actual fill, so neither uses it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

BUY = "BUY"
SELL = "SELL"

# The authoritative default mirrors ``order_manager._round_tick``: NIFTY futures
# tick at 0.05 points. Kept local so this module stays decoupled and cannot be
# pulled into a circular import with :mod:`order_manager`.
DEFAULT_TICK_SIZE = 0.05


def _round_tick(price: float, tick: float) -> float:
    """Round ``price`` to the nearest ``tick``, floored at a sensible minimum."""
    step = float(tick or DEFAULT_TICK_SIZE)
    return round(round(price / step) * step, 2)


def ltp_fill(price: float, side: str, tick_size: float, slippage_ticks: float) -> float:
    """Adverse-tick fallback used when no order book is available.

    Identical to :func:`order_manager.apply_slippage`: the reference price shifted
    by ``slippage_ticks`` ticks in the adverse direction, then rounded to the
    tick. Keeping the formula in step matters - a fill with no depth must grade
    the same as every other paper book's fill, or the comparison between venues
    would measure the pricing model instead of the venues.
    """
    offset = float(slippage_ticks or 0.0) * float(tick_size or 0.0)
    adjusted = price + offset if side == BUY else price - offset
    return _round_tick(adjusted, tick_size)


def is_degenerate(open_: float, high: float, low: float, close: float) -> bool:
    """True for a bar with no range at all - a placeholder, not a traded bar.

    A real bar almost never has ``high == low``. When it does, the "close" is not
    a market level but whatever single tick happened to seed the bar, so pricing
    an order off it books the fill somewhere the market never traded. This is the
    shape that made a GIFT entry print 84 points above the true close.
    """
    try:
        values = [float(v) for v in (open_, high, low, close)]
    except (TypeError, ValueError):
        return True
    if any(v <= 0 for v in values):
        return True
    return max(values) == min(values)


@dataclass(frozen=True)
class FillResult:
    """Outcome of simulating one market order against the order book."""

    price: float
    filled_quantity: int
    partial: bool
    levels_touched: int = 0
    source: str = "ltp"  # "book" | "ltp"


def _levels(depth: Optional[dict[str, Any]], side: str) -> list[tuple[float, int]]:
    """Normalise one side of a Kite ``depth`` payload into (price, qty) rows.

    A BUY consumes the *sell* (ask) side, walked from best (lowest) to worst; a
    SELL consumes the *buy* (bid) side, walked from best (highest) to worst.
    Rows with non-positive price or quantity are dropped rather than raising, so
    a malformed quote cannot stop the order path.
    """
    if not isinstance(depth, dict):
        return []
    book = depth.get("sell") if side == BUY else depth.get("buy")
    if not isinstance(book, (list, tuple)):
        return []
    levels: list[tuple[float, int]] = []
    for row in book:
        if not isinstance(row, dict):
            continue
        try:
            price = float(row.get("price"))
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0 and quantity > 0:
            levels.append((price, quantity))
    levels.sort(key=lambda item: item[0], reverse=(side == SELL))
    return levels


def simulate_fill(
    symbol: str,
    side: str,
    quantity: int,
    *,
    ltp: float,
    depth: Optional[dict[str, Any]] = None,
    tick_size: float = DEFAULT_TICK_SIZE,
    slippage_ticks: float = 1.0,
) -> FillResult:
    """Simulate one market order against the visible book.

    Returns the volume-weighted average fill price across the levels the order
    consumed. When the book does not cover the full quantity, the result is a
    *partial* fill - ``filled_quantity`` is the portion that filled, and the
    remainder is left for the caller to report honestly (no auto-retry, no
    invented price for liquidity that was not shown). With no depth at all the
    order fills in full at the adverse-tick approximation.
    """
    try:
        qty = int(quantity)
        reference = float(ltp)
    except (TypeError, ValueError):
        return FillResult(0.0, 0, False)

    levels = _levels(depth, side)
    if not levels or qty <= 0:
        return FillResult(
            ltp_fill(reference, side, tick_size, slippage_ticks),
            max(qty, 0),
            False,
            0,
            "ltp",
        )

    remaining = qty
    notional = 0.0
    touched = 0
    for price, available in levels:
        if remaining <= 0:
            break
        take = min(remaining, available)
        notional += price * take
        remaining -= take
        touched += 1

    filled = qty - remaining
    if filled <= 0:
        return FillResult(
            ltp_fill(reference, side, tick_size, slippage_ticks),
            qty,
            False,
            0,
            "ltp",
        )
    return FillResult(
        _round_tick(notional / filled, tick_size),
        filled,
        remaining > 0,
        touched,
        "book",
    )


__all__ = [
    "BUY",
    "SELL",
    "DEFAULT_TICK_SIZE",
    "FillResult",
    "ltp_fill",
    "simulate_fill",
]
