"""Shadow paper book: mirror the live position so the paper model can be graded.

PAPER mode and LIVE mode are never exercised on the same bar. A paper session
and a live session see different quotes, different hours and different fills, so
comparing their P&L across time says almost nothing. What is worth knowing is
narrower and harder to get:

    *On this signal, at this moment, what fill would the paper model have
    invented, and how far is that from what the broker actually gave me?*

This module answers exactly that. While ``EXECUTION_MODE=LIVE`` and
``PAPER_MIRROR=1``, every reconciled change in the live book is reproduced in a
paper book kept in its **own SQLite file**:

* **The live fill is the reference price.** The paper fill is built from the
  price the broker actually filled at, then pushed through this bot's own slippage
  model. The resulting difference is therefore the *model's* error - its slippage
  and cost assumptions - not a market-data difference. Live fills arrive through
  :meth:`PaperMirror.note_fill` from the order path; a matching fill older than
  :data:`FILL_TOLERANCE_SECONDS` is booked from the current quote and labelled
  ``live_fill_stale``, because an entry priced minutes later is not evidence
  about that fill.
* **Quantities come from the live book, not from lot logic.** The mirror moves to
  the same signed quantity the live ledger holds, so a partial fill or a manual
  square-off is copied rather than reconstructed.
* **Separate file, no shared state.** The shadow book cannot touch the live
  ledger, and deleting its file loses only the shadow history. Its account starts
  from ``PAPER_MIRROR_BALANCE``.
* **Costs use the same model as PAPER**, deliberately: the point of the exercise
  is to measure that model, so substituting anything else would hide the error
  being looked for.

The mirror is read-only with respect to trading: it can never place, size or close
a real order. It also cannot stop the bot - every failure is recorded on
:meth:`PaperMirror.status` and surfaced on the dashboard, because a shadow book
that quietly dies is worse than one that was never switched on.

Matching an observed fill to a side is deliberately loose: the newest live fill
of the matching transaction type for that symbol, within the tolerance window.
Anything stricter (an id handshake through the order manager) buys precision the
comparison does not need, since the live book and this one are reconciled from the
same position state.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import database as db
from config import PAPER, get_execution_mode, settings
from order_manager import apply_slippage, estimate_charges

log = logging.getLogger("paper_mirror")

# How recently the live fill must have been observed for the mirror to price its
# own fill from it. Past this the entry is booked from the current quote and
# flagged, so the dataset distinguishes "compared against a real fill" from
# "compared against a quote from a few minutes later".
FILL_TOLERANCE_SECONDS = 180.0

# Live fills kept for matching. The window above is minutes wide, so a handful is
# plenty; the cap only stops unbounded growth over a long session.
_MAX_RECENT_FILLS = 50

# Recorded in the shadow book's ``source`` column, which is how the dashboard and
# any later analysis tell mirror rows apart from hand-entered paper trades.
SOURCE = "MIRROR"


class PaperMirror:
    """Keeps a paper book in step with the live positions, and compares them."""

    def __init__(
        self,
        path: Optional[str] = None,
        mirror: Optional[bool] = None,
        lot_size: Optional[int] = None,
    ) -> None:
        self._path = path if path is not None else settings.paper_mirror_db_path
        # None means "follow configuration"; a bool pins it for tests.
        self._mirror_override = mirror
        self._lot_size_override = lot_size
        self._lock = threading.RLock()
        self._initialised = False
        self._recent_fills: list[dict[str, Any]] = []
        self._prices: dict[str, float] = {}
        self._prices_at: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[str] = None
        self._last_reconcile_at: Optional[str] = None
        self._reconciles = 0
        self._failures = 0
        self._consecutive_failures = 0

    # -- state -----------------------------------------------------------
    @property
    def configured(self) -> bool:
        """Whether ``PAPER_MIRROR`` (or the test override) asks for a mirror."""
        if self._mirror_override is not None:
            return bool(self._mirror_override)
        return bool(settings.paper_mirror)

    @property
    def enabled(self) -> bool:
        """Mirroring is only meaningful while trading live."""
        return self.configured and get_execution_mode() == "LIVE"

    @property
    def path(self) -> str:
        return self._path

    def disabled_reason(self) -> str:
        """Why the mirror is not running, for the dashboard."""
        if not self.configured:
            return "PAPER_MIRROR is off"
        if get_execution_mode() != "LIVE":
            return "only mirrors while EXECUTION_MODE=LIVE"
        return ""

    # -- fills -----------------------------------------------------------
    def note_fill(
        self,
        *,
        symbol: str,
        transaction_type: str,
        quantity: int,
        price: float,
        kind: str = "ENTRY",
    ) -> None:
        """Record a live fill so the mirror can price its own fill against it."""
        if not self.enabled:
            return
        try:
            fill = {
                "at": time.time(),
                "symbol": str(symbol).strip().upper(),
                "transaction_type": str(transaction_type).strip().upper(),
                "quantity": int(quantity),
                "price": float(price),
                "kind": kind,
            }
        except (TypeError, ValueError):
            return
        with self._lock:
            self._recent_fills.append(fill)
            del self._recent_fills[:-_MAX_RECENT_FILLS]

    def _match_fill(self, symbol: str, side: str) -> tuple[Optional[tuple[float, float]], bool]:
        """Newest matching fill as ``(price, age)``, plus whether any match exists."""
        now = time.time()
        with self._lock:
            fills = list(self._recent_fills)
        for fill in reversed(fills):
            if fill["symbol"] != symbol or fill["transaction_type"] != side:
                continue
            age = now - fill["at"]
            if age <= FILL_TOLERANCE_SECONDS:
                return (float(fill["price"]), age), True
            return None, True
        return None, False

    # -- prices ----------------------------------------------------------
    def mark(self, prices: dict[str, float]) -> None:
        """Store fresh prices and mark the shadow book to market.

        Called from the mark loop, which already prices the execution future for
        the live book, so the mirror adds no API calls of its own.
        """
        if not self.enabled:
            return
        with self._lock:
            if prices:
                self._prices.update({k: float(v) for k, v in prices.items()})
                self._prices_at = time.time()
            prices_now = dict(self._prices)
        if not prices_now:
            return
        try:
            self._ensure_initialised()
            db.mark_open_positions(PAPER, prices_now, path=self._path)
            positions = db.get_open_positions(self._path, mode=PAPER)
            metrics = db.get_performance_metrics(
                path=self._path,
                open_positions=positions,
                prices=prices_now,
                mode=PAPER,
            )
            db.record_equity(
                metrics["equity"],
                metrics["balance"],
                metrics["unrealized_pnl"],
                path=self._path,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"Mirror mark failed: {exc}")

    def _price(self, symbol: str) -> Optional[float]:
        """Current price from the last mark, or a direct quote as a fallback."""
        with self._lock:
            price = self._prices.get(symbol)
        if price:
            return float(price)
        try:
            from kite_client import get_client

            quote = get_client().get_ltp(symbol)
        except Exception:  # pragma: no cover - defensive
            quote = None
        return float(quote) if quote else None

    def _refresh_prices(self, symbol: str) -> None:
        """Ask the feed and the book for a price before giving up on an entry.

        The mark loop only publishes the execution future once the live book has
        a position, so an entry can arrive before any price is known. Without
        this the mirror would drop a position it should be tracking. Costs one
        quote at the moment of an entry, not per poll.
        """
        try:
            from kite_client import get_client

            quote = get_client().get_ltp(symbol)
        except Exception:  # pragma: no cover - network
            quote = None
        if not quote:
            try:
                from gift_nifty import get_feed

                status = get_feed().status()
                quote = status.get("exec_last_price") or status.get("last_candle_close")
            except Exception:  # pragma: no cover - defensive
                quote = None
        try:
            price = float(quote) if quote else 0.0
        except (TypeError, ValueError):
            price = 0.0
        if price > 0:
            with self._lock:
                self._prices[symbol] = price
                self._prices_at = time.time()

    # -- reconciliation --------------------------------------------------
    def reconcile(self) -> Optional[dict[str, Any]]:
        """Move the shadow book to the live book's net position, per symbol.

        Safe to call often: it compares quantities and does nothing when they
        already agree, so the engine can call it on every poll.
        """
        if not self.enabled:
            return None
        try:
            live_positions = db.get_open_positions(mode="LIVE")
        except Exception as exc:
            self._note_error(f"Could not read the live book: {exc}")
            return None

        want: dict[str, int] = {}
        source: dict[str, dict[str, Any]] = {}
        for position in live_positions:
            symbol = str(position["symbol"]).strip().upper()
            want[symbol] = want.get(symbol, 0) + (
                int(position["quantity"]) * int(position["position_key"])
            )
            source[symbol] = position
        want = {symbol: qty for symbol, qty in want.items() if qty != 0}

        try:
            self._ensure_initialised()
            have = self._mirror_quantities()
            failures_before = self._failures
            for symbol in sorted(set(want) | set(have)):
                target = want.get(symbol, 0)
                current = have.get(symbol, 0)
                if target != current:
                    self._sync_symbol(symbol, current, target, source.get(symbol))
        except Exception as exc:
            self._note_error(f"Mirror reconcile failed: {exc}")
            with self._lock:
                self._consecutive_failures += 1
            # Report the failure rather than returning nothing, so the dashboard
            # can show it - a mirror that fails silently looks identical to one
            # with nothing to do.
            return self.status()

        with self._lock:
            self._reconciles += 1
            self._last_reconcile_at = db.iso()
            self._finish_pass(failures_before)
        return self.status()

    def _finish_pass(self, failures_before: int) -> None:
        """Update health after one pass, whether or not it raised.

        A pass is only clean when nothing in it recorded a failure. Clearing the
        error unconditionally would erase real reports - a position skipped for
        being under one lot, for instance - on the very next poll.
        """
        if self._failures > failures_before:
            self._consecutive_failures += 1
        else:
            self._consecutive_failures = 0
            self._last_error = None
            self._last_error_at = None

    def _mirror_quantities(self) -> dict[str, int]:
        """Net signed quantity of each open shadow position."""
        net: dict[str, int] = {}
        for position in db.get_open_positions(self._path, mode=PAPER):
            symbol = str(position["symbol"]).strip().upper()
            net[symbol] = net.get(symbol, 0) + (
                int(position["quantity"]) * int(position["position_key"])
            )
        return {symbol: qty for symbol, qty in net.items() if qty != 0}

    def _sync_symbol(
        self,
        symbol: str,
        current: int,
        target: int,
        live_position: Optional[dict[str, Any]],
    ) -> None:
        """Close what the shadow book holds, then open the live quantity.

        The live book only reaches a new state from a signal, a flip or a
        square-off, so closing and reopening expresses every transition without
        needing partial-quantity edits.
        """
        if current != 0:
            self._close(symbol, current, live_position)
        if target != 0:
            self._open(symbol, target, live_position)

    def _allocate_price(
        self, symbol: str, side: str
    ) -> tuple[Optional[float], Optional[float], str]:
        """Paper fill price for ``side``, with the fill lag and how it was priced."""
        matched, any_match = self._match_fill(symbol, side)
        if matched is not None:
            price, lag = matched
            # The model's own slippage is applied on top of the broker's actual
            # fill, so the gap between the two is the model's error.
            return apply_slippage(price, side), lag, "live_fill"
        quote = self._price(symbol)
        if quote is None:
            # An entry can reach the mirror before the mark loop has ever
            # published a price, so ask once before giving up on the position.
            self._refresh_prices(symbol)
            quote = self._price(symbol)
        if quote is None:
            return None, None, "unavailable"
        basis = "live_fill_stale" if any_match else "quote"
        return apply_slippage(quote, side), None, basis

    def _open(
        self, symbol: str, target: int, live_position: Optional[dict[str, Any]]
    ) -> None:
        side = "BUY" if target > 0 else "SELL"
        lot_size = self._lot_size()
        quantity = (abs(target) // lot_size) * lot_size
        if quantity <= 0:
            self._note_error(
                f"Mirror skipped {symbol}: live quantity {target} is under one lot"
            )
            return
        price, lag, basis = self._allocate_price(symbol, side)
        if price is None:
            self._note_error(f"Mirror could not price the {side} entry for {symbol}")
            return

        notes = {
            "basis": basis,
            "live_entry_time": (live_position or {}).get("entry_time"),
            "live_entry_price": (live_position or {}).get("entry_price"),
            "fill_lag_seconds": None if lag is None else round(lag, 3),
        }
        position_id = db.open_position(
            symbol,
            quantity if target > 0 else -quantity,
            price,
            path=self._path,
            mode=PAPER,
            source=SOURCE,
            strategy="Surikbs (mirror)",
            notes=json.dumps(notes),
        )
        db.record_order(
            path=self._path,
            symbol=symbol,
            transaction_type=side,
            quantity=quantity,
            price=price,
            order_type=settings.order_type,
            product=settings.product,
            status="COMPLETE",
            mode=PAPER,
            source=SOURCE,
            position_id=position_id,
            message=f"MIRROR ENTRY | basis {basis}"
            + (f" | fill lag {lag:.1f}s" if lag is not None else ""),
        )
        log.info(
            "Mirror entry %s %s %d @ %.2f (basis=%s%s) to match live",
            side,
            symbol,
            quantity,
            price,
            basis,
            f", lag {lag:.1f}s" if lag is not None else "",
        )

    def _close(
        self, symbol: str, current: int, live_position: Optional[dict[str, Any]]
    ) -> None:
        side = "SELL" if current > 0 else "BUY"
        open_positions = [
            position
            for position in db.get_open_positions(self._path, mode=PAPER)
            if str(position["symbol"]).strip().upper() == symbol
        ]
        if not open_positions:
            return
        price, lag, basis = self._allocate_price(symbol, side)
        if price is None:
            self._note_error(f"Mirror could not price the {side} exit for {symbol}")
            return

        for position in open_positions:
            quantity = int(position["quantity"])
            entry = float(position["entry_price"])
            # Costs are charged per position on the round trip, matching how the
            # live manager charges a close.
            buy_turnover = price * quantity if side == "BUY" else entry * quantity
            sell_turnover = price * quantity if side == "SELL" else entry * quantity
            fees = estimate_charges(buy_turnover, sell_turnover)
            trade = db.close_position(
                position["id"],
                price,
                path=self._path,
                fees=fees,
                exit_reason="MIRROR",
                quantity=quantity,
            )
            db.record_order(
                path=self._path,
                symbol=symbol,
                transaction_type=side,
                quantity=quantity,
                price=price,
                order_type=settings.order_type,
                product=settings.product,
                status="COMPLETE",
                mode=PAPER,
                source=SOURCE,
                position_id=position["id"],
                message=f"MIRROR EXIT | basis {basis} | net {trade['net_pnl']:+.2f}"
                + (f" | fill lag {lag:.1f}s" if lag is not None else ""),
            )
            log.info(
                "Mirror exit %s %s %d @ %.2f (basis=%s, net %+.2f) to match live",
                side,
                symbol,
                quantity,
                price,
                basis,
                trade["net_pnl"],
            )

    def _lot_size(self) -> int:
        """Live lot size when it can be read, so the mirror matches the contract."""
        if self._lot_size_override is not None:
            return max(1, int(self._lot_size_override))
        try:
            from order_manager import get_order_manager

            return max(1, int(get_order_manager().current_lot_size()))
        except Exception:  # pragma: no cover - defensive
            return max(1, int(settings.lot_size))

    def _ensure_initialised(self) -> None:
        if self._initialised:
            return
        # The account row is created by the first connection, so a file that does
        # not exist yet is the only safe moment to apply PAPER_MIRROR_BALANCE -
        # doing it later would wipe a shadow history someone meant to keep.
        first_time = not Path(self._path).exists()
        db.init_db(self._path)
        if first_time:
            db.reset_account(self._path, initial_balance=settings.paper_mirror_balance)
        self._initialised = True

    def _note_error(self, message: str) -> None:
        """Record a failure without ever raising into the caller's loop."""
        with self._lock:
            self._failures += 1
            self._last_error = message
            self._last_error_at = db.iso()
        log.warning("%s", message)

    # -- reporting -------------------------------------------------------
    def comparison(self) -> Optional[dict[str, Any]]:
        """Live and shadow results side by side, with the gap between them."""
        if not self.enabled:
            return None
        self._ensure_initialised()
        prices = dict(self._prices)
        live = db.get_performance_metrics(
            open_positions=db.get_open_positions(mode="LIVE"),
            prices=prices,
            mode="LIVE",
        )
        mirror = db.get_performance_metrics(
            path=self._path,
            open_positions=db.get_open_positions(self._path, mode=PAPER),
            prices=prices,
            mode=PAPER,
        )
        summary_live = _summarise(live)
        summary_mirror = _summarise(mirror)
        summary_live["realized_total"] = round(float(live.get("realized_total") or 0.0), 2)
        summary_mirror["realized_total"] = round(float(mirror.get("realized_total") or 0.0), 2)
        return {
            "live": summary_live,
            "mirror": summary_mirror,
            "net_pnl_gap": round(
                summary_mirror["realized_total"] - summary_live["realized_total"], 2
            ),
        }

    def status(self) -> dict[str, Any]:
        """Mirror health and its view of the world, for ``/api/status``."""
        enabled = self.enabled
        payload: dict[str, Any] = {
            "enabled": enabled,
            "configured": self.configured,
            "reason": "" if enabled else self.disabled_reason(),
            "db_path": self._path,
            "last_reconcile_at": self._last_reconcile_at,
            "reconciles": self._reconciles,
            "failures": self._failures,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "fills_seen": len(self._recent_fills),
            "prices_at": self._prices_at,
            "live_net": {},
            "mirror_net": {},
            "in_sync": None,
        }
        if not enabled:
            return payload

        self._ensure_initialised()
        try:
            live_positions = db.get_open_positions(mode="LIVE")
            mirror_positions = db.get_open_positions(self._path, mode=PAPER)
            live_net = _net_by_symbol(live_positions)
            mirror_net = _net_by_symbol(mirror_positions)
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"Mirror status failed: {exc}")
            payload["last_error"] = self._last_error
            payload["last_error_at"] = self._last_error_at
            return payload

        payload.update(
            {
                "live_net": live_net,
                "mirror_net": mirror_net,
                "in_sync": live_net == mirror_net,
                "live_positions": len(live_positions),
                "mirror_positions": len(mirror_positions),
            }
        )
        try:
            payload["comparison"] = self.comparison()
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"Mirror comparison failed: {exc}")
        return payload


def _net_by_symbol(positions: list[dict[str, Any]]) -> dict[str, int]:
    net: dict[str, int] = {}
    for position in positions:
        symbol = str(position["symbol"]).strip().upper()
        net[symbol] = net.get(symbol, 0) + (
            int(position["quantity"]) * int(position["position_key"])
        )
    return {symbol: qty for symbol, qty in net.items() if qty != 0}


def _summarise(metrics: dict[str, Any]) -> dict[str, Any]:
    """The handful of comparison fields the dashboard actually shows."""
    return {
        "realized_total": 0.0,
        "realized_today": round(float(metrics.get("realized_today") or 0.0), 2),
        "unrealized_pnl": round(float(metrics.get("unrealized_pnl") or 0.0), 2),
        "trades": int(metrics.get("total_trades") or 0),
        "win_rate": metrics.get("win_rate"),
        "equity": round(float(metrics.get("equity") or 0.0), 2),
        "balance": round(float(metrics.get("balance") or 0.0), 2),
    }


_mirror: Optional[PaperMirror] = None
_mirror_lock = threading.Lock()


def get_mirror() -> PaperMirror:
    global _mirror
    with _mirror_lock:
        if _mirror is None:
            _mirror = PaperMirror()
        return _mirror


__all__ = ["PaperMirror", "get_mirror", "FILL_TOLERANCE_SECONDS", "SOURCE"]
