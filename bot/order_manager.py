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
from config import LIVE, PAPER, get_execution_mode, settings
from kite_client import KiteClient, future_symbol, get_client, is_market_open, market_status

log = logging.getLogger("order_manager")

BUY = "BUY"
SELL = "SELL"
FLAT = "FLAT"
PANIC = "PANIC"
VALID_ACTIONS = {BUY, SELL, FLAT, PANIC}

# Indicative NSE Nifty-futures cost model used for paper accounting (INR).
BROKERAGE_PER_ORDER = 20.0
STT_RATE_SELL = 0.000125      # 0.0125% on sell turnover
EXCHANGE_TXN_RATE = 0.0000173  # 0.00173% on turnover
SEBI_RATE = 0.000001           # Rs 10 per crore
STAMP_RATE_BUY = 0.00002       # 0.002% on buy turnover
GST_RATE = 0.18                # on brokerage + transaction + SEBI


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
def estimate_charges(turnover_buy: float, turnover_sell: float, orders: int = 2) -> float:
    """Approximate F&O cost stack for a round trip (per order bookkeeping)."""
    brokerage = BROKERAGE_PER_ORDER * orders
    exchange = EXCHANGE_TXN_RATE * (turnover_buy + turnover_sell)
    sebi = SEBI_RATE * (turnover_buy + turnover_sell)
    stt = STT_RATE_SELL * turnover_sell
    stamp = STAMP_RATE_BUY * turnover_buy
    gst = GST_RATE * (brokerage + exchange + sebi)
    return round(brokerage + exchange + sebi + stt + stamp + gst, 2)


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

    @property
    def client(self) -> KiteClient:
        if self._client is None:
            self._client = get_client()
        self._client.mode = get_execution_mode()
        return self._client

    # -- utilities -------------------------------------------------------
    def current_symbol(self, now: Optional[datetime] = None) -> str:
        if get_execution_mode() == LIVE:
            contract = self.client.front_month_contract(now)
            return str(contract["tradingsymbol"]) if contract else ""
        return future_symbol(now)

    def _force_off_hours(self) -> bool:
        """Testing/ops escape hatch: TRADING_ALLOW_OFF_HOURS=1."""
        import os

        return os.getenv("TRADING_ALLOW_OFF_HOURS", "0").lower() in {"1", "true", "yes"}

    def _price(
        self, symbol: str, payload: dict[str, Any], allow_payload_price: bool
    ) -> Optional[float]:
        if allow_payload_price and payload.get("paper_after_hours") is True:
            try:
                price = float(payload.get("price"))
            except (TypeError, ValueError):
                return None
            return price if price > 0 else None
        ltp = self.client.get_ltp(symbol)
        if ltp:
            return float(ltp)
        if allow_payload_price:
            for key in ("price", "ltp", "close"):
                if payload.get(key) not in (None, ""):
                    try:
                        value = float(payload[key])
                    except (TypeError, ValueError):
                        continue
                    self.client.remember_tick(symbol, value)
                    return value
        return None

    def _lot_size(self, symbol: str, mode: str) -> int:
        if mode == LIVE:
            instrument = self.client.instrument_for_symbol(symbol)
            if not instrument or instrument.get("instrument_type") != "FUT":
                raise ValueError(f"{symbol} is not a verified NIFTY futures contract")
            lot_size = int(instrument.get("lot_size") or 0)
            if lot_size <= 0:
                raise ValueError(f"Kite returned an invalid lot size for {symbol}")
            contract = self.client.front_month_contract()
            if not contract or contract.get("tradingsymbol") != symbol:
                raise ValueError(f"{symbol} is not the current front-month NIFTY future")
            return lot_size
        return settings.lot_size

    def _current_net_lots(self, symbol: str, mode: str, lot_size: int) -> int:
        quantity = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(mode=mode)
            if position["symbol"] == symbol
        )
        if quantity % lot_size:
            raise ValueError(
                f"Ledger quantity {quantity} for {symbol} is not a whole number of {lot_size}-unit lots"
            )
        return quantity // lot_size

    def _live_position_sync_error(self) -> Optional[str]:
        if get_execution_mode() != LIVE:
            return None
        try:
            broker_positions = self.client.positions()
            future_symbols = {
                str(instrument["tradingsymbol"])
                for instrument in self.client.nifty_future_instruments()
                if instrument.get("tradingsymbol")
            }
        except Exception as exc:
            return f"Could not reconcile Zerodha positions: {exc}"
        if not future_symbols:
            return "Could not load NIFTY futures instruments to reconcile Zerodha positions"

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

    def _live_sync_state(self) -> Optional[dict[str, Any]]:
        """Broker-vs-ledger net quantity for the traded contract, when readable.

        This is the authoritative position source for LIVE. A mismatch is not
        auto-corrected: silently rewriting the ledger could hide a real loss, so
        it blocks trading until a human reconciles.
        """
        if get_execution_mode() != LIVE:
            return None
        symbol = self.current_symbol()
        try:
            broker = self.client.net_position(symbol)
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

    # -- public API ------------------------------------------------------
    def process_signal(self, payload: dict[str, Any], source: str = "WEBHOOK") -> ExecutionResult:
        """Validate and route a signal. Never raises for business rejections."""
        action = str(payload.get("action") or payload.get("signal") or "").strip().upper()
        signal_id = str(payload.get("signal_id") or payload.get("id") or "").strip() or None
        mode = get_execution_mode()
        symbol = str(payload.get("symbol") or payload.get("ticker") or "").strip().upper()
        symbol = symbol or self.current_symbol()

        def reject(reason: str) -> ExecutionResult:
            db.record_signal(action or "UNKNOWN", json.dumps(payload, default=str), False, reason)
            log.warning("Rejected %s signal: %s", action or "?", reason)
            return ExecutionResult(False, action or "UNKNOWN", mode, symbol, reason=reason)

        if action not in VALID_ACTIONS:
            return reject(f"Unknown action {action!r}; expected one of {sorted(VALID_ACTIONS)}")

        if self._paused and action in {BUY, SELL}:
            return reject(f"Strategy paused ({self._paused_reason or 'PAUSED'}); {action} ignored")

        if signal_id and self._already_processed(signal_id, mode):
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
                    result = self.square_off("SIGNAL_FLAT", source=source, symbol=symbol, mode=mode)
                elif action == PANIC:
                    result = self.square_off("PANIC", source=source, mode=mode)
                else:
                    result = self._execute_directional(payload, action, symbol, mode, source)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Execution failure")
                return reject(f"Execution error: {exc}")

        db.record_signal(
            action,
            json.dumps({"signal_id": signal_id, **payload}, default=str),
            result.ok,
            result.reason,
        )
        return result

    def _already_processed(self, signal_id: str, mode: str) -> bool:
        needle = f'"signal_id": "{signal_id}"'
        for row in db.get_signals(limit=200):
            if row["accepted"] and needle in (row["payload"] or ""):
                return True
        return False

    def _execute_directional(
        self,
        payload: dict[str, Any],
        action: str,
        symbol: str,
        mode: str,
        source: str,
    ) -> ExecutionResult:
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
            current = self._current_net_lots(symbol, mode, lot_size)
        except (TypeError, ValueError) as exc:
            return ExecutionResult(False, action, mode, symbol, reason=str(exc))
        if mode == LIVE:
            futures = {item["tradingsymbol"] for item in self.client.nifty_future_instruments()}
            other_positions = [
                position for position in db.get_open_positions(mode=LIVE)
                if position["symbol"] in futures and position["symbol"] != symbol
            ]
            if other_positions:
                open_symbols = ", ".join(sorted({position["symbol"] for position in other_positions}))
                return ExecutionResult(
                    False, action, mode, symbol,
                    reason=f"Existing NIFTY futures exposure on {open_symbols}; square it off before rollover",
                )
        target_lots = lots if action == BUY else -lots

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
            closed = self.square_off("FLIP", source=source, symbol=symbol, mode=mode, mark_price=price)
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
        ack = self.client.place_order(
            symbol=symbol,
            transaction_type=action,
            quantity=qty,
            price=fill,
            order_type=settings.order_type,
            product=settings.product,
            tag="niftybot",
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
        if filled_quantity == 0:
            db.record_order(
                symbol=symbol, transaction_type=action, quantity=qty, price=fill,
                order_type=settings.order_type, product=settings.product,
                status=status, mode=mode, source=source,
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
        signed_quantity = filled_quantity if action == BUY else -filled_quantity
        position_id = db.open_position(
            symbol=symbol,
            quantity=signed_quantity,
            price=fill_price,
            mode=mode,
            source=source,
            strategy=str(payload.get("strategy") or "Surikbs"),
            notes=json.dumps({k: payload.get(k) for k in ("signal_id", "interval", "timestamp") if payload.get(k)}),
        )
        db.record_order(
            symbol=symbol, transaction_type=action, quantity=filled_quantity, price=fill_price,
            order_type=settings.order_type, product=settings.product,
            status=status, mode=mode, source=source,
            exchange_order_id=order_id, position_id=position_id,
            message=ack.get("message"),
        )
        if order_id:
            order_ids.append(str(order_id))
        if mode == PAPER:
            self._mark_equity()

        pending_fill = status in {"OPEN", "TRIGGER_PENDING", "PENDING"}
        filled_lots = filled_quantity / lot_size
        return ExecutionResult(
            not pending_fill, action, mode, symbol, quantity=filled_quantity, price=fill_price,
            reason=(
                f"Order remains unresolved ({filled_quantity}/{qty} filled)"
                if pending_fill
                else f"Opened {filled_lots:g} lot(s) {'LONG' if signed_quantity > 0 else 'SHORT'} @ {fill_price:.2f}"
            ),
            position_id=position_id, order_ids=tuple(order_ids),
            details={
                "lots": filled_lots,
                "requested_quantity": qty,
                "filled_quantity": filled_quantity,
                "partial": filled_quantity < qty,
                "fill_source": self.client.data_source(),
            },
        )

    def square_off(
        self,
        reason: str = "MANUAL",
        source: str = "MANUAL",
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        mark_price: Optional[float] = None,
    ) -> ExecutionResult:
        """Close every open position (optionally only one symbol)."""
        mode = mode or get_execution_mode()
        positions = db.get_open_positions()
        positions = [
            p for p in positions
            if p["mode"] == mode and (symbol is None or p["symbol"] == symbol)
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
                tag="niftybot",
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
            if filled_quantity == 0:
                db.record_order(
                    symbol=pos["symbol"], transaction_type=side, quantity=pos["quantity"],
                    price=fill, order_type=settings.order_type, product=settings.product,
                    status=status, mode=mode, source=source,
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
            trade = db.close_position(
                pos["id"], exit_price, fees=charges, exit_reason=reason,
                quantity=filled_quantity,
            )
            db.record_order(
                symbol=pos["symbol"], transaction_type=side, quantity=filled_quantity,
                price=exit_price, order_type=settings.order_type, product=settings.product,
                status=status, mode=mode, source=source,
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
        prices: dict[str, float] = {}
        mode = get_execution_mode()
        positions = db.get_open_positions(mode=mode)
        market_open = is_market_open()
        gift_price: Optional[float] = None
        if not market_open and any(
            pos["mode"] == PAPER and pos["source"] == "ENGINE" for pos in positions
        ):
            from gift_nifty import get_feed

            feed = get_feed().status()
            candidate = feed.get("last_price")
            if candidate is None:
                candidate = feed.get("last_candle_close")
            try:
                parsed_price = float(candidate)
            except (TypeError, ValueError):
                parsed_price = 0.0
            if parsed_price > 0:
                gift_price = parsed_price

        for pos in positions:
            if (
                gift_price is not None
                and not market_open
                and pos["mode"] == PAPER
                and pos["source"] == "ENGINE"
            ):
                prices[pos["symbol"]] = gift_price
                continue
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
    "apply_slippage",
    "BUY",
    "SELL",
    "FLAT",
    "PANIC",
    "VALID_ACTIONS",
]
