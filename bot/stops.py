"""Per-strategy stop-loss and trailing-stop arithmetic.

Two strategies trade the same NIFTY future, each with its own protective stop:

* **GIFT NIFTY 15m** - 50-point initial stop, 5-point trail, widening to a
  30-point trail once the trade is 50 points in profit.
* **NIFTY 60m** - 200-point initial stop, 100-point trail once the trade is 200
  points in profit.

The rules are identical in shape and differ only in numbers, so they live in one
place and are parameterised per strategy rather than duplicated. Every level is
computed from the **futures execution price** - the fill the broker gave and the
live LTP the mark loop reads - never from the signal series. The signal series is
an index; a stop measured on it would sit at a level the position can never be
filled at, and the index/future basis would silently move the stop.

Two invariants hold for every level this module produces:

* the stop only ever moves in the profitable direction, and
* it never widens. A trailing stop that can loosen is not a stop; it is a
  suggestion, and it would let a trade that was once protected run unprotected.

Nothing here places an order. :func:`evaluate` answers "has this position
breached its stop, and what is the new level", and the caller decides what to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# A long is protected below the entry, a short above it. ``position_key`` is the
# ledger's own sign convention: +1 long, -1 short.
LONG = 1
SHORT = -1


@dataclass(frozen=True)
class StopRules:
    """One strategy's stop configuration, in index points.

    ``trail_after`` is the profit (in points) at which the trail switches from
    ``trail_points`` to ``trail_points_after``. A zero ``trail_points`` disables
    trailing entirely and leaves only the initial stop.
    """

    initial_points: float
    trail_points: float = 0.0
    trail_after: float = 0.0
    trail_points_after: float = 0.0

    @property
    def enabled(self) -> bool:
        return self.initial_points > 0

    def trail_for(self, profit_points: float) -> float:
        """The trail distance that applies at ``profit_points`` of open profit.

        The switch is a step, not a ramp: below ``trail_after`` the initial trail
        applies, at or above it the wider trail applies. ``trail_after`` of zero
        means the wider trail is in force from the first tick.
        """
        if self.trail_points <= 0:
            return 0.0
        if self.trail_points_after > 0 and profit_points >= self.trail_after:
            return self.trail_points_after
        return self.trail_points

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_points": self.initial_points,
            "trail_points": self.trail_points,
            "trail_after": self.trail_after,
            "trail_points_after": self.trail_points_after,
            "enabled": self.enabled,
        }


@dataclass
class StopState:
    """A position's stop levels, as stored on the ledger row."""

    initial: Optional[float]
    current: Optional[float]
    best_price: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_stop": self.initial,
            "stop": self.current,
            "best_price": self.best_price,
        }


def initial_stop(entry_price: float, position_key: int, rules: StopRules) -> Optional[float]:
    """The stop placed at entry, or ``None`` when stops are disabled."""
    if not rules.enabled:
        return None
    if position_key == LONG:
        return entry_price - rules.initial_points
    return entry_price + rules.initial_points


def _best_price(entry_price: float, position_key: int, mark: float, best: Optional[float]) -> float:
    """The most favourable price seen since entry: the high for a long, the low
    for a short. Seeded at the entry so the first mark can only improve it."""
    if best is None:
        best = entry_price
    return max(best, mark) if position_key == LONG else min(best, mark)


def _trailed_stop(
    entry_price: float,
    position_key: int,
    best: float,
    rules: StopRules,
) -> Optional[float]:
    """The stop implied by the best price reached, before the never-widen clamp.

    The trail only counts once the position is **in profit**. Before that the
    initial stop is the protection, and a trail measured from a best price at or
    behind the entry would be *tighter* than the configured initial stop - which
    silently replaced it. That is not a cosmetic difference: with a 5-point trail
    and a 50-point initial stop, the position was protected at 5 points from the
    first tick and the 50-point stop never applied at all. The same defect put
    the 60-minute book's 200-point stop at 100.

    So the rule is: the initial stop holds until the trail has locked in profit,
    and from then on the trail only ever tightens (see :func:`_tighten`). Every
    configured number then means what it says - the initial stop is the loss the
    position can take, and the trail is what it keeps once it is winning.

    ``trail_after`` is the profit at which the trail **activates**, not merely the
    point at which it widens. The two strategies specify it that way: the 60m book
    has a 200-point stop and a 100-point trail that activates at +200, so before
    +200 the position is protected by the 200-point stop and nothing else. Without
    this gate the 100-point trail engaged from the first profitable tick and
    replaced the 200-point stop with a 100-point one - the position was half as
    protected as configured, and the 200-point stop never applied at all.
    """
    if not rules.enabled or rules.trail_points <= 0:
        return None
    profit = (best - entry_price) * position_key
    if profit <= 0:
        # Not yet in profit: the initial stop is the protection.
        return None
    if rules.trail_after > 0 and profit < rules.trail_after:
        # Not yet at the activation level: the initial stop still holds.
        return None
    distance = rules.trail_for(profit)
    if distance <= 0:
        return None
    return best - distance if position_key == LONG else best + distance


def _tighten(current: Optional[float], candidate: Optional[float], position_key: int) -> Optional[float]:
    """Keep whichever stop is more protective.

    A long's stop only rises, a short's only falls. This is the never-widen rule:
    a candidate that would loosen the stop is discarded, so a trade that was once
    protected can never become unprotected.
    """
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate) if position_key == LONG else min(current, candidate)


def evaluate(
    *,
    entry_price: float,
    position_key: int,
    mark: float,
    rules: StopRules,
    current_stop: Optional[float] = None,
    best_price: Optional[float] = None,
) -> tuple[StopState, bool]:
    """Advance a position's stop to ``mark`` and report whether it breached.

    Returns ``(state, breached)``. ``state`` carries the levels to persist;
    ``breached`` is True when ``mark`` has reached or passed the stop that was in
    force *before* this update - the stop is a level the market trades through,
    so touching it counts.

    The breach is tested against the pre-update stop, not the tightened one: a
    mark that both advances the trail and sits beyond it is a breach of the level
    the position was actually protected by.
    """
    if not rules.enabled:
        return StopState(None, None, best_price), False

    best = _best_price(entry_price, position_key, mark, best_price)
    stop = current_stop
    if stop is None:
        stop = initial_stop(entry_price, position_key, rules)

    breached = False
    if stop is not None:
        breached = mark <= stop if position_key == LONG else mark >= stop

    trailed = _trailed_stop(entry_price, position_key, best, rules)
    stop = _tighten(stop, trailed, position_key)
    return StopState(initial=None, current=stop, best_price=best), breached
