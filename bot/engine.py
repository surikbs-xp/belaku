"""Strategy engine: evaluate ``st.pine`` on GIFT Nifty, execute on Nifty futures.

Signal source and execution venue are deliberately different instruments:

* **signals** come from GIFT Nifty (``NSEIX:GIFT NIFTY``) candles, loaded from
  Kite history by :mod:`gift_nifty` (tick-built bars are a fallback). GIFT Nifty
  is the 20-hour pre-market proxy for Nifty 50, so it reacts to overnight and
  early-morning global moves before NSE opens.
* **orders** go to the front-month NFO Nifty future via
  :class:`~order_manager.OrderManager`, because GIFT Nifty is quote-only on
  Kite and cannot be traded there.

The engine **evaluates** on every closed GIFT Nifty bar, around the clock.
LIVE orders execute only inside the NSE F&O session (09:15-15:30 IST). PAPER
orders can also simulate after-hours using the just-closed GIFT candle; a
deferred LIVE signal is never auto-executed later.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import database as db
import risk
import strategy
from config import PAPER, get_execution_mode, settings, strategy_params
from gift_nifty import GIFT_KEY, get_feed
from kite_client import IST, get_client, market_status
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


def required_bars() -> int:
    """Bars ``strategy.evaluate`` needs before it will produce a verdict."""
    return strategy.required_bars(strategy_params().get("supertrends"))


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
        # Memoised dashboard verdicts, keyed by (bar timestamp, candle count).
        self._verdict_cache: dict[str, tuple[tuple[str, int], strategy.StrategyVerdict]] = {}
        # Latched risk breach. Sticky on purpose: a limit that clears itself
        # when price recovers turns one bad day into a run of them.
        self._breach: Optional[risk.Breach] = None

    # -- risk ------------------------------------------------------------
    @property
    def breach(self) -> Optional[risk.Breach]:
        """The latched risk breach, if any."""
        return self._breach

    def clear_breach(self) -> None:
        """Operator acknowledgement. Resumes entries; does not close anything."""
        with self._lock:
            if self._breach is not None:
                log.warning("Risk breach cleared by operator: %s", self._breach.detail)
            self._breach = None

    def enforce_risk(self) -> Optional[dict[str, Any]]:
        """Apply the risk limits. Returns the breach dict when one is active.

        Called from the loop rather than from signal handling so it runs even
        when no signal fires - a position drifting against entry produces no
        signal at all, which is exactly the case a stop-loss exists for.
        """
        mode = get_execution_mode()
        manager = get_order_manager()

        if self._breach is not None:
            return self._breach.to_dict()

        breach = risk.evaluate(mode)
        if breach is None:
            return None

        with self._lock:
            self._breach = breach
        log.error("RISK LIMIT BREACHED: %s", breach.detail)

        # Close only the offending symbol. A stop-loss is a per-position limit,
        # so flattening unrelated positions would be a bigger action than the
        # breach calls for.
        symbol = breach.symbol
        try:
            result = manager.square_off(
                reason=breach.kind, source=f"RISK_{breach.kind}", symbol=symbol
            )
            log.error(
                "Risk square-off (%s): ok=%s %s",
                breach.kind, result.ok, result.reason,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Risk square-off failed")
            result = None

        # Pause regardless of whether the square-off succeeded. Leaving entries
        # open after a breach would let the strategy re-enter the same position.
        manager.pause(f"{breach.kind}: {breach.detail}")

        payload = breach.to_dict()
        payload["square_off_ok"] = bool(result and result.ok)
        payload["square_off_reason"] = result.reason if result else "square-off raised"
        payload["paused"] = manager.paused
        return payload

    def _opposes_open_paper_position(self, signal: str) -> bool:
        if get_execution_mode() != PAPER or signal not in {strategy.BULL, strategy.BEAR}:
            return False

        symbol = get_order_manager().current_symbol()
        target = 1 if signal == strategy.BULL else -1
        quantity = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(mode=PAPER)
            if position["symbol"] == symbol
        )
        return quantity * target < 0

    # -- evaluation ------------------------------------------------------
    def evaluate_now(self, force: bool = False) -> dict[str, Any]:
        """Evaluate the current GIFT Nifty series once.

        ``force`` re-evaluates the newest bar even if it was already seen
        (used by the API for an on-demand check).
        """
        feed = get_feed()
        # Kite history is authoritative; refresh it so the indicators run on
        # exchange OHLC rather than tick-built approximations.
        try:
            feed.refresh_history()
        except Exception as exc:  # pragma: no cover - network
            log.warning("GIFT Nifty history refresh failed: %s", exc)
        candles = feed.candles()
        params = strategy_params()
        needed = required_bars()
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
        initial_bar = self._last_bar_at is None
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

        last_candle = candles[-1]
        ohlc = {
            key: last_candle.get(key)
            for key in ("open", "high", "low", "close")
        }
        log.info(
            "Indicator values source=%s interval_seconds=%s candle_at=%s "
            "history_bars=%d ohlc=%s indicators=%s signal=%s",
            GIFT_KEY,
            feed.seconds,
            newest,
            verdict.bars,
            json.dumps(ohlc, sort_keys=True),
            json.dumps(verdict.values, sort_keys=True),
            verdict.signal,
        )

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

        if initial_bar:
            if not self._opposes_open_paper_position(signal):
                return {
                    "ok": True,
                    "signal": signal,
                    "detail": verdict.detail,
                    "action": None,
                    "reason": "existing signal primed; waiting for the next transition",
                }
            previous = strategy.NEUTRAL

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
        closed_at = datetime.fromisoformat(newest) + timedelta(seconds=feed.seconds)
        result = self.execute(
            action,
            verdict.detail,
            paper_reference_price=verdict.close,
            signal_timestamp=closed_at,
        )
        return {"ok": True, "signal": signal, "detail": verdict.detail, "action": action,
                "result": result}

    # -- execution -------------------------------------------------------
    def execute(
        self,
        action: str,
        detail: str = "",
        *,
        paper_reference_price: Optional[float] = None,
        signal_timestamp: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Place a signal, using a GIFT-close fill only for after-hours PAPER."""
        manager = get_order_manager()
        mode = get_execution_mode()
        symbol = manager.current_symbol()
        market = market_status()
        paper_after_hours = mode == PAPER and not market["is_open"]

        if not market["is_open"] and not paper_after_hours:
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

        if paper_after_hours and (
            paper_reference_price is None
            or paper_reference_price <= 0
            or signal_timestamp is None
        ):
            reason = "After-hours PAPER signal needs a closed-candle price and timestamp"
            with self._lock:
                self.state.orders_blocked += 1
                self.state.last_error = reason
                self.state.last_result = {"ok": False, "action": action, "reason": reason}
            db.record_signal(action, f"GIFT {detail}".strip(), False, reason)
            log.error("%s", reason)
            return {"ok": False, "reason": reason}

        if manager.paused:
            with self._lock:
                self.state.orders_blocked += 1
            reason = f"{action} blocked: strategy paused"
            db.record_signal(action, f"GIFT {detail}".strip(), False, reason)
            log.warning("%s", reason)
            return {"ok": False, "reason": reason}

        price = (
            paper_reference_price
            if paper_after_hours
            else get_client().latest_known_price(symbol)
        )
        payload: dict[str, Any] = {
            "action": action,
            "symbol": symbol,
            "qty_lots": settings.default_lots,
            "signal_id": f"GIFT-{action}-{self._last_bar_at}",
            "strategy": "Surikbs",
            "source": "GIFT_NIFTY_ENGINE",
            "gift_detail": detail,
            "paper_after_hours": paper_after_hours,
            "max_age_seconds": 300,
        }
        if signal_timestamp is not None:
            payload["timestamp"] = signal_timestamp.astimezone(IST).isoformat()
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
                self.enforce_risk()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Risk check failed")
                with self._lock:
                    self.state.last_error = f"risk check: {exc}"
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

    def _cached_verdict(
        self,
        slot: str,
        candles: list[dict[str, Any]],
        bar_at: Optional[str],
    ) -> Optional[strategy.StrategyVerdict]:
        """Evaluate ``candles`` once per bar, reusing the result across polls.

        Keyed on the bar timestamp plus the candle count, so a new bar or a
        backfilled history invalidates it. Returns ``None`` while the series is
        still too short to evaluate.
        """
        if bar_at is None or len(candles) < required_bars():
            return None
        key = (bar_at, len(candles))
        with self._lock:
            cached = self._verdict_cache.get(slot)
            if cached is not None and cached[0] == key:
                return cached[1]
        try:
            verdict = strategy.evaluate(candles, strategy_params())
        except strategy.InsufficientData:
            return None
        with self._lock:
            self._verdict_cache[slot] = (key, verdict)
        return verdict

    def status(self) -> dict[str, Any]:
        feed = get_feed()
        try:
            feed.refresh_history()
        except Exception as exc:  # pragma: no cover - network
            log.warning("GIFT Nifty history refresh failed: %s", exc)
        candles, current_bar_in_progress = feed.candle_snapshot()
        closed_candles = candles[:-1] if current_bar_in_progress else candles
        previous_bar_at = closed_candles[-1]["time"] if closed_candles else None
        current_bar_at = candles[-1]["time"] if candles else None

        # The dashboard polls this every 1.5s but the inputs only change when a
        # bar closes, so the verdicts are memoised on the bar timestamp. This
        # turns a poll into a dict lookup instead of two full indicator passes.
        current_verdict = self._cached_verdict("current", candles, current_bar_at)
        previous_verdict = self._cached_verdict("previous", closed_candles, previous_bar_at)

        market = market_status()
        with self._lock:
            payload = self.state.to_dict()
        payload["feed"] = feed.status()
        payload["current_values"] = current_verdict.values if current_verdict else None
        payload["current_conditions"] = current_verdict.conditions if current_verdict else None
        payload["current_bar_at"] = current_bar_at
        payload["current_bar_in_progress"] = current_bar_in_progress
        payload["previous_values"] = previous_verdict.values if previous_verdict else None
        payload["previous_conditions"] = previous_verdict.conditions if previous_verdict else None
        payload["previous_bar_at"] = previous_bar_at
        payload["market"] = market
        payload["execution_symbol"] = get_order_manager().current_symbol()
        mode = get_execution_mode()
        payload["mode"] = mode
        payload["will_execute"] = bool(market["is_open"] or mode == PAPER)
        payload["paused"] = get_order_manager().paused
        payload["paused_reason"] = get_order_manager().paused_reason
        payload["risk"] = risk.limits()
        payload["risk_breach"] = self._breach.to_dict() if self._breach else None
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
