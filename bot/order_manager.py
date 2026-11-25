"""Trade execution: market-hour gating, sizing, margin checks, paper/live orders.

Signal semantics (aligned with the Pine script's ``strategy.entry`` calls):

* ``BUY``  -> target position +N lots  (flip shorts to longs)
* ``SELL`` -> target position -N lots  (flip longs to shorts)

Execution is idempotent per ``(signal_id, mode)`` so a repeated signal never
doubles up a position.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import database as db
import fill
from config import LIVE, PAPER, get_execution_mode, settings
from kite_client import (
    KiteClient,
    _expiry_has_passed,
    contract_expiry,
    future_symbol,
    get_client,
    is_market_open,
    market_status,
)

log = logging.getLogger("order_manager")

BUY = "BUY"
SELL = "SELL"
FLAT = "FLAT"
PANIC = "PANIC"
VALID_ACTIONS = {BUY, SELL, FLAT, PANIC}

# NSE Nifty-futures cost model (Zerodha), used for paper and LIVE accounting (INR).
#
# Every rate below is the *statutory* one, applied to the side it belongs to:
# STT on the sell leg only, stamp duty on the buy leg only. STT and stamp duty
# are rounded to the nearest whole rupee, which is how the exchange levies them;
# rounding them per trade rather than carrying fractions is what makes a
# small-quantity round trip match a contract note.
BROKERAGE_CAP = 20.0           # per order, flat for F&O
BROKERAGE_RATE = 0.0003        # 0.03%, whichever is lower
STT_RATE_SELL = 0.0005         # 0.05% on sell turnover only
EXCHANGE_TXN_RATE = 0.0000183  # 0.00183% on total turnover
SEBI_RATE = 0.000001           # 0.0001% (Rs 10 per crore) on total turnover
STAMP_RATE_BUY = 0.00002       # 0.002% on buy turnover only
GST_RATE = 0.18                # on brokerage + exchange + SEBI


def charge_breakdown(
    turnover_buy: float, turnover_sell: float, orders: int = 2
) -> dict[str, float]:
    """Itemised round-trip cost stack, per component.

    ``turnover_buy``/``turnover_sell`` are the *values actually transacted*, not
    the notional of both sides: for a long round trip the buy leg is the entry
    value and the sell leg the exit value, so a winning trade pays more STT.

    ``orders`` is how many orders the round trip used (2 for a normal
    entry+exit). Brokerage is capped per order, so this is the only place the
    order count enters.

    Returned per-component so a caller can show the breakdown instead of only the
    total - the total alone cannot tell you whether STT or brokerage dominates.
    """
    buy_value = max(float(turnover_buy), 0.0)
    sell_value = max(float(turnover_sell), 0.0)
    total_turnover = buy_value + sell_value
    order_count = max(int(orders), 0)

    brokerage_buy = min(BROKERAGE_CAP, BROKERAGE_RATE * buy_value)
    brokerage_sell = min(BROKERAGE_CAP, BROKERAGE_RATE * sell_value)
    if order_count == 2:
        # The normal entry+exit round trip: one order per leg, each capped.
        brokerage = brokerage_buy + brokerage_sell
    else:
        # Any other count has no per-leg split to work from, so cap the rate
        # against the whole turnover and bound it by the per-order cap.
        brokerage = min(BROKERAGE_CAP * order_count, BROKERAGE_RATE * total_turnover)

    # Rupee-rounded levies. A fractional rupee cannot be charged, and rounding
    # only the final total would drift from the contract note by up to a rupee.
    stt = float(round(STT_RATE_SELL * sell_value))
    stamp = float(round(STAMP_RATE_BUY * buy_value))

    exchange = EXCHANGE_TXN_RATE * total_turnover
    sebi = SEBI_RATE * total_turnover
    gst = GST_RATE * (brokerage + exchange + sebi)
    total = brokerage + stt + stamp + exchange + sebi + gst

    return {
        "brokerage": round(brokerage, 2),
        "stt": round(stt, 2),
        "stamp_duty": round(stamp, 2),
        "exchange": round(exchange, 2),
        "sebi": round(sebi, 2),
        "gst": round(gst, 2),
        "total": round(total, 2),
    }


def estimate_charges(turnover_buy: float, turnover_sell: float, orders: int = 2) -> float:
    """Round-trip charge total for the given leg turnovers."""
    return charge_breakdown(turnover_buy, turnover_sell, orders)["total"]


def breakeven_points(
    quantity: int, turnover_buy: float, turnover_sell: float, orders: int = 2
) -> float:
    """What a position must move to cover the charge stack entirely."""
    if quantity <= 0:
        return 0.0
    return round(estimate_charges(turnover_buy, turnover_sell, orders) / quantity, 2)


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    action: str
    mode: str
    symbol: str
    quantity: int = 0
    price: Optional[float] = None
    reason: str = ""
    position_id: Optional[int] = None
    order_ids: tuple[str, ...] = ()
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["order_ids"] = list(self.order_ids)
        return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _round_tick(price: float) -> float:
    tick = settings.tick_size or 0.05
    return round(round(price / tick) * tick, 2)


def apply_slippage(price: float, side: str) -> float:
    """Adverse slippage in ticks for paper fills."""
    offset = settings.slippage_ticks * settings.tick_size
    return _round_tick(price + offset if side == BUY else price - offset)


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=db.IST)
    return dt.astimezone(db.IST)


def _signal_age_seconds(payload: dict[str, Any]) -> Optional[float]:
    ts = _parse_ts(payload.get("timestamp") or payload.get("time"))
    if ts is None:
        return None
    return (datetime.now(timezone.utc) - ts.astimezone(timezone.utc)).total_seconds()


# ---------------------------------------------------------------------------
# Order manager
# ---------------------------------------------------------------------------
class OrderManager:
    def __init__(self, client: Optional[KiteClient] = None) -> None:
        self._client = client
        self._lock = __import__("threading").RLock()
        self._paused = False
        self._paused_reason = ""
        # Per-book pause, keyed by strategy id. Separate from ``_paused``, which
        # is the operator's global kill switch: pausing one book must not stop
        # the others, and PANIC must still stop everything.
        self._book_paused: dict[str, str] = {}

    @property
    def client(self) -> KiteClient:
        if self._client is None:
            self._client = get_client()
        self._client.mode = get_execution_mode()
        return self._client

    # -- utilities -------------------------------------------------------
    def current_symbol(self, now: Optional[datetime] = None) -> str:
        if get_execution_mode() == LIVE:
            # Same early-rollover window as PAPER, resolved against Kite's dump
            # so the symbol is one the broker actually lists.
            return self.client.front_month_symbol(now)
        return future_symbol(now)

    def _force_off_hours(self) -> bool:
        """Testing/ops escape hatch: TRADING_ALLOW_OFF_HOURS=1."""
        import os

        return os.getenv("TRADING_ALLOW_OFF_HOURS", "0").lower() in {"1", "true", "yes"}

    def _price(
        self, symbol: str, payload: dict[str, Any], allow_payload_price: bool
    ) -> Optional[float]:
        """The price to fill at, always from the future being traded.

        A payload price is never used as a fill price. It used to be, for an
        after-hours PAPER entry, which is what booked the entry at the *GIFT
        close* - an index level. Since the position is then marked at the
        future's own LTP, the row opened already showing the index/future basis
        as P&L that no trade had produced. It is now at most a fallback tick for
        a symbol with no quote at all, and only where the payload is not carrying
        a signal-source price.
        """
        # An order needs the freshest price, so bypass the quote cache here.
        ltp = self.client.get_ltp(symbol, max_age=0)
        if ltp:
            return float(ltp)
        if allow_payload_price and payload.get("paper_after_hours") is not True:
            for key in ("price", "ltp", "close"):
                if payload.get(key) not in (None, ""):
                    try:
                        value = float(payload[key])
                    except (TypeError, ValueError):
                        continue
                    self.client.remember_tick(symbol, value)
                    return value
        return None

    def _paper_fill(
        self, symbol: str, side: str, quantity: int, reference: float, depth: Any
    ) -> fill.FillResult:
        """Simulate a paper market order against the visible order book.

        The reference price is the future's own last price (never the GIFT close).
        When the quote carries depth the fill walks the book; otherwise it falls
        back to the same adverse-tick approximation :func:`apply_slippage` has
        always used, so a quote without depth grades exactly as before.
        """
        return fill.simulate_fill(
            symbol,
            side,
            quantity,
            ltp=reference,
            depth=depth,
            tick_size=settings.tick_size,
            slippage_ticks=settings.slippage_ticks,
        )

    def _lot_size(self, symbol: str, mode: str) -> int:
        if mode == LIVE:
            instrument = self.client.instrument_for_symbol(symbol)
            if not instrument or instrument.get("instrument_type") != "FUT":
                raise ValueError(f"{symbol} is not a verified NIFTY futures contract")
            lot_size = int(instrument.get("lot_size") or 0)
            if lot_size <= 0:
                raise ValueError(f"Kite returned an invalid lot size for {symbol}")
            # Must be the contract the engine would trade, which inside the
            # rollover window is next month's - not the calendar front month.
            # Comparing against `front_month_contract` here would reject every
            # live order for the last five days of the contract's life.
            expected = self.current_symbol()
            if not expected or symbol != expected:
                raise ValueError(
                    f"{symbol} is not the contract LIVE trades ({expected or 'none available'})"
                )
            return lot_size
        return settings.lot_size

    def _other_contract_positions(
        self, symbol: str, mode: str, strategy_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Open NIFTY futures positions in a contract other than ``symbol``.

        Restricted to real futures symbols so a GIFT index-book row, which lives
        in its own database but shares the position table's shape, can never be
        mistaken for a futures leg.
        """
        if mode == LIVE:
            futures = {item["tradingsymbol"] for item in self.client.nifty_future_instruments()}
        else:
            futures = None
        out: list[dict[str, Any]] = []
        for position in db.get_open_positions(mode=mode, strategy_id=strategy_id):
            held = str(position["symbol"])
            if held == symbol:
                continue
            if futures is not None:
                if held not in futures:
                    continue
            elif contract_expiry(held) is None:
                continue
            out.append(position)
        return out

    def _expired_positions(
        self, mode: str, strategy_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Open positions whose contract has stopped trading.

        A contract stops trading at the 15:30 close on its expiry day, so this is
        the state in which a position has no venue left. It is deliberately
        separate from :meth:`_rolled_away_positions`: that one is the *old leg of
        a rollover*, which is still tradeable and is closed by the next flip,
        while this one is an expiry that only a human can resolve.

        Restricted to real futures symbols so a GIFT index-book row, which lives
        in its own database but shares the position table's shape, can never be
        mistaken for a futures leg.
        """
        out: list[dict[str, Any]] = []
        for position in db.get_open_positions(mode=mode, strategy_id=strategy_id):
            expiry = contract_expiry(str(position["symbol"]))
            if expiry is not None and _expiry_has_passed(expiry):
                out.append(position)
        return out

    def _rolled_away_positions(
        self, symbol: str, mode: str, strategy_id: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Open positions in a contract the engine has already rolled away from.

        These are the old leg of a rollover. The engine switches to the next
        month five days before expiry, so inside that window the contract it
        names is next month's while a position may still be held in the front
        month - which is still *tradeable*, just no longer the one being traded.

        The test is therefore not "has it expired" but "is it at or behind the
        contract the engine has moved on from": a position whose expiry is on or
        before the front month's expiry is the old leg, while one in a *later*
        month is not a rollover at all and is refused as double exposure.
        """
        front_month_expiry = contract_expiry(future_symbol(rollover_days=0))
        out: list[dict[str, Any]] = []
        for position in self._other_contract_positions(symbol, mode, strategy_id):
            held_expiry = contract_expiry(str(position["symbol"]))
            if held_expiry is None:
                continue
            if front_month_expiry is None or held_expiry <= front_month_expiry:
                out.append(position)
        return out

    def _current_net_lots(
        self,
        symbol: str,
        mode: str,
        lot_size: int,
        strategy_id: Optional[str] = None,
    ) -> int:
        """Signed lots this strategy holds in ``symbol``.

        ``strategy_id`` is required in spirit even though it is optional in
        signature: two strategies trade the same front-month contract, so an
        unscoped count lets one book read the other's leg as its own. With A long
        and B short the net is zero, and neither would ever enter. Only the
        portfolio-wide callers (PANIC, broker reconciliation) pass ``None``.
        """
        quantity = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(mode=mode, strategy_id=strategy_id)
            if position["symbol"] == symbol
        )
        if quantity % lot_size:
            raise ValueError(
                f"Ledger quantity {quantity} for {symbol} is not a whole number of {lot_size}-unit lots"
            )
        return quantity // lot_size

    def current_lot_size(self) -> int:
        """Lot size of the contract that would trade right now.

        LIVE verifies it against Kite's NFO instruments, so a mirror built from
        this matches the contract the broker actually filled. A failure falls
        back to the configured lot size rather than raising, because the shadow
        book must never be able to stop live trading.
        """
        try:
            symbol = self.current_symbol()
            return int(self._lot_size(symbol, get_execution_mode()) or settings.lot_size)
        except Exception:
            return int(settings.lot_size)

    def _live_position_sync_error(self, mode: Optional[str] = None) -> Optional[str]:
        """Reconcile broker positions against the ledger; a string means blocked.

        ``mode`` defaults to the active execution mode. The readiness report
        passes ``LIVE`` explicitly so the check can run *before* the switch - a
        gate that only works once you are already live is not a gate.
        """
        if (mode or get_execution_mode()) != LIVE:
            return None
        try:
            # force=True: this runs from the readiness gate while the bot is
            # still in PAPER, so the client's own mode would short-circuit the
            # read and the gate could never pass.
            broker_positions = self.client.positions(force=True)
            future_symbols = {
                str(instrument["tradingsymbol"])
                for instrument in self.client.nifty_future_instruments()
                if instrument.get("tradingsymbol")
            }
        except Exception as exc:
            return f"Could not reconcile Zerodha positions: {exc}"
        if not future_symbols:
            return "Could not load NIFTY futures instruments to reconcile Zerodha positions"

        # The engine switches to next month five days before expiry, so inside
        # that window the contract it trades is a contract the broker may not be
        # in yet - and the calendar front month, which the broker *is* in, is
        # then an old leg rather than a mismatch. Neither is exempted: agreement
        # needs no exemption, and a *disagreement* on either leg is real drift.
        local_by_symbol: dict[str, int] = {}
        for position in db.get_open_positions(mode=LIVE):
            if position["symbol"] in future_symbols:
                symbol = position["symbol"]
                local_by_symbol[symbol] = local_by_symbol.get(symbol, 0) + (
                    int(position["quantity"]) * int(position["position_key"])
                )

        broker_by_symbol: dict[str, int] = {}
        for position in broker_positions:
            symbol = str(position.get("tradingsymbol") or "")
            if symbol in future_symbols:
                broker_by_symbol[symbol] = broker_by_symbol.get(symbol, 0) + int(
                    position.get("quantity") or 0
                )

        for symbol in sorted(future_symbols | local_by_symbol.keys() | broker_by_symbol.keys()):
            local_quantity = local_by_symbol.get(symbol, 0)
            broker_quantity = broker_by_symbol.get(symbol, 0)
            if local_quantity != broker_quantity:
                return (
                    f"Position mismatch for {symbol}: local ledger {local_quantity}, "
                    f"Zerodha {broker_quantity}; reconcile manually before trading"
                )
        return None

    def _live_sync_state(self, mode: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Broker-vs-ledger net quantity for the traded contract, when readable.

        This is the authoritative position source for LIVE. A mismatch is not
        auto-corrected: silently rewriting the ledger could hide a real loss, so
        it blocks trading until a human reconciles.

        ``mode`` defaults to the active execution mode; the readiness report
        passes ``LIVE`` so the figure can be shown while still in PAPER.
        """
        if (mode or get_execution_mode()) != LIVE:
            return None
        # Resolved through the LIVE path rather than `current_symbol()`, which
        # branches on the *active* mode - this must name the live contract even
        # when the report runs while the bot is still in PAPER.
        symbol = self.client.front_month_symbol()
        try:
            # force=True for the same reason as above: the readiness report runs
            # before the switch, so the client's mode must not gate the read.
            broker = self.client.net_position(symbol, force=True)
        except Exception as exc:
            return {"symbol": symbol, "broker": None, "ledger": None,
                    "in_sync": False, "error": f"Could not read Zerodha positions: {exc}"}
        if broker is None:
            return {"symbol": symbol, "broker": None, "ledger": None,
                    "in_sync": False, "error": "Zerodha positions unavailable"}
        ledger = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(mode=LIVE)
            if position["symbol"] == symbol
        )
        return {
            "symbol": symbol,
            "broker": broker,
            "ledger": ledger,
            "in_sync": broker == ledger,
            "error": None,
        }

    def _margin_available(self, mode: str) -> float:
        if mode == LIVE:
            cash = self.client.available_cash()
            return float(cash) if cash is not None else 0.0
        return float(db.get_account()["balance"])

    def _required_margin(
        self, symbol: str, action: str, quantity: int, mode: str, price: Optional[float]
    ) -> tuple[float, bool]:
        """Margin needed for an entry, and whether the figure is authoritative.

        LIVE asks Kite for the real SPAN+exposure requirement. PAPER has no
        broker to ask, so it uses the ``LOT_MARGIN`` estimate and reports it as
        an estimate. LIVE falling back to the estimate is flagged too, so the UI
        can say the guard is a guess rather than imply it is exact.
        """
        if mode == LIVE:
            exact = self.client.required_margin(
                symbol, action, quantity, price=price, product=settings.product
            )
            if exact is not None:
                return exact, True
        lots = max(1, quantity // max(1, settings.lot_size))
        return lots * float(settings.lot_margin), False

    # -- pause / resume --------------------------------------------------
    @property
    def paused(self) -> bool:
        """True while new entries are blocked (panic switch, ops hold)."""
        return self._paused

    @property
    def paused_reason(self) -> str:
        """Why entries are blocked, for the dashboard and the readiness report."""
        return self._paused_reason

    def pause(self, reason: str = "PAUSED") -> None:
        """Block new BUY/SELL entries. Exits (FLAT/PANIC) stay available."""
        self._paused = True
        self._paused_reason = reason
        log.warning("Strategy paused: %s", reason)

    def resume(self) -> None:
        self._paused = False
        self._paused_reason = ""
        log.warning("Strategy resumed")

    # -- per-book pause --------------------------------------------------
    def book_paused(self, strategy_id: str) -> bool:
        """Whether this book's entries are blocked.

        The global switch still wins: an operator who pressed Pause means every
        book stops, whatever the per-book state says.
        """
        with self._lock:
            return self._paused or str(strategy_id) in self._book_paused

    def book_paused_reason(self, strategy_id: str) -> str:
        with self._lock:
            if self._paused:
                return self._paused_reason or "PAUSED"
            return self._book_paused.get(str(strategy_id), "")

    def pause_book(self, strategy_id: str, reason: str = "MANUAL") -> None:
        """Block new entries for one book. Its exits stay available."""
        with self._lock:
            self._book_paused[str(strategy_id)] = reason or "MANUAL"
        log.warning("Book %s paused: %s", strategy_id, reason)

    def resume_book(self, strategy_id: str) -> None:
        with self._lock:
            self._book_paused.pop(str(strategy_id), None)
        log.warning("Book %s resumed", strategy_id)

    def paused_books(self) -> dict[str, str]:
        with self._lock:
            return dict(self._book_paused)

    # -- public API ------------------------------------------------------
    def process_signal(
        self,
        payload: dict[str, Any],
        source: str = "WEBHOOK",
        strategy_id: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> ExecutionResult:
        """Validate and route a signal. Never raises for business rejections.

        ``strategy_id`` names the book this signal belongs to. It scopes the
        netting, the position lookups and the fill, so two strategies trading the
        same contract cannot read or close each other's leg. Defaults to the
        legacy book so an existing caller keeps working unchanged.

        ``mode`` is that book's mode, resolved by the caller from
        ``StrategyRun.mode()``. It is not read from the global setting here for
        the same reason ``strategy_id`` is not defaulted to one value: once books
        can differ, the order must be booked into the ledger the book actually
        trades, and re-reading the process-wide mode would put a paper book's
        fill into the live ledger (or a live one into paper) whenever the bot's
        switch and the book's consent disagree.
        """
        strategy_id = strategy_id or db.DEFAULT_STRATEGY_ID
        action = str(payload.get("action") or payload.get("signal") or "").strip().upper()
        signal_id = str(payload.get("signal_id") or payload.get("id") or "").strip() or None
        mode = mode or get_execution_mode()
        symbol = str(payload.get("symbol") or payload.get("ticker") or "").strip().upper()
        symbol = symbol or self.current_symbol()

        def reject(reason: str) -> ExecutionResult:
            db.record_signal(
                action or "UNKNOWN", json.dumps(payload, default=str), False, reason,
                strategy_id=strategy_id,
            )
            log.warning("Rejected %s signal: %s", action or "?", reason)
            return ExecutionResult(False, action or "UNKNOWN", mode, symbol, reason=reason)

        if action not in VALID_ACTIONS:
            return reject(f"Unknown action {action!r}; expected one of {sorted(VALID_ACTIONS)}")

        if action in {BUY, SELL} and self.book_paused(strategy_id):
            return reject(
                f"Strategy paused ({self.book_paused_reason(strategy_id) or 'PAUSED'}); "
                f"{action} ignored"
            )

        if signal_id and self._already_processed(signal_id, mode, strategy_id):
            return reject(f"Duplicate signal_id {signal_id}")

        after_hours_paper_engine_signal = (
            mode == PAPER
            and source == "ENGINE"
            and payload.get("paper_after_hours") is True
        )
        if (
            action not in {FLAT, PANIC}
            and not is_market_open()
            and not after_hours_paper_engine_signal
        ):
            return reject(f"Outside market hours ({market_status()['reason']})")

        age = _signal_age_seconds(payload)
        max_age = int(payload.get("max_age_seconds") or 300)
        if action not in {FLAT, PANIC} and age is not None and age > max_age:
            return reject(f"Stale signal ({age:.0f}s old, max {max_age}s)")

        with self._lock:
            try:
                # LIVE must not trade on a dead session: without a valid access
                # token the GIFT feed stops and the mark loop goes stale, so any
                # entry would be placed against a frozen price.
                if mode == LIVE:
                    health = self.client.session_status()
                    if not health.get("valid"):
                        return reject(
                            "LIVE disabled: Kite session is not valid "
                            f"({health.get('error') or health.get('state') or 'token expired'}). "
                            "Re-login from the dashboard."
                        )
                sync_error = self._live_position_sync_error()
                if sync_error:
                    return reject(sync_error)
                if action == FLAT:
                    result = self.square_off(
                        "SIGNAL_FLAT", source=source, symbol=symbol, mode=mode,
                        strategy_id=strategy_id,
                    )
                elif action == PANIC:
                    # Deliberately unscoped: the operator's big red button closes
                    # every book, not just the one that called it.
                    result = self.square_off("PANIC", source=source, mode=mode)
                else:
                    # The book this signal belongs to. Omitting it fell back to
                    # the legacy default, so every entry scoped its position
                    # lookup to a book nobody configured: the real book appeared
                    # to hold nothing while the default id accumulated the legs,
                    # and each entry was then rejected as "already positioned"
                    # against the previous book's fill.
                    result = self._execute_directional(
                        payload, action, symbol, mode, source, strategy_id=strategy_id,
                    )
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Execution failure")
                return reject(f"Execution error: {exc}")

        db.record_signal(
            action,
            json.dumps({"signal_id": signal_id, **payload}, default=str),
            result.ok,
            result.reason,
            strategy_id=strategy_id,
        )
        return result

    def _already_processed(
        self, signal_id: str, mode: str, strategy_id: Optional[str] = None
    ) -> bool:
        """Whether this book has already acted on this signal.

        Scoped to ``strategy_id``. The scan used to cover every book's rows, so
        two strategies that produced the same signal id - the same action on the
        same bar - collided: the second book's order was rejected as a duplicate
        of the first's, and it silently never traded. The id is now built from
        the book's own bar as well, but the guard has to be scoped regardless:
        an id is only unique within the book that generated it.
        """
        needle = f'"signal_id": "{signal_id}"'
        for row in db.get_signals(limit=200):
            if not row["accepted"]:
                continue
            if strategy_id is not None and row.get("strategy_id") != strategy_id:
                continue
            if needle in (row["payload"] or ""):
                return True
        return False

    def _execute_directional(
        self,
        payload: dict[str, Any],
        action: str,
        symbol: str,
        mode: str,
        source: str,
        strategy_id: str,
    ) -> ExecutionResult:
        """Place an entry for ``strategy_id``.

        ``strategy_id`` is required: position lookups, netting and the ledger
        stamp are all scoped to it, and a default made an omission invisible -
        the order quietly belonged to the legacy book instead of the caller's.
        """
        lots = int(payload.get("qty_lots") or payload.get("lots") or payload.get("quantity") or settings.default_lots)
        if lots <= 0:
            return ExecutionResult(False, action, mode, symbol, reason="Quantity must be positive")
        pending = db.get_pending_orders(mode, symbol)
        if pending:
            return ExecutionResult(
                False, action, mode, symbol,
                reason=f"Unresolved order {pending[-1]['exchange_order_id'] or pending[-1]['id']} for {symbol}",
                order_ids=tuple(
                    str(row["exchange_order_id"])
                    for row in pending
                    if row.get("exchange_order_id")
                ),
            )
        try:
            lot_size = self._lot_size(symbol, mode)
            qty = lots * lot_size
            current = self._current_net_lots(symbol, mode, lot_size, strategy_id)
        except (TypeError, ValueError) as exc:
            return ExecutionResult(False, action, mode, symbol, reason=str(exc))
        target_lots = lots if action == BUY else -lots

        # A leg whose contract has stopped trading has no venue left. It is an
        # expiry to resolve by hand, so new entries are blocked until it is
        # squared off - opening the next month beside it would be two contracts
        # at once, which is the double exposure this method refuses below.
        #
        # Checked before the rollover branch so the block is the reason reported,
        # rather than a rollover that cannot fill. Exits are never blocked: a
        # position that cannot be closed is far worse than one that cannot be
        # opened.
        expired = self._expired_positions(mode, strategy_id)
        if expired:
            expired_symbols = ", ".join(sorted({position["symbol"] for position in expired}))
            return ExecutionResult(
                False, action, mode, symbol,
                reason=(
                    f"Entries blocked: {expired_symbols} has stopped trading and is an "
                    f"expiry to resolve by hand. Square it off (PANIC) before opening "
                    f"{symbol}."
                ),
            )

        # A position in a *different* NIFTY futures contract is either a rollover
        # in progress or a mistake, and the two are told apart by which contract
        # it is in.
        #
        #  - At or behind the front month: the engine has already rolled away
        #    from it (it switches five days before expiry), so this is the old leg
        #    of a rollover.
        #  - A later month: two contracts at once is not a rollover, it is double
        #    exposure. Refuse, in both modes, so PAPER cannot book a state that
        #    LIVE would reject.
        stale = self._rolled_away_positions(symbol, mode, strategy_id)
        if stale:
            stale_symbols = ", ".join(sorted({position["symbol"] for position in stale}))
            # In *lots*, to match target_lots. The ledger stores units, so
            # comparing the two directly would never agree and every signal would
            # roll - which is the opposite of what this branch is for.
            held_units = sum(
                int(position["quantity"]) * int(position["position_key"])
                for position in stale
            )
            held_lots = round(held_units / max(1, lot_size))
            if held_lots == target_lots:
                # The position already points the same way as the signal, so
                # rolling it would pay two spreads to end up exactly where it
                # already is. Leave it in the contract it is in and let it run to
                # expiry; the next *flip* is what moves it.
                return ExecutionResult(
                    False, action, mode, symbol,
                    reason=(
                        f"Holding {stale_symbols} {held_lots:+d} lots, which already "
                        f"matches the {action} signal; leaving it until expiry rather "
                        f"than paying two spreads to roll"
                    ),
                )
            closed = self.square_off(
                "ROLLOVER", source=source, mode=mode, mark_price=None,
                strategy_id=strategy_id,
            )
            if not closed.ok:
                return ExecutionResult(
                    False, action, mode, symbol,
                    reason=(
                        f"Rollover blocked: could not close {stale_symbols} "
                        f"({closed.reason})"
                    ),
                    order_ids=closed.order_ids,
                )
            log.info(
                "Rolled over on a flip: closed %s (%+d lots) before opening %s",
                stale_symbols, held_lots, symbol,
            )
        else:
            other_positions = self._other_contract_positions(symbol, mode, strategy_id)
            if other_positions:
                open_symbols = ", ".join(sorted({position["symbol"] for position in other_positions}))
                return ExecutionResult(
                    False, action, mode, symbol,
                    reason=f"Existing NIFTY futures exposure on {open_symbols}; square it off before rollover",
                )

        if current == target_lots:
            return ExecutionResult(
                False, action, mode, symbol, reason=f"Already positioned at {current:+d} lots"
            )
        if abs(lots) > settings.max_open_lots:
            return ExecutionResult(
                False, action, mode, symbol,
                reason=f"{lots} lots exceeds MAX_OPEN_LOTS={settings.max_open_lots}",
            )

        price = self._price(symbol, payload, allow_payload_price=(mode == PAPER))
        if not price or price <= 0:
            return ExecutionResult(False, action, mode, symbol, reason="No reference price available")

        order_ids: list[str] = []
        # 1) Close any opposing exposure.
        if current != 0:
            closed = self.square_off(
                "FLIP", source=source, symbol=symbol, mode=mode,
                mark_price=price, strategy_id=strategy_id,
            )
            if not closed.ok:
                return closed
            order_ids.extend(closed.order_ids)

        needed_lots = abs(lots)
        required_margin, margin_is_exact = self._required_margin(
            symbol, action, qty, mode, price
        )
        available = self._margin_available(mode)
        if available < required_margin:
            estimate_note = "" if margin_is_exact else " (estimated)"
            return ExecutionResult(
                False, action, mode, symbol,
                reason=(
                    f"Insufficient margin: need Rs {required_margin:,.0f}{estimate_note}, "
                    f"have Rs {available:,.0f}"
                ),
                details={"required_margin": required_margin, "margin_is_exact": margin_is_exact},
            )

        fill = apply_slippage(price, action)
        simulated: Optional[fill.FillResult] = None
        ack = self.client.place_order(
            symbol=symbol,
            transaction_type=action,
            quantity=qty,
            price=fill,
            order_type=settings.order_type,
            product=settings.product,
            tag="niftybot",
            # The book's mode, so a paper book cannot submit a real order just
            # because the process is armed.
            mode=mode,
        )
        order_id = ack.get("order_id")
        status = str(ack.get("status") or "REJECTED").upper().replace(" ", "_")
        try:
            filled_quantity = int(ack.get("filled_quantity") or 0)
        except (TypeError, ValueError):
            filled_quantity = 0
        if status == "COMPLETE" and filled_quantity == 0:
            filled_quantity = qty
        if filled_quantity < 0 or filled_quantity > qty:
            return ExecutionResult(
                False, action, mode, symbol, reason=f"Invalid filled quantity from Kite: {filled_quantity}"
            )
        # PAPER simulates the fill against the order book when the quote carried
        # depth; a partial fill books only the portion that actually filled and
        # reports the rest honestly rather than inventing a price for liquidity
        # that was not shown. LIVE keeps the broker's own fill untouched.
        partial_note = ""
        if mode == PAPER and status == "COMPLETE":
            simulated = self._paper_fill(
                symbol, action, filled_quantity, price, ack.get("depth")
            )
            if simulated.filled_quantity < filled_quantity:
                partial_note = (
                    f" (partial: {simulated.filled_quantity}/{filled_quantity} filled; "
                    "remaining not shown in the book)"
                )
            filled_quantity = simulated.filled_quantity
            fill = simulated.price
        if filled_quantity == 0:
            db.record_order(
                symbol=symbol, transaction_type=action, quantity=qty, price=fill,
                order_type=settings.order_type, product=settings.product,
                status=status, mode=mode, source=source, strategy_id=strategy_id,
                exchange_order_id=order_id, message=ack.get("message"),
            )
            return ExecutionResult(
                False, action, mode, symbol,
                reason=(
                    f"Order not filled (status {status}): {ack.get('message')}"
                    if ack.get("message")
                    else f"Order not filled (status {status})"
                ),
                order_ids=(str(order_id),) if order_id else (),
            )

        fill_price = float(ack.get("average_price") or fill)
        if simulated is not None:
            # The simulated book fill is the authoritative paper price; the ack's
            # average_price is only the fallback tick approximation the mock
            # echoed back. Preferring the simulation keeps entry and mark on the
            # same (book-derived) instrument price.
            fill_price = simulated.price
        signed_quantity = filled_quantity if action == BUY else -filled_quantity
        # The protective stop is placed at the fill, on the futures price. The
        # engine sends the book's rules; the level is derived here so it is
        # relative to the price the broker actually gave, not the signal close.
        # A payload without rules (a webhook, a manual order) has no stop.
        position_key = 1 if signed_quantity > 0 else -1
        initial_stop = None
        rules = payload.get("stop_rules")
        if isinstance(rules, dict) and rules.get("enabled"):
            import stops

            initial_stop = stops.initial_stop(
                fill_price,
                position_key,
                stops.StopRules(
                    initial_points=float(rules.get("initial_points") or 0.0),
                    trail_points=float(rules.get("trail_points") or 0.0),
                    trail_after=float(rules.get("trail_after") or 0.0),
                    trail_points_after=float(rules.get("trail_points_after") or 0.0),
                ),
            )
        position_id = db.open_position(
            symbol=symbol,
            quantity=signed_quantity,
            price=fill_price,
            mode=mode,
            source=source,
            strategy=str(payload.get("strategy") or strategy_id),
            strategy_id=strategy_id,
            sl_price=initial_stop,
            initial_sl_price=initial_stop,
            trailing_stop=initial_stop,
            best_price=fill_price,
            notes=json.dumps({k: payload.get(k) for k in ("signal_id", "interval", "timestamp") if payload.get(k)}),
        )
        db.record_order(
            symbol=symbol, transaction_type=action, quantity=filled_quantity, price=fill_price,
            order_type=settings.order_type, product=settings.product,
            status=status, mode=mode, source=source, strategy_id=strategy_id,
            exchange_order_id=order_id, position_id=position_id,
            message=ack.get("message"),
        )
        if order_id:
            order_ids.append(str(order_id))
        # Hand the real fill to the shadow book. This is the whole point of the
        # mirror: it prices its own fill from the price the broker actually gave,
        # so the resulting difference is the paper model's slippage error rather
        # than a difference in market data. Failures here must never affect a
        # live order, so nothing is allowed to propagate.
        if mode == LIVE:
            try:
                from paper_mirror import get_mirror

                get_mirror().note_fill(
                    symbol=symbol,
                    transaction_type=action,
                    quantity=filled_quantity,
                    price=fill_price,
                    kind="ENTRY",
                )
            except Exception:
                log.exception("Could not hand a live fill to the shadow book")
        if mode == PAPER:
            self._mark_equity()

        pending_fill = status in {"OPEN", "TRIGGER_PENDING", "PENDING"}
        filled_lots = filled_quantity / lot_size
        return ExecutionResult(
            not pending_fill, action, mode, symbol, quantity=filled_quantity, price=fill_price,
            reason=(
                f"Order remains unresolved ({filled_quantity}/{qty} filled)"
                if pending_fill
                else f"Opened {filled_lots:g} lot(s) {'LONG' if signed_quantity > 0 else 'SHORT'} @ {fill_price:.2f}{partial_note}"
            ),
            position_id=position_id, order_ids=tuple(order_ids),
            details={
                "lots": filled_lots,
                "requested_quantity": qty,
                "filled_quantity": filled_quantity,
                "partial": filled_quantity < qty,
                "fill_source": self.client.data_source(),
                "fill_model": simulated.source if simulated is not None else "broker",
                "levels_touched": simulated.levels_touched if simulated is not None else None,
            },
        )

    def square_off(
        self,
        reason: str = "MANUAL",
        source: str = "MANUAL",
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        mark_price: Optional[float] = None,
        position_id: Optional[int] = None,
        strategy_id: Optional[str] = None,
    ) -> ExecutionResult:
        """Close open positions, narrowed by symbol and/or strategy.

        ``strategy_id`` is the guard that keeps a two-strategy deployment safe:
        without it, one book's exit liquidates the other's position in the same
        contract. ``None`` means every book, which is only correct for PANIC -
        the operator's square-off must close everything, not one strategy's leg.

        A ``symbol`` filter also matches a leg the engine has rolled away from,
        and one whose contract has stopped trading. The engine names the *next*
        month once the rollover window opens, so a FLAT signal arrives carrying a
        symbol the held leg does not have - and filtering strictly on it would
        report "No open positions" while the exposure is still there. An exit
        that cannot reach its position is worse than one that closes a little
        more than asked.

        ``position_id`` narrows that to exactly one row. A stop-loss is a
        per-position limit, so the widening above is wrong for it: a stop on a
        symbol that is not a futures contract (a test row, or the index book's
        own ledger) would otherwise sweep in the front-month leg as a
        "rolled-away" position and close a trade that never breached anything.
        """
        mode = mode or get_execution_mode()
        positions = db.get_open_positions(strategy_id=strategy_id)
        if position_id is not None:
            # A stop-out names one row. No widening: the breach is that position
            # and nothing else, so an unrelated leg must not be closed with it.
            positions = [
                p for p in positions
                if p["id"] == position_id and p["mode"] == mode
            ]
            if not positions:
                return ExecutionResult(
                    True, "FLAT", mode, symbol or "*",
                    reason="Position already closed",
                )
        else:
            reachable = {p["id"] for p in self._expired_positions(mode, strategy_id)}
            if symbol is not None:
                # The old leg of a rollover: still tradeable, but no longer the
                # contract the engine names, so a symbol filter would walk past it.
                reachable.update(
                    p["id"] for p in self._rolled_away_positions(symbol, mode, strategy_id)
                )
            positions = [
                p for p in positions
                if p["mode"] == mode
                and (symbol is None or p["symbol"] == symbol or p["id"] in reachable)
            ]
        sync_error = self._live_position_sync_error() if mode == LIVE else None
        if sync_error:
            return ExecutionResult(False, "FLAT", mode, symbol or "*", reason=sync_error)
        pending = db.get_pending_orders(mode, symbol)
        if pending:
            return ExecutionResult(
                False, "FLAT", mode, symbol or "*",
                reason=f"Unresolved order {pending[-1]['exchange_order_id'] or pending[-1]['id']}",
                order_ids=tuple(
                    str(row["exchange_order_id"])
                    for row in pending
                    if row.get("exchange_order_id")
                ),
            )
        if not positions:
            return ExecutionResult(True, "FLAT", mode, symbol or "*", reason="No open positions")

        order_ids: list[str] = []
        closed_count = 0
        failures: list[str] = []
        last_symbol = positions[0]["symbol"]
        for pos in positions:
            mark = mark_price or self.client.get_ltp(pos["symbol"]) or pos["last_price"] or pos["entry_price"]
            side = SELL if pos["position_key"] == 1 else BUY
            fill = apply_slippage(float(mark), side)
            ack = self.client.place_order(
                symbol=pos["symbol"], transaction_type=side, quantity=pos["quantity"],
                price=fill, order_type=settings.order_type, product=settings.product,
                tag="niftybot", mode=mode,
            )
            if ack.get("order_id"):
                order_ids.append(str(ack["order_id"]))
            status = str(ack.get("status") or "REJECTED").upper().replace(" ", "_")
            try:
                filled_quantity = int(ack.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                filled_quantity = 0
            if status == "COMPLETE" and filled_quantity == 0:
                filled_quantity = int(pos["quantity"])
            if filled_quantity < 0 or filled_quantity > int(pos["quantity"]):
                failures.append(f"{pos['symbol']}: invalid filled quantity {filled_quantity}")
                filled_quantity = 0
            # The same book-walking fill model as entries: a paper exit fills at
            # the visible bid/ask touch and only books the portion the book shows.
            if mode == PAPER and status == "COMPLETE" and filled_quantity > 0:
                simulated = self._paper_fill(
                    pos["symbol"], side, filled_quantity, float(mark), ack.get("depth")
                )
                if simulated.filled_quantity < filled_quantity:
                    failures.append(
                        f"{pos['symbol']}: partial fill {simulated.filled_quantity}/{filled_quantity}"
                    )
                filled_quantity = simulated.filled_quantity
                fill = simulated.price
            if filled_quantity == 0:
                db.record_order(
                    symbol=pos["symbol"], transaction_type=side, quantity=pos["quantity"],
                    price=fill, order_type=settings.order_type, product=settings.product,
                    status=status, mode=mode, source=source, strategy_id=strategy_id,
                    position_id=pos["id"], message=ack.get("message"),
                )
                failures.append(
                    f"{pos['symbol']}: exit not filled (status {status})"
                )
                continue

            exit_price = float(ack.get("average_price") or fill)
            turnover_buy = exit_price * filled_quantity if side == BUY else pos["entry_price"] * filled_quantity
            turnover_sell = exit_price * filled_quantity if side == SELL else pos["entry_price"] * filled_quantity
            charges = estimate_charges(turnover_buy, turnover_sell)
            try:
                trade = db.close_position(
                    pos["id"], exit_price, fees=charges, exit_reason=reason,
                    quantity=filled_quantity,
                )
            except ValueError as exc:
                # The engine loop and the mark loop can both reach the same
                # position in the same tick. The loser of that race finds the
                # row already closed - which is the outcome it wanted, not a
                # failure. Reporting it as one made the engine log "exit NOT
                # confirmed" and retry a position that was already flat.
                if "already closed" not in str(exc):
                    raise
                log.info(
                    "Position %s was already closed by another check; treating as done",
                    pos["id"],
                )
                continue
            db.record_order(
                symbol=pos["symbol"], transaction_type=side, quantity=filled_quantity,
                price=exit_price, order_type=settings.order_type, product=settings.product,
                status=status, mode=mode, source=source, strategy_id=strategy_id,
                exchange_order_id=ack.get("order_id"), position_id=pos["id"],
                message=f"{reason} | net PnL {trade['net_pnl']:+.2f} | fees {charges:.2f}",
            )
            if filled_quantity == int(pos["quantity"]):
                closed_count += 1
            else:
                failures.append(
                    f"{pos['symbol']}: closed {filled_quantity}/{pos['quantity']}; "
                    "remaining exposure is still open"
                )
            if status in {"OPEN", "TRIGGER_PENDING", "PENDING"}:
                failures.append(f"{pos['symbol']}: exit order remains unresolved")
            # Feed the real exit fill to the shadow book so the paper model is
            # graded against the broker's price, not a quote taken later.
            if mode == LIVE:
                try:
                    from paper_mirror import get_mirror

                    get_mirror().note_fill(
                        symbol=pos["symbol"],
                        transaction_type=side,
                        quantity=filled_quantity,
                        price=exit_price,
                        kind="EXIT",
                    )
                except Exception:
                    log.exception("Could not hand a live fill to the shadow book")
            last_symbol = pos["symbol"]

        if closed_count and mode == PAPER:
            self._mark_equity()

        return ExecutionResult(
            not failures, "FLAT", mode, last_symbol,
            reason=(
                f"Squared off {closed_count} position(s) ({reason})"
                if not failures
                else f"Square-off incomplete: {'; '.join(failures)}"
            ),
            order_ids=tuple(order_ids),
            details={"closed_positions": closed_count, "failures": failures},
        )

    # -- valuation -------------------------------------------------------
    def price_map(self) -> dict[str, float]:
        """Mark price per open symbol, always the future's own last price.

        PAPER used to substitute the GIFT Nifty level here once the NSE closed,
        on the reasoning that an after-hours PAPER fill is priced from the GIFT
        close and should be marked the same way. The effect was that the futures
        book kept *moving* after the close on an index that still ticks, so a
        position could show a PnL the future it holds could not possibly have
        produced - and it made the futures book disagree with the very LTP the
        dashboard showed for the same contract.

        The substitute is gone, so both modes mark from `get_ltp`, which returns
        the last known price after hours and is frozen for a closed market.
        """
        prices: dict[str, float] = {}
        mode = get_execution_mode()
        positions = db.get_open_positions(mode=mode)
        for pos in positions:
            ltp = self.client.get_ltp(pos["symbol"])
            if ltp:
                prices[pos["symbol"]] = ltp
        return prices

    def _mark_equity(self) -> None:
        prices = self.price_map()
        positions = db.get_open_positions(mode=PAPER)
        metrics = db.get_performance_metrics(
            open_positions=positions, prices=prices, mode=PAPER
        )
        db.record_equity(metrics["equity"], metrics["balance"], metrics["unrealized_pnl"])

    def portfolio(self) -> dict[str, Any]:
        """Aggregate liquidation value for the active execution mode."""
        mode = get_execution_mode()
        prices = self.price_map()
        positions = db.get_open_positions(mode=mode)
        metrics = db.get_performance_metrics(
            open_positions=positions, prices=prices, mode=mode
        )
        live_cash = self.client.available_cash() if mode == LIVE else None
        if live_cash is not None:
            metrics["equity"] = live_cash + metrics["unrealized_pnl"]
        metrics["open_positions"] = positions
        metrics["prices"] = prices
        metrics["mode"] = mode
        metrics["market"] = market_status()
        metrics["data_source"] = self.client.data_source()
        metrics["kite_auth"] = self.client.auth_status()
        return metrics


_manager: Optional[OrderManager] = None


def get_order_manager() -> OrderManager:
    global _manager
    if _manager is None:
        _manager = OrderManager()
    return _manager


__all__ = [
    "OrderManager",
    "ExecutionResult",
    "get_order_manager",
    "estimate_charges",
    "charge_breakdown",
    "breakeven_points",
    "apply_slippage",
    "BUY",
    "SELL",
    "FLAT",
    "PANIC",
    "VALID_ACTIONS",
]
