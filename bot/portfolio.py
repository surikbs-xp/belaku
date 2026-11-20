"""The portfolio-level rules that no single strategy can enforce on its own.

Two strategies trading the same front-month NIFTY future are not independent at
the broker. Zerodha reports **one netted position per contract**, so:

* A long in one book and a short in the other is *not* two positions. It is one
  flat position, and the margin, the P&L and the risk are all different from what
  summing the two books suggests.
* The existing broker reconciliation compares ledger net to broker net. With
  opposing books those net to the same number by construction, so it can no
  longer detect drift.

Neither problem is visible to a :class:`strategy_run.StrategyRun`, because each
one only knows its own book. They are portfolio questions, so they live here, in
the one component that can see every run at once.

Three rules, in the order they are checked:

1. **No opposing legs in one contract.** If run A is long the contract and run B
   would go short in it, the order is refused. This is the rule that stops the
   netting from silently cancelling two real positions.
2. **Combined size within the limit.** ``MAX_OPEN_LOTS`` is a portfolio budget,
   not a per-strategy one. Two runs at the per-run limit would otherwise double
   it.
3. **A run may not exceed its own budget.**

What this module deliberately does not do: place orders, write the ledger, or
choose a strategy. It answers one question - *may this run take this action?* -
and explains itself when the answer is no.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import database as db
from config import PAPER, get_execution_mode, settings
from order_manager import BUY, FLAT, SELL

log = logging.getLogger("portfolio")


@dataclass(frozen=True)
class Verdict:
    """Whether one intended action may proceed, and why not if it may not."""

    allowed: bool
    reason: str = ""
    # The combined exposure after the action, for the dashboard and the log.
    projected_lots: int = 0

    def __bool__(self) -> bool:  # allows `if verdict:`
        return self.allowed

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "projected_lots": self.projected_lots,
        }


class PortfolioAggregator:
    """Cross-strategy exposure rules. Stateless; reads the ledger each call.

    Stateless on purpose. Two engines, or the API and the engine, can hold their
    own instance and still agree, because the only input is the ledger - there is
    no cached exposure to drift out of date.
    """

    def __init__(self, *, max_lots: Optional[int] = None) -> None:
        self._max_lots = max_lots

    @property
    def max_lots(self) -> int:
        """The whole-portfolio lot budget."""
        if self._max_lots is not None:
            return int(self._max_lots)
        return int(getattr(settings, "max_open_lots", 4))

    # -- book reads ------------------------------------------------------
    def open_positions(self, mode: Optional[str] = None) -> list[dict[str, Any]]:
        return db.get_open_positions(mode=mode or get_execution_mode())

    def run_lots(self, strategy_id: str, symbol: str, mode: Optional[str] = None) -> int:
        """Signed lots ``strategy_id`` holds in ``symbol``, in units' own terms.

        Reported in *units*, not lots: the lot size is a property of the contract
        and the caller already knows it, so dividing here would only invite two
        different lot sizes to be silently mixed.
        """
        return sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in self.open_positions(mode)
            if position["strategy_id"] == strategy_id and position["symbol"] == symbol
        )

    def total_units(self, symbol: str, mode: Optional[str] = None) -> int:
        """Signed units across every strategy - the broker's own view."""
        return sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in self.open_positions(mode)
            if position["symbol"] == symbol
        )

    def against(
        self, symbol: str, direction: int, mode: Optional[str] = None,
        exclude: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Open legs in ``symbol`` that point the *other* way from ``direction``.

        The test is against the position that would exist, not against one that
        already does: an intended long is opposed by a held short, even though
        nothing in the ledger currently contradicts itself.
        """
        return [
            position
            for position in self.open_positions(mode)
            if position["symbol"] == symbol
            and int(position["position_key"]) == -direction
            and position["strategy_id"] != exclude
        ]

    def opposing(self, symbol: str, mode: Optional[str] = None) -> list[dict[str, Any]]:
        """Open rows in ``symbol`` from more than one direction, if any exist."""
        rows = [p for p in self.open_positions(mode) if p["symbol"] == symbol]
        longs = [p for p in rows if int(p["position_key"]) == 1]
        shorts = [p for p in rows if int(p["position_key"]) == -1]
        if longs and shorts:
            return rows
        return []

    # -- the rules -------------------------------------------------------
    def authorise(
        self,
        strategy_id: str,
        action: str,
        symbol: str,
        lots: int,
        *,
        lot_size: int,
        mode: Optional[str] = None,
    ) -> Verdict:
        """May ``strategy_id`` take ``action`` in ``symbol`` right now?

        ``lots`` is the size the run wants, in lots. ``lot_size`` converts to the
        units the ledger stores.
        """
        mode = mode or get_execution_mode()
        action = (action or "").strip().upper()

        # Exits are never blocked. A position that cannot be closed is far worse
        # than one that cannot be opened, and this holds for the portfolio rules
        # too: refusing a square-off would trap every book at once.
        if action in {FLAT, "PANIC"}:
            return Verdict(True, "exits are never blocked")

        if action not in {BUY, SELL}:
            return Verdict(True)

        direction = 1 if action == BUY else -1

        # 1. No opposing legs in one contract.
        clash = self.against(symbol, direction, mode, exclude=strategy_id)
        if clash:
            other = sorted({
                f"{p['strategy_id']} {'long' if int(p['position_key']) == 1 else 'short'}"
                for p in clash
            })
            if other:
                return Verdict(
                    False,
                    f"{symbol} already carries opposing legs ({', '.join(other)}). "
                    "Zerodha nets these into one position, so the second leg would "
                    f"cancel the first instead of being a separate trade. Close one "
                    f"book before {action} in the other.",
                )

        # 2. Combined size stays inside the portfolio budget.
        current_units = self.total_units(symbol, mode)
        wanted_units = direction * abs(lots) * lot_size
        own_units = self.run_lots(strategy_id, symbol, mode)
        # What the total becomes once this run moves from its own position to the
        # target: the other books are unchanged, so replace only this run's share.
        projected = current_units - own_units + wanted_units
        projected_lots = abs(projected) // max(1, lot_size)

        if projected_lots > self.max_lots:
            return Verdict(
                False,
                f"{projected_lots} lots would exceed MAX_OPEN_LOTS={self.max_lots}; "
                "the limit is a portfolio budget shared by every strategy",
                projected_lots=projected_lots,
            )

        # 3. A run's own budget never exceeds the portfolio's.
        if abs(lots) > self.max_lots:
            return Verdict(
                False,
                f"{abs(lots)} lots exceeds MAX_OPEN_LOTS={self.max_lots}",
                projected_lots=projected_lots,
            )

        return Verdict(True, "", projected_lots=projected_lots)

    # -- reporting -------------------------------------------------------
    def exposure(self, mode: Optional[str] = None) -> dict[str, Any]:
        """Per-strategy and netted exposure, side by side.

        Both are shown because they answer different questions. The per-strategy
        rows say what each book believes it holds; ``netted`` says what the broker
        will actually report, and a divergence between them is the fact an
        operator needs to see.
        """
        mode = mode or get_execution_mode()
        rows = self.open_positions(mode)
        by_strategy: dict[str, dict[str, Any]] = {}
        by_symbol: dict[str, int] = {}
        for position in rows:
            key = position["strategy_id"] or db.DEFAULT_STRATEGY_ID
            entry = by_strategy.setdefault(
                key, {"strategy_id": key, "symbols": {}, "gross_units": 0, "net_units": 0}
            )
            units = int(position["quantity"]) * int(position["position_key"])
            symbol = position["symbol"]
            entry["symbols"][symbol] = entry["symbols"].get(symbol, 0) + units
            entry["gross_units"] += abs(units)
            entry["net_units"] += units
            by_symbol[symbol] = by_symbol.get(symbol, 0) + units

        return {
            "mode": mode,
            "strategies": sorted(by_strategy.values(), key=lambda r: r["strategy_id"]),
            "netted": by_symbol,
            "gross_units": sum(r["gross_units"] for r in by_strategy.values()),
            "net_units": sum(by_symbol.values()),
            "max_lots": self.max_lots,
            "conflicts": sorted(
                symbol for symbol in by_symbol if len({
                    p["strategy_id"] for p in rows if p["symbol"] == symbol
                }) > 1
            ),
        }


_aggregator: Optional[PortfolioAggregator] = None


def get_aggregator() -> PortfolioAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = PortfolioAggregator()
    return _aggregator


def reset_aggregator() -> None:
    """Drop the cached instance. For tests that change ``MAX_OPEN_LOTS``."""
    global _aggregator
    _aggregator = None


__all__ = [
    "PortfolioAggregator",
    "Verdict",
    "get_aggregator",
    "reset_aggregator",
]
