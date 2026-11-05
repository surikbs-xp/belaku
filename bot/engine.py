"""Strategy engine: evaluate ``st.pine`` on GIFT Nifty, execute on Nifty futures.

Signal source and execution venue are deliberately different instruments:

* **signals** come from GIFT Nifty (``NSEIX:GIFT NIFTY``) candles, built from
  live ticks by :mod:`gift_nifty`. GIFT Nifty is the 20-hour pre-market proxy
  for Nifty 50, so it reacts to overnight and early-morning global moves before
  NSE opens.
* **orders** go to the front-month NFO Nifty future via
  :class:`~order_manager.OrderManager`, because GIFT Nifty is quote-only on
  Kite and cannot be traded there.

Two clocks therefore matter, and they are not the same:

* the engine **evaluates** on every closed GIFT Nifty bar, around the clock;
* it only **executes** inside the NSE F&O session (09:15-15:30 IST), because
  outside it the future cannot be filled.

A signal that fires outside the window is recorded as ``deferred`` rather than
dropped, so the dashboard can show it and an operator can see why nothing
happened. It is deliberately *not* auto-executed at 09:15 - a stale direction
from a 03:00 move is not a trade, it is a guess.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import database as db
import strategy
from config import get_execution_mode, settings, strategy_params
from gift_nifty import get_feed
from kite_client import get_client, market_status
from order_manager import BUY, SELL, get_order_manager

log = logging.getLogger("engine")


@dataclass
class EngineState:
    """Last evaluation result and counters, surfaced on the dashboard."""

    last_bar_at: Optional[str] = None
    last_eval_at: Optional[str] = None
    last_signal: str = strategy.NEUTRAL
    last_detail: str = ""
    last_conditions: dict[str, bool] = field(default_factory=dict)
    last_values: dict[str, Any] = field(default_factory=dict)
    evaluations: int = 0
    signals_fired: int = 0
    orders_sent: int = 0
    orders_blocked: int = 0
    deferred: int = 0
    skipped: int = 0
    last_action: Optional[str] = None
    last_action_at: Optional[str] = None
    last_result: Optional[dict[str, Any]] = None
    last_error: Optional[str] = None
    warmup_needed: int = 0
    warmup_have: int = 0
    ready: bool = False
    interval_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_bar_at": self.last_bar_at,
            "last_eval_at": self.last_eval_at,
            "last_signal": self.last_signal,
            "last_detail": self.last_detail,
            "conditions": self.last_conditions,
            "values": self.last_values,
            "evaluations": self.evaluations,
            "signals_fired": self.signals_fired,
            "orders_sent": self.orders_sent,
            "orders_blocked": self.orders_blocked,
            "deferred": self.deferred,
            "skipped": self.skipped,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "warmup_needed": self.warmup_needed,
            "warmup_have": self.warmup_have,
            "ready": self.ready,
        }


def required_bars(params: Optional[dict[str, Any]] = None) -> int:
    """Bars ``strategy.evaluate`` needs before it will produce a verdict."""
    cfg = {**strategy.DEFAULTS, **(params or strategy_params())}
    return max(
        int(cfg["rsi_length"]) + 1,
        int(cfg["tsv_length"]) + 1,
    ) + 5


class StrategyEngine:
    """Watches GIFT Nifty bars and trades the Nifty future on signal changes.

    Deduplication mirrors the Pine script: a signal is acted on only when the
    condition *becomes* true (``not longCondition[1]``). ``_last_signal`` holds
    the previous bar's verdict, so a run of BULL bars fires exactly one entry.
    """

    def __init__(self) -> None:
        self.state = EngineState()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_signal = strategy.NEUTRAL
        self._last_bar_at: Optional[str] = None

    # -- evaluation ------------------------------------------------------
    def evaluate_now(self, force: bool = False) -> dict[str, Any]:
        """Evaluate the current GIFT Nifty series once.

        ``force`` re-evaluates the newest bar even if it was already seen
        (used by the API for an on-demand check).
        """
        feed = get_feed()
        candles = feed.candles()
        params = strategy_params()
        needed = required_bars(params)
        with self._lock:
            self.state.warmup_needed = needed
            self.state.warmup_have = len(candles)

        if len(candles) < needed:
            with self._lock:
                self.state.ready = False
                self.state.last_error = None
                self.state.skipped += 1
            return {
                "ok": False,
                "reason": f"warming up: {len(candles)}/{needed} GIFT Nifty bars",
                "ready": False,
                "have": len(candles),
                "needed": needed,
            }

        newest = candles[-1]["time"]
        if not force and newest == self._last_bar_at:
            return {"ok": True, "reason": "no new closed bar", "skipped": True}
        self._last_bar_at = newest

        try:
            verdict = strategy.evaluate(candles, params)
        except strategy.InsufficientData as exc:
            with self._lock:
                self.state.ready = False
                self.state.last_error = str(exc)
            return {"ok": False, "reason": f"insufficient history: {exc}"}

        with self._lock:
            self.state.ready = True
            self.state.last_error = None
            self.state.last_bar_at = newest
            self.state.last_eval_at = db.iso()
            self.state.last_signal = verdict.signal
            self.state.last_detail = verdict.detail
            self.state.last_conditions = verdict.conditions
            self.state.last_values = verdict.values
            self.state.evaluations += 1

        signal = verdict.signal
        previous = self._last_signal
        self._last_signal = signal

        # Only act on a fresh transition, exactly like the Pine `not cond[1]`.
        if signal == previous or signal == strategy.NEUTRAL:
            return {
                "ok": True,
                "signal": signal,
                "detail": verdict.detail,
                "action": None,
                "reason": "no signal change" if signal == previous else "neutral",
            }

        action = BUY if signal == strategy.BULL else SELL
        with self._lock:
            self.state.signals_fired += 1
        result = self.execute(action, verdict.detail)
        return {"ok": True, "signal": signal, "detail": verdict.detail, "action": action,
                "result": result}

    # -- execution -------------------------------------------------------
    def execute(self, action: str, detail: str = "") -> dict[str, Any]:
        """Place ``action`` on the Nifty future, respecting the NSE session."""
        manager = get_order_manager()
        mode = get_execution_mode()
        symbol = manager.current_symbol()
        market = market_status()

        if not market["is_open"]:
            with self._lock:
                self.state.deferred += 1
                self.state.last_action = f"{action} deferred"
                self.state.last_action_at = db.iso()
            reason = (
                f"{action} signal from GIFT Nifty held: NSE F&O closed "
                f"({market['reason']}). Not auto-firing later."
            )
            db.record_signal(action, f"GIFT {detail}".strip(), False, reason)
            with self._lock:
                self.state.last_result = {"ok": False, "action": action, "reason": reason,
                                          "deferred": True}
            log.warning("%s", reason)
            return {"ok": False, "deferred": True, "reason": reason}

        if manager.paused:
            with self._lock:
                self.state.orders_blocked += 1
            reason = f"{action} blocked: strategy paused"
            db.record_signal(action, f"GIFT {detail}".strip(), False, reason)
            log.warning("%s", reason)
            return {"ok": False, "reason": reason}

        price = get_client().latest_known_price(symbol)
        payload: dict[str, Any] = {
            "action": action,
            "symbol": symbol,
            "qty_lots": settings.default_lots,
            "signal_id": f"GIFT-{action}-{self._last_bar_at}",
            "strategy": "Surikbs",
            "source": "GIFT_NIFTY_ENGINE",
            "gift_detail": detail,
        }
        if price:
            payload["price"] = price

        try:
            result = manager.process_signal(payload, source="ENGINE")
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Engine execution failed")
            with self._lock:
                self.state.last_error = str(exc)
            return {"ok": False, "reason": f"execution error: {exc}"}

        data = result.to_dict()
        with self._lock:
            self.state.orders_sent += 1 if result.ok else 0
            self.state.orders_blocked += 0 if result.ok else 1
            self.state.last_action = action
            self.state.last_action_at = db.iso()
            self.state.last_result = data
        log.info("Engine %s -> ok=%s %s", action, result.ok, result.reason)
        return data

    # -- loop ------------------------------------------------------------
    def loop(self, stop: threading.Event, interval: Optional[float] = None) -> None:
        """Evaluate whenever a new GIFT Nifty bar closes.

        The loop is cheap: :meth:`evaluate_now` returns immediately unless the
        newest bar timestamp changed, so a short poll interval costs one
        comparison, not a re-evaluation.
        """
        period = float(interval if interval is not None else settings.engine_interval_seconds)
        with self._lock:
            self.state.interval_seconds = period
        log.info("Strategy engine started (poll %.1fs, GIFT Nifty bars)", period)
        while not stop.wait(period):
            try:
                self.evaluate_now()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Engine evaluation failed")
                with self._lock:
                    self.state.last_error = str(exc)
        log.info("Strategy engine stopped")

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.loop, args=(self._stop,), name="strategy-engine", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        feed = get_feed()
        market = market_status()
        with self._lock:
            payload = self.state.to_dict()
        payload["feed"] = feed.status()
        payload["market"] = market
        payload["execution_symbol"] = get_order_manager().current_symbol()
        payload["mode"] = get_execution_mode()
        payload["will_execute"] = bool(market["is_open"])
        payload["paused"] = get_order_manager().paused
        return payload


_engine: Optional[StrategyEngine] = None
_engine_lock = threading.Lock()


def get_engine() -> StrategyEngine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = StrategyEngine()
        return _engine


__all__ = ["EngineState", "StrategyEngine", "get_engine", "required_bars"]
