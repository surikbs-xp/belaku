"""GIFT Nifty index book: paper-trade the signal source itself.

Belaku's design is built on a split - **signals come from GIFT Nifty, orders go to
the NFO Nifty future** - because GIFT Nifty is a non-tradeable index on Kite
(``NSEIX:GIFT NIFTY``, quote-only). That split is deliberate, but it leaves an
open question the futures book cannot answer:

    *If the index were tradeable, would trading it directly beat trading the
    future on the same signals?*

This module answers it by running the **same signal stream** through a second,
independent paper book priced on the GIFT Nifty level:

* **One signal, two books.** The engine settles both books on the same closed
  bar and the same verdict. Neither is derived from the other, so the comparison
  is between venues, not between two copies of one decision.
* **GIFT Nifty can never go live.** There is no GIFT Nifty futures contract on
  Kite, so this book has no LIVE path at all. That is why it lives separately
  from :mod:`order_manager` rather than as another branch inside it - there is no
  code path here that can reach a broker.
* **Its own database.** ``GIFT_BOOK_DB_PATH`` keeps the index ledger out of the
  futures ledger, so deleting it loses only the index history and neither book
  can corrupt the other.
* **GIFT-hours semantics.** The index trades ~20 h/day, so a signal that fires at
  02:00 is a real fill here at the completed GIFT close - precisely the case where
  the futures book has to defer. This is the most interesting difference between
  the two books, and the reason the comparison is worth running.

Fills are the completed bar's close plus this bot's slippage model, and costs use
the same :func:`order_manager.estimate_charges` stack as the paper futures book,
so the two books differ in *venue and hours*, not in accounting.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import database as db
from config import PAPER, settings
from gift_nifty import GIFT_KEY
from order_manager import BUY, FLAT, SELL, apply_slippage, estimate_charges

log = logging.getLogger("gift_book")


def _book_alert(
    kind: str,
    detail: str,
    severity: str = "info",
    fields: Optional[dict[str, Any]] = None,
) -> None:
    """Send an index-book notification. Never raises, never blocks the book.

    The index book is a separate ledger marked off GIFT Nifty itself, so its
    entries and exits are their own events - not duplicates of the futures book.
    """
    try:
        import notifier

        if not notifier.configured():
            log.info(
                "GIFT book alert %s skipped: ALERT_ENABLED is off or the channel "
                "is not configured",
                kind,
            )
            return
        delivered = notifier.send(
            {
                "kind": kind,
                "detail": detail,
                "severity": severity,
                "source": "belaku",
                "mode": PAPER,
                "symbol": "NSEIX:GIFT NIFTY",
                "at_ist": db.iso(),
                "fields": {k: v for k, v in (fields or {}).items() if v not in (None, "")},
            }
        )
        log.info("GIFT book alert %s delivered=%s", kind, delivered)
    except Exception:  # pragma: no cover - defensive
        log.exception("GIFT book alert failed")

# The index book's rows are stamped with this so the dashboard, and any later
# analysis, can tell them from futures rows without knowing the symbol layout.
SOURCE = "GIFT_BOOK"

# Position sizing is in *units*, because an index has no contract. The default
# mirrors one futures lot so the two books are comparable at the same notional
# exposure rather than one silently being larger.
DEFAULT_UNITS = 65

# The id stamped on a book built without one - the pre-multi-strategy default,
# so an existing deployment's rows keep their attribution.
DEFAULT_STRATEGY = "Surikbs"


class GiftBook:
    """A paper book on the GIFT Nifty index, driven by the engine's signals."""

    def __init__(
        self,
        path: Optional[str] = None,
        enabled: Optional[bool] = None,
        units: Optional[int] = None,
        strategy_id: Optional[str] = None,
    ) -> None:
        # Which strategy owns this book. One GIFT book per strategy, not one for
        # the process: two GIFT-signalled strategies on different timeframes are
        # two independent books, and a shared ledger let one read the other's
        # position as its own and close it.
        self.strategy_id = (strategy_id or DEFAULT_STRATEGY).strip() or DEFAULT_STRATEGY
        if path is not None:
            self._path = path
        elif strategy_id:
            # A book per strategy, in its own file, so deleting one loses only
            # that strategy's index history.
            stem = self.strategy_id.replace("/", "-").replace(" ", "-")
            self._path = f"{settings.gift_book_db_path}.{stem}"
        else:
            self._path = settings.gift_book_db_path
        self._enabled_override = enabled
        self._units_override = units
        self._lock = threading.RLock()
        self._initialised = False
        self._last_price: Optional[float] = None
        self._last_price_at: Optional[float] = None
        self._last_action: Optional[str] = None
        self._last_action_at: Optional[str] = None
        self._last_position_id: Optional[int] = None
        self._fills = 0
        self._last_error: Optional[str] = None
        self._last_error_at: Optional[str] = None
        self._failures = 0
        self._skipped = 0

    # -- state -----------------------------------------------------------
    @property
    def enabled(self) -> bool:
        """Whether ``GIFT_BOOK`` (or the test override) asks for the index book.

        ``getattr`` rather than a direct read: several callers stub ``settings``
        with a partial namespace, and a missing key must read as "off" - the safe
        direction for a ledger nobody asked for - rather than raise on the
        dashboard's poll path.
        """
        if self._enabled_override is not None:
            return bool(self._enabled_override)
        return bool(getattr(settings, "gift_book", False))

    @property
    def path(self) -> str:
        return self._path

    @property
    def symbol(self) -> str:
        """The non-tradeable index key, which is also the ledger's symbol."""
        return GIFT_KEY

    @property
    def units(self) -> int:
        """Units per position, kept comparable with one futures lot."""
        if self._units_override is not None:
            return max(1, int(self._units_override))
        return max(1, int(settings.gift_book_units or DEFAULT_UNITS))

    # -- portfolio -------------------------------------------------------
    def open_positions(self) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        self._ensure_initialised()
        return db.get_open_positions(self._path, mode=PAPER)

    def net_units(self) -> int:
        """Signed open quantity, as the engine's view of the index book."""
        return sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in self.open_positions()
        )

    def mark_price(self) -> Optional[float]:
        """The index level this book is valued at, or ``None`` if never marked.

        The book's own mark, not the futures price. The two are different
        instruments: the future carries a basis and stops ticking at the 15:30
        NSE close, while the index keeps trading. Valuing the index book off the
        future would report the basis as P&L and make the two books look like one
        position.
        """
        return self._last_price

    def execute(
        self,
        action: str,
        price: Optional[float],
        *,
        bar_at: Optional[str] = None,
        detail: str = "",
    ) -> Optional[dict[str, Any]]:
        """Settle one signal in the index book. Never raises.

        ``BUY``/``SELL`` move the book to ``+units``/``-units``, flipping an
        opposing position first - the same semantics the Pine entries have, so the
        two books cannot drift apart in *when* they act, only in where they fill.
        ``FLAT`` closes whatever is open, and is what the engine sends when the
        pair stops agreeing.
        """
        if not self.enabled:
            return None
        if action not in {BUY, SELL, FLAT}:
            return None
        try:
            self._ensure_initialised()
        except Exception as exc:
            self._note_error(f"GIFT book unavailable: {exc}")
            return None
        if price is None or float(price) <= 0:
            with self._lock:
                self._skipped += 1
            reason = f"GIFT book skipped {action}: no GIFT Nifty price for the closed bar"
            self._note_error(reason)
            self._record_signal(action, False, reason, bar_at, detail)
            return None

        try:
            with self._lock:
                current = self.net_units()
                if action == FLAT:
                    if current == 0:
                        return None
                    result = self._close_all(float(price), "FLAT")
                else:
                    # A pause blocks *entries*, not exits. The index book is a
                    # separate ledger, so it has to honour the same switch the
                    # futures book does - otherwise pausing the bot still opens
                    # index trades, and a reset is undone on the next bar.
                    if self._entries_paused():
                        reason = f"GIFT book held {action}: strategy paused"
                        self._record_signal(action, False, reason, bar_at, detail)
                        return None
                    target = self.units if action == BUY else -self.units
                    if current == target:
                        return None
                    if current != 0:
                        self._close_all(float(price), "FLIP")
                    result = self._open(float(price), target, detail)
                self._last_action = action
                self._last_action_at = db.iso()
                self._fills += 1
                self._last_error = None
                self._last_error_at = None
            self._record_signal(action, True, result.get("reason", ""), bar_at, detail)
            return result
        except Exception as exc:  # pragma: no cover - defensive
            reason = f"GIFT book {action} failed: {exc}"
            self._note_error(reason)
            self._record_signal(action, False, reason, bar_at, detail)
            return None

    def _entries_paused(self) -> bool:
        """True while the operator has paused new entries.

        Read from the order manager rather than a flag of this book's own: the
        pause is one operator decision, and having the index book keep its own
        copy would let the two books disagree about whether the bot is running.
        """
        try:
            from order_manager import get_order_manager

            # Scoped to this book. The global switch alone was not enough: a book
            # paused from its own tile still opened index trades, so the pause
            # looked applied on the futures leg and ignored on this one.
            return get_order_manager().book_paused(self.strategy_id)
        except Exception:  # pragma: no cover - defensive
            return False

    def _record_signal(
        self,
        action: str,
        accepted: bool,
        reason: str,
        bar_at: Optional[str],
        detail: str,
    ) -> None:
        """Append to this book's own signal trail.

        The index book has its own ledger, so its audit trail has to live there
        too: the futures ``signals`` table is a different database and would
        report the index venue as silent while it is trading. Only real actions
        and real refusals are recorded - a repeat of the position already held is
        a no-op, not a decision, and recording it every bar would bury the trail.
        """
        try:
            db.record_signal(
                action,
                f"GIFT {detail}".strip(),
                accepted,
                reason,
                path=self._path,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("GIFT book could not record its signal: %s", exc)

    def _open(
        self,
        price: float,
        target: int,
        detail: str,
    ) -> dict[str, Any]:
        side = BUY if target > 0 else SELL
        fill = apply_slippage(price, side)
        quantity = abs(target)
        position_id = db.open_position(
            self.symbol,
            target,
            fill,
            path=self._path,
            mode=PAPER,
            source=SOURCE,
            # Stamped with the owning strategy, so a leg in this ledger can be
            # traced to the strategy that opened it and never to another's.
            strategy=f"{self.strategy_id} (GIFT Nifty index)",
            strategy_id=self.strategy_id,
            notes=detail or None,
        )
        db.record_order(
            path=self._path,
            symbol=self.symbol,
            transaction_type=side,
            quantity=quantity,
            price=fill,
            order_type="MARKET",
            product="INDEX",
            status="COMPLETE",
            mode=PAPER,
            # Attributed to this book. Without it every index order fell back to
            # the ledger's default id, so the gift-60m book's fills were recorded
            # under `Surikbs` - invisible to any tile or filter reading the book
            # they belonged to.
            strategy_id=self.strategy_id,
            source=SOURCE,
            position_id=position_id,
            message=f"GIFT INDEX ENTRY | signal close {price:.2f} | fill {fill:.2f}",
        )
        with self._lock:
            self._last_position_id = position_id
        log.info(
            "GIFT index book %s %d units @ %.2f (signal close %.2f, %s)",
            side,
            quantity,
            fill,
            price,
            detail or "no detail",
        )
        _book_alert(
            "GIFT_TRADE_OPENED",
            f"GIFT index book {side} {quantity} units @ {fill:.2f} "
            f"(signal close {price:.2f})",
            severity="info",
            fields={
                "strategy": self.strategy_id,
                "venue": PAPER,
                "symbol": "NSEIX:GIFT NIFTY",
                "action": side,
                "quantity": quantity,
                "price": f"{fill:.2f}",
                "position_id": position_id,
                "signal_close": f"{price:.2f}",
            },
        )
        return {
            "book": "GIFT",
            "action": side,
            "quantity": quantity,
            "price": fill,
            "position_id": position_id,
            "reason": f"opened {quantity} units {'LONG' if target > 0 else 'SHORT'} @ {fill:.2f}",
        }

    def flatten(self, reason: str = "PANIC") -> Optional[dict[str, Any]]:
        """Close every open index position at the current mark.

        The operator's square-off has to reach this ledger too. ``PANIC`` went
        through the order manager, which owns the *futures* book - the index book
        is a separate database, so an emergency stop left its positions open.
        That is the one state an emergency stop must never leave behind.

        Priced at the last mark rather than a signal close: there is no bar to
        act on, and a flatten that cannot find a price is worse than one that
        uses the most recent one. Returns ``None`` when there is nothing to do.
        """
        if not self.enabled:
            return None
        try:
            self._ensure_initialised()
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"GIFT book unavailable: {exc}")
            return None
        with self._lock:
            if self.net_units() == 0:
                return None
            price = self._last_price
            if not price or price <= 0:
                # No mark yet: fall back to the entry price of the open leg so
                # the position still closes rather than being stranded.
                positions = self.open_positions()
                price = float(positions[0]["entry_price"]) if positions else 0.0
            if price <= 0:
                return None
            result = self._close_all(float(price), reason)
            self._last_action = "FLAT"
            self._last_action_at = db.iso()
        self._record_signal("FLAT", True, result.get("reason", ""), None, reason)
        log.warning("GIFT index book flattened (%s): %s", reason, result.get("reason"))
        return result

    def _close_all(self, price: float, reason: str) -> dict[str, Any]:
        """Close every open index position at the signal's close."""
        closed: list[int] = []
        net = 0.0
        for position in self.open_positions():
            side = SELL if int(position["position_key"]) == 1 else BUY
            fill = apply_slippage(price, side)
            quantity = int(position["quantity"])
            entry = float(position["entry_price"])
            buy_turnover = fill * quantity if side == BUY else entry * quantity
            sell_turnover = fill * quantity if side == SELL else entry * quantity
            trade = db.close_position(
                position["id"],
                fill,
                path=self._path,
                fees=estimate_charges(buy_turnover, sell_turnover),
                exit_reason=reason,
                quantity=quantity,
            )
            db.record_order(
                path=self._path,
                symbol=self.symbol,
                transaction_type=side,
                quantity=quantity,
                price=fill,
                order_type="MARKET",
                product="INDEX",
                status="COMPLETE",
                mode=PAPER,
                # See the entry above: an unattributed index order is filed under
                # the ledger's default id and cannot be found by its book.
                strategy_id=self.strategy_id,
                source=SOURCE,
                position_id=position["id"],
                message=(
                    f"GIFT INDEX EXIT ({reason}) | signal close {price:.2f} | "
                    f"net {trade['net_pnl']:+.2f}"
                ),
            )
            closed.append(position["id"])
            net += float(trade["net_pnl"])
            log.info(
                "GIFT index book exit %s %d @ %.2f (%s, net %+.2f)",
                side,
                quantity,
                fill,
                reason,
                trade["net_pnl"],
            )
        if closed:
            # The index book is a separate ledger, so a close here is its own
            # event. Without this the only notification was for the futures book
            # and an index exit went unseen.
            _book_alert(
                "GIFT_TRADE_CLOSED",
                f"GIFT index book closed {len(closed)} position(s) at {price:.2f} "
                f"({reason}), net {net:+.2f}",
                severity="warning",
                fields={
                    "strategy": self.strategy_id,
                    "venue": PAPER,
                    "symbol": "NSEIX:GIFT NIFTY",
                    "action": "EXIT",
                    "quantity": len(closed),
                    "price": f"{price:.2f}",
                    "pnl": f"{net:+.2f}",
                    "reason": reason,
                },
            )
        return {
            "book": "GIFT",
            "action": "FLAT",
            "closed_positions": len(closed),
            "position_ids": closed,
            "net_pnl": round(net, 2),
            "reason": f"closed {len(closed)} position(s) ({reason})",
        }

    # -- valuation -------------------------------------------------------
    def mark(self, price: Optional[float]) -> None:
        """Mark the index book to the GIFT Nifty level.

        Uses the index price directly - there is no basis, because there is no
        contract - which is exactly what makes this book interesting to compare.
        """
        if not self.enabled:
            return
        try:
            value = float(price) if price else 0.0
        except (TypeError, ValueError):
            value = 0.0
        if value <= 0:
            return
        with self._lock:
            self._last_price = value
            self._last_price_at = time.time()
        try:
            self._ensure_initialised()
            positions = db.get_open_positions(self._path, mode=PAPER)
            if not positions:
                return
            db.mark_open_positions(PAPER, {self.symbol: value}, path=self._path)
            metrics = db.get_performance_metrics(
                path=self._path,
                open_positions=db.get_open_positions(self._path, mode=PAPER),
                prices={self.symbol: value},
                mode=PAPER,
            )
            db.record_equity(
                metrics["equity"],
                metrics["balance"],
                metrics["unrealized_pnl"],
                path=self._path,
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"GIFT book mark failed: {exc}")

    def _ensure_initialised(self) -> None:
        if self._initialised:
            return
        # The virtual balance is applied only when the file is first created, so a
        # restart cannot reset a book someone meant to keep.
        first_time = not Path(self._path).exists()
        db.init_db(self._path)
        if first_time:
            db.reset_account(self._path, initial_balance=settings.gift_book_balance)
        self._initialised = True

    def _note_error(self, message: str) -> None:
        with self._lock:
            self._failures += 1
            self._last_error = message
            self._last_error_at = db.iso()
        log.warning("%s", message)

    # -- reporting -------------------------------------------------------
    def positions(self, status: str = "all", limit: int = 200) -> list[dict[str, Any]]:
        """Positions in the index book, shaped like the futures endpoint's rows."""
        if not self.enabled:
            return []
        self._ensure_initialised()
        if status == "open":
            rows = db.get_open_positions(self._path, mode=PAPER)
        else:
            query = "SELECT * FROM positions WHERE mode = ?"
            params: list[Any] = [PAPER]
            if status == "closed":
                query += " AND status = 'CLOSED'"
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = db._fetch_all(query, params, self._path)
        return sorted(rows, key=lambda r: r["entry_time"], reverse=True)

    def trades(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        self._ensure_initialised()
        return db.get_trades(limit=limit, path=self._path)

    def orders(self, limit: int = 500) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        self._ensure_initialised()
        return db.get_orders(limit=limit, path=self._path)

    def signals(self, limit: int = 100) -> list[dict[str, Any]]:
        """This book's own signal trail, newest first.

        Kept per-book for the same reason as the orders and trades: the futures
        ``signals`` table is a different database, so reading it under the index
        heading would show the wrong book's decisions.
        """
        if not self.enabled:
            return []
        self._ensure_initialised()
        return db.get_signals(limit=limit, path=self._path)

    def analytics(self, limit: int = 5000) -> dict[str, Any]:
        """Post-close statistics for the index book, shaped like the futures one.

        The futures endpoint filters one ledger by mode; this reads a different
        ledger entirely, so the two can never be mixed by a stray query param.
        """
        if not self.enabled:
            return {}
        self._ensure_initialised()
        return db.get_trade_analytics(mode=PAPER, limit=limit, path=self._path)

    def summary(self) -> dict[str, Any]:
        """Book-level metrics, computed against the index's own price."""
        if not self.enabled:
            return {"enabled": False, "reason": "GIFT_BOOK is off"}
        self._ensure_initialised()
        prices = {self.symbol: self._last_price} if self._last_price else {}
        positions = db.get_open_positions(self._path, mode=PAPER)
        metrics = db.get_performance_metrics(
            path=self._path, open_positions=positions, prices=prices, mode=PAPER
        )
        return {
            "enabled": True,
            "symbol": self.symbol,
            "tradeable": False,
            "units": self.units,
            "db_path": self._path,
            "open_units": self.net_units(),
            "last_price": self._last_price,
            "last_price_at": self._last_price_at,
            "last_action": self._last_action,
            "last_action_at": self._last_action_at,
            "fills": self._fills,
            "skipped": self._skipped,
            "failures": self._failures,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at,
            "metrics": metrics,
        }

    def status(self) -> dict[str, Any]:
        """Compact form for ``/api/status``: health plus the headline numbers."""
        if not self.enabled:
            return {"enabled": False, "configured": False, "symbol": self.symbol}
        try:
            data = self.summary()
        except Exception as exc:  # pragma: no cover - defensive
            self._note_error(f"GIFT book status failed: {exc}")
            return {
                "enabled": True,
                "configured": True,
                "symbol": self.symbol,
                "last_error": self._last_error,
            }
        metrics = data.pop("metrics", {})
        return {
            **data,
            "configured": True,
            "net_pnl": round(float(metrics.get("realized_total") or 0.0), 2),
            "unrealized_pnl": round(float(metrics.get("unrealized_pnl") or 0.0), 2),
            "equity": round(float(metrics.get("equity") or 0.0), 2),
            "trades": int(metrics.get("total_trades") or 0),
            "win_rate": metrics.get("win_rate"),
            "realized_today": round(float(metrics.get("realized_today") or 0.0), 2),
        }


_books: dict[str, GiftBook] = {}
_book_lock = threading.Lock()


def get_gift_book(strategy_id: Optional[str] = None) -> GiftBook:
    """The index book belonging to one strategy.

    One book per strategy, not one per process. Two GIFT-signalled strategies
    are two independent books: a shared ledger let the 15-minute book read the
    60-minute book's position as its own, close it, and report P&L that belonged
    to neither. Keyed by strategy id and each with its own database file, so the
    books cannot read or corrupt each other by construction.

    ``strategy_id=None`` returns the default book, which is what every caller
    written before this existed expects.
    """
    key = (strategy_id or DEFAULT_STRATEGY).strip() or DEFAULT_STRATEGY
    with _book_lock:
        book = _books.get(key)
        if book is None:
            # The default book keeps the *unsuffixed* path on purpose: that is
            # where every row written before books were per-strategy lives, and
            # pointing it at a new file would make the operator's whole GIFT
            # index history disappear from the dashboard on upgrade.
            book = GiftBook(strategy_id=None if key == DEFAULT_STRATEGY else key)
            _books[key] = book
        return book


def reset_gift_books() -> None:
    """Drop every cached book, so a test or a reconnect starts clean."""
    with _book_lock:
        _books.clear()


def gift_books() -> list[GiftBook]:
    """Every strategy's index book, in configuration order.

    The API used to read one book and report it as *the* index venue. With one
    book per strategy that showed a single strategy's GIFT leg and silently hid
    the rest, which reads as "the other strategies never traded the index". This
    returns them all so an endpoint can aggregate or filter by ``strategy_id``.
    """
    from config import strategy_configs

    books: list[GiftBook] = []
    for cfg in strategy_configs():
        if not cfg.enabled:
            continue
        # Only a strategy that asked for the index leg has a book. Creating one
        # for a futures-only strategy would invent a ledger nobody trades.
        if "gift" not in cfg.resolved_legs:
            continue
        books.append(get_gift_book(cfg.id))
    # A ledger from before books were per-strategy belongs to no configured id.
    # Dropping it from the list does not delete the rows - it hides them, which
    # is worse: the operator sees a GIFT book that has never traded while their
    # whole index history sits unread on disk. It is included, and the dashboard
    # marks it unattributed the same way it does an orphaned futures position.
    configured = {book.strategy_id for book in books}
    if DEFAULT_STRATEGY not in configured:
        legacy = get_gift_book()
        try:
            has_rows = bool(legacy.positions(limit=1)) or bool(legacy.trades(limit=1))
        except Exception:  # pragma: no cover - defensive
            has_rows = False
        if has_rows:
            books.append(legacy)
    return books


def gift_book_for(strategy_id: Optional[str] = None) -> GiftBook:
    """One book by strategy id, or the default when none is named.

    ``all`` (and an empty value) is deliberately *not* special-cased here: a
    caller that means every book must say so, rather than a typo silently
    switching a single-book report into an aggregate.
    """
    return get_gift_book(strategy_id)


__all__ = [
    "GiftBook", "get_gift_book", "gift_books", "gift_book_for", "reset_gift_books",
    "SOURCE", "DEFAULT_UNITS", "DEFAULT_STRATEGY",
]
