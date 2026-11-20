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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

import database as db
import risk
import strategy
from config import (
    PAPER,
    get_execution_mode,
    settings,
    strategy_configs,
    strategy_params,
)
from bar_source import GiftBarSource
from gift_nifty import GIFT_KEY, get_feed
from strategy_run import StrategyRun
from kite_client import (
    IST,
    MARKET_OPEN,
    _expiry_has_passed,
    contract_expiry,
    future_symbol,
    get_client,
    market_status,
    rollover_status,
)
from order_manager import BUY, FLAT, SELL, ExecutionResult, get_order_manager
from portfolio import get_aggregator

log = logging.getLogger("engine")


def _trade_alert(
    kind: str,
    detail: str,
    severity: str = "info",
    symbol: Optional[str] = None,
    fields: Optional[dict[str, Any]] = None,
) -> None:
    """Send a trade notification. Never raises, never blocks the engine.

    Trade events are informational, not faults: they go out on the same channel
    as the watchdog's warnings so the operator sees the whole session in one
    place rather than having to open the dashboard to find out whether a signal
    was taken.

    ``fields`` is the structured context the notifier renders as labelled rows.
    A trade message without the strategy, the venue and the order id is not
    actionable: "BUY NIFTY26NOVFUT 65 @ 24180.50" does not say which of the three
    books took it, or whether it was real money.
    """
    try:
        import notifier

        if not notifier.configured():
            log.info(
                "Trade alert %s skipped: ALERT_ENABLED is off or the channel is "
                "not configured",
                kind,
            )
            return
        context = {k: v for k, v in (fields or {}).items() if v not in (None, "")}
        if symbol and "symbol" not in context:
            context["symbol"] = symbol
        delivered = notifier.send(
            {
                "kind": kind,
                "detail": detail,
                "severity": severity,
                "source": "belaku",
                "mode": get_execution_mode(),
                "symbol": symbol,
                "at_ist": db.iso(),
                "fields": context,
            }
        )
        # Logged either way. A trade alert that silently does nothing is
        # indistinguishable from one that was never sent, and "did the exit
        # notification go out?" has to be answerable from the log.
        log.info("Trade alert %s delivered=%s", kind, delivered)
    except Exception:  # pragma: no cover - defensive
        log.exception("Trade alert failed")


def _bar_closed_at(bar_at: Optional[str], seconds: int) -> Optional[datetime]:
    """Moment the bar timestamped ``bar_at`` stops trading.

    A bar timestamp is its *open* (the bucket start), so the close is that plus
    the interval - the 09:00 bar on 15-minute bars closes at 09:15. Returns None
    when the timestamp cannot be parsed.
    """
    if not bar_at:
        return None
    try:
        return datetime.fromisoformat(str(bar_at)) + timedelta(seconds=seconds)
    except ValueError:
        return None


@dataclass
class EngineState:
    """Last evaluation result and counters, surfaced on the dashboard."""

    last_bar_at: Optional[str] = None
    last_eval_at: Optional[str] = None
    last_signal: str = strategy.NO_SIGNAL
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
    # Front-month rollover state, so the dashboard can warn before an expiry
    # rather than only after entries start being refused.
    rollover: dict[str, Any] = field(default_factory=dict)
    rollover_paused: bool = False

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
            "rollover": self.rollover,
            "rollover_paused": self.rollover_paused,
        }


def required_bars() -> int:
    """Bars ``strategy.evaluate`` needs before it will produce a verdict."""
    return strategy.required_bars()


# Where the owed-entry latch lives. In the ``settings`` table rather than in
# memory because a restart must not lose it: the futures book defers an
# out-of-session signal to the next morning, and a process that bounced overnight
# has to know it still owes that entry. Losing the latch would leave the bot flat
# while the indicators point, with no way to recover but a manual restart.
PENDING_SIGNAL_KEY = "engine_pending_signal"
PENDING_SIGNAL_BAR_KEY = "engine_pending_signal_bar"


class StrategyEngine:
    """Watches GIFT Nifty bars and trades the Nifty future on fresh signals.

    Entry is *transition-driven*: the bot trades the bar on which the two rules
    start agreeing, not every bar on which they still agree. That is what makes an
    exit final - the signal in force when the position closed is not re-used, so
    the next trade needs the conditions to form again from scratch.

    A transition alone is not enough, because a signal can close while the NSE is
    shut and the executor drops it. So a fresh direction is *latched* as an owed
    entry and honoured on the first bar that can actually fill it. The latch is
    persisted, which is what lets the bot recover a deferred signal across a
    restart without anyone intervening.

    A ``NO_SIGNAL`` bar **is** the exit. The pair disagreeing is the only thing
    that closes a position: there is no stop-loss and no profit target, so the
    position is held for as long as the two rules keep agreeing and closed on the
    first bar they stop. The exit is a transition too - a run of disagreeing bars
    is one exit, not one per bar - and the bot then waits for the next agreement.
    """

    @property
    def strategy_id(self) -> str:
        """The ledger book this engine instance trades.

        Read from config so a two-strategy deployment can name its runs. It
        defaults to the legacy id, which is what every existing row carries, so
        a single-strategy deployment is bit-for-bit the same as before.
        """
        # getattr: tests and older callers stub ``settings`` with a partial
        # namespace, and a missing id must fall back to the default book rather
        # than raise on the order path.
        value = getattr(settings, "strategy_id", None) or db.DEFAULT_STRATEGY_ID
        return str(value).strip() or db.DEFAULT_STRATEGY_ID

    def _build_runs(self) -> list[StrategyRun]:
        """Every enabled strategy, from config.

        With nothing extra configured this returns exactly one run built from the
        flat keys, which is the single-strategy deployment this bot has always
        been. ``STRATEGY_2_SOURCE=nifty`` is what adds the second book, so the
        default is unchanged rather than merely compatible.
        """
        runs: list[StrategyRun] = []
        for cfg in strategy_configs():
            if not cfg.enabled:
                continue
            try:
                source = self._build_source(cfg)
            except Exception as exc:
                log.error(
                    "Strategy %s is configured but its source failed to build (%s); "
                    "skipping it rather than trading the wrong series",
                    cfg.id, exc,
                )
                continue
            runs.append(
                StrategyRun(
                    cfg.id,
                    source,
                    params=cfg.strategy_params(),
                    lots=cfg.lots,
                    display_name=cfg.name or cfg.id,
                    live=getattr(cfg, "live", False),
                    legs=getattr(cfg, "resolved_legs", None),
                )
            )
        if not runs:
            # Never leave the engine with nothing to trade: a misconfigured
            # filter should not silently stop a running bot.
            log.error("No enabled strategy resolved; falling back to the default book")
            runs.append(
                StrategyRun(
                    self.strategy_id,
                    GiftBarSource(get_feed()),
                    params=strategy_params(),
                    lots=getattr(settings, "default_lots", 1),
                )
            )
        return runs

    def _build_source(self, cfg: Any):
        """The bar source for one strategy.

        The GIFT source is built without a feed, so it resolves ``get_feed()`` on
        first use rather than at construction. That keeps every existing stub and
        reset of the feed working, and it is also what lets two GIFT strategies
        share the one process-global websocket instead of opening a second.
        """
        from config import build_bar_source_for

        # The module-level get_feed is resolved here rather than inside
        # build_bar_source_for so that a patched engine.get_feed (which is what
        # every test and the feed reset do) is the one that takes effect.
        feed = get_feed() if cfg.source.startswith("gift") else None
        return build_bar_source_for(cfg, gift_feed=feed)

    @property
    def run(self) -> StrategyRun:
        """The active run. Raises if the engine has none, which is a bug."""
        if not self.runs:
            raise RuntimeError("engine has no strategy run configured")
        return self.runs[0]

    def __init__(self) -> None:
        self.state = EngineState()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_signal = strategy.NO_SIGNAL
        # The book this engine trades. A single run today; the list exists so a
        # second strategy can be added without inventing a parallel set of
        # attributes. Everything below that looks per-strategy reads through it.
        self.runs: list[StrategyRun] = self._build_runs()
        self._last_bar_at: Optional[str] = None
        # Memoised dashboard verdicts, keyed by (bar timestamp, candle count).
        self._verdict_cache: dict[str, tuple[tuple[str, int], strategy.StrategyVerdict]] = {}
        # The most recent signal exit, kept for the dashboard. Reported, never
        # latched into a gate: an exit closes one trade and the strategy keeps
        # running, so this must not be able to block the next entry.
        self._breach: Optional[risk.Breach] = None
        # Rollover warning throttle and a once-only "expired" log line.
        self._last_rollover_warning: Optional[float] = None
        self._rollover_logged = False
        # The pre-open reconcile flag and the owed-entry latch live on each
        # StrategyRun, not here: they are per-book state, and a single field was
        # shared by every book. `_overnight_pending` survives only as the
        # *active* run's mirror so an existing caller keeps working - see
        # `_set_overnight_pending`.
        self._overnight_pending = False
        self._pending_signal: Optional[str] = None
        self._pending_signal_bar: Optional[str] = None
        self._pending_loaded = False

    def _set_overnight_pending(self, value: bool, run: Optional[StrategyRun] = None) -> None:
        """Set the pre-open reconcile flag on the book that owns it.

        One field for every book meant a pre-open bar in the GIFT series armed a
        reconcile for the NIFTY book as well, which would then decide the
        morning on a leg it does not hold. The engine-level attribute mirrors the
        active run, so the dashboard and existing callers still see one value.
        """
        book = run if run is not None else self.run
        book.state.overnight_pending = bool(value)
        if book is self.run:
            self._overnight_pending = bool(value)

    # -- pending entry latch ---------------------------------------------
    def _load_pending_signal(self, run: Optional[StrategyRun] = None) -> None:
        """Read the owed entry for one book from the ledger.

        Delegated to the run, whose latch key is scoped to its strategy id. Two
        strategies sharing one key would overwrite each other's deferred entry on
        every bar - one book's signal silently becoming the other's.
        """
        book = run if run is not None else self.run
        book.load_pending_signal()
        if book is self.run:
            self._pending_signal = book.pending_signal
            self._pending_signal_bar = book.pending_signal_bar

    def _set_pending_signal(
        self,
        signal: Optional[str],
        bar_at: Optional[str] = None,
        run: Optional[StrategyRun] = None,
    ) -> None:
        """Record (or clear) the direction an entry is still owed for.

        Written through to SQLite so the intent survives a restart, under a key
        scoped to this run's strategy id. See :class:`StrategyRun` for why the key
        cannot be shared.

        ``run`` names the book. Without it every write went to the *active* run,
        so a latch set by any book belonged to the first one: the entry was then
        consumed against that book's position and booked under its id. That is
        how a palette book's fill ended up in the wrong ledger - the position
        showed ``strategy: gift-flip`` but its ``strategy_id`` was the engine's
        legacy default, and no tile could find it.
        """
        book = run if run is not None else self.run
        book.set_pending_signal(signal, bar_at)
        if book is self.run:
            self._pending_signal = book.pending_signal
            self._pending_signal_bar = book.pending_signal_bar

    # -- risk ------------------------------------------------------------
    @property
    def breach(self) -> Optional[risk.Breach]:
        """The most recent signal exit, if any."""
        return self._breach

    def clear_breach(self) -> None:
        """Dismiss the reported exit. Does not close or reopen anything.

        Nothing is gated on this any more - it only clears what the dashboard
        shows - so it is housekeeping rather than the resume switch it used to be.
        """
        with self._lock:
            if self._breach is not None:
                log.info("Exit notice dismissed: %s", self._breach.detail)
            self._breach = None

    def reset_for_mode(self, mode: str) -> None:
        """Drop the strategy state that belongs to the mode being left.

        The *rules* are identical in PAPER and LIVE - ``strategy`` and ``risk``
        never read the mode - so the two modes cannot disagree about what a signal
        is. What can disagree is the bookkeeping around them, and every piece of it
        is per-mode:

        - ``_last_bar_at`` is the "already evaluated this bar" guard. Carried
          across a switch it makes the first bar in the new mode look seen, so
          that bar is skipped and its signal is lost.
        - ``_overnight_pending`` arms the open reconcile. Set in PAPER it would
          fire a reconcile in LIVE on a leg PAPER was carrying.
        - the owed-entry latch is persisted, so without clearing it a PAPER-era
          signal fills as a real order.

        Every one of these is per-book state, so this clears every run rather than
        only the active one. It used to touch the engine-level attributes alone,
        and those mirror ``runs[0]``: with two books the second kept its "already
        evaluated this bar" guard across the switch, so after arming LIVE it
        skipped the bar it had already seen and displayed the previous mode's
        indicators. Only the first book ever evaluated again.

        Clearing all four makes the switch a clean start: the new mode evaluates
        its own first bar and needs its own fresh signal, exactly as a restart
        does. Nothing is closed or opened here - an open position is untouched and
        is closed by the next flat signal, which is what makes this safe to call
        while trading.
        """
        with self._lock:
            for run in self.runs:
                run.state.last_bar_at = None
                run.state.last_action = None
                run.state.overnight_pending = False
                # Written through to SQLite, so a PAPER-era owed entry cannot be
                # filled as a real order by the live book.
                run.set_pending_signal(None)
            self._last_bar_at = None
            self._last_signal = strategy.NO_SIGNAL
            self._overnight_pending = False
            self._pending_signal = None
            self._pending_signal_bar = None
            # The memoised dashboard verdicts are keyed by bar, not by mode, so
            # they stay valid - the indicators do not care which book is trading.
            log.info(
                "Strategy state reset for %s across %d book(s): bar guard, "
                "overnight reconcile and owed entry all cleared",
                mode,
                len(self.runs),
            )

    def _open_contract_symbol(self) -> Optional[str]:
        """The contract an open position is actually held in, if any.

        This is the only contract that can be expired: the engine always trades
        the current front month, so a *new* entry can never name a dead contract.
        What can go wrong is a position carried across the 15:30 close, and that
        is what the rollover warning is about.
        """
        try:
            positions = db.get_open_positions(mode=get_execution_mode())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not read open positions for the rollover check: %s", exc)
            return None
        symbols = sorted({str(p["symbol"]) for p in positions if p.get("symbol")})
        return symbols[0] if symbols else None

    def enforce_rollover(self) -> Optional[dict[str, Any]]:
        """Warn before, then flag, a contract that has stopped trading.

        NSE Nifty futures expire on the last Tuesday of the month and stop trading
        at the 15:30 close. A position still open at that instant has no venue
        left, so it is an expiry to resolve by hand rather than something the bot
        can trade out of.

        The warning is the point of this method. The engine always trades the
        current front month, so a *new* entry can never name an expired contract -
        the calendar rolls at the close, and the symbol rolls with it. The failure
        mode is a position carried across the close, which is why the check is
        driven by the open position's symbol rather than by the calendar.

        Failures stay local to *new entries*. An open position that expired is
        real exposure and needs a human decision, so this deliberately does not
        touch it; blocking an exit would be far worse than blocking an entry.
        """
        open_symbol = self._open_contract_symbol()
        status = rollover_status(open_symbol=open_symbol)
        moment = time.time()
        with self._lock:
            self.state.rollover = status
            self.state.rollover_paused = bool(status["rolled"])
            # Warn once an hour inside the final session, so a rollover cannot be
            # missed but also cannot fill the log with the same line.
            should_warn = bool(status["expiring_today"]) and (
                self._last_rollover_warning is None
                or (moment - self._last_rollover_warning) >= 3600.0
            )
            if should_warn:
                self._last_rollover_warning = moment
        if should_warn:
            log.warning(
                "Front month %s expires today at %s IST; it rolls to %s after the close",
                status["current_symbol"],
                status["expiry"][11:16],
                status["next_symbol"],
            )
        if status["rolled"] and not self._rollover_logged:
            self._rollover_logged = True
            log.error(
                "%s expired on %s (last Tuesday) and has no venue left. New entries "
                "are blocked until it is squared off; %s is the contract the engine "
                "would trade next.",
                status["open_symbol"],
                status["open_expiry"],
                status["current_symbol"],
            )
        if not status["rolled"]:
            self._rollover_logged = False
        return status

    def exit_on_signal(
        self,
        verdict: strategy.StrategyVerdict,
        run: Optional[StrategyRun] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """Close the open position when the signal says the trade is over.

        This is the strategy's only exit. There is no stop-loss and no profit
        target, so the position lives exactly as long as the signal supports it.
        Which bar ends it is ``STRATEGY_EXIT``:

        * ``flip`` (default) - the pair turning the *other* way. A ``NO_SIGNAL``
          bar is held, because the pair disagreeing says nothing about a position
          already on; cutting there exits trades that were only pausing.
        * ``flat`` - the first disagreement, which is the rule this replaced.

        Either way the exit is a *transition*, so a run of qualifying bars closes
        the position once rather than once per bar.

        The exit is verified rather than assumed. ``square_off`` reports a
        ``closed_positions`` count, and when that is zero the position is still
        open, so the exit is retried on the next bar instead of being forgotten -
        a position the bot believes it closed but did not is the worst state this
        method can leave behind.

        Nothing is paused or latched. The strategy keeps evaluating and takes the
        next fresh agreement like any other entry.

        A confirmed ``flip`` exit **reverses on the same bar**, and has to. The
        bar that flips the pair is by definition a fresh transition into the new
        direction, and the bar after it is not - it is the same state persisting.
        So if the reversal were left to "the next fresh signal" it would never
        happen: the bot would close on the flip and then sit flat until some
        unrelated flip came along, taking one trade in two out of every trend.
        This is why the exit reports the direction it wants entered, and
        ``evaluate_now`` places that entry here rather than falling through.

        Under ``flat`` there is no reversal to make: the bar that exits carries
        ``NO_SIGNAL``, so there is no direction to enter and none is opened.
        """
        # The book this exit belongs to. Without it every strategy's exit would
        # read every strategy's position and square off the wrong one - and with
        # two books on one contract, "the wrong one" is the other book's leg,
        # which is exactly the double exposure the order manager exists to
        # prevent. The exit rule is per-run too: two books may differ.
        book = run.id if run is not None else self.strategy_id
        # The caller's resolved params win: ``evaluate_run`` overlays the live
        # config onto the active run's snapshot, so re-reading either source here
        # would disagree with the params the verdict was actually computed under.
        resolved = params if params is not None else (
            run.params if run is not None else strategy_params()
        )
        exit_rule = (resolved.get("exit") or "flip").strip().lower()
        if not strategy.exits(verdict, exit_rule):
            return None

        mode = run.mode() if run is not None else get_execution_mode()
        manager = get_order_manager()
        symbol = manager.current_symbol()
        if not symbol:
            return None

        # Only a position in the contract being traded *and in this book* is this
        # method's business. Zero means none here; another book's position is not
        # this strategy's to close.
        quantity = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(mode=mode, strategy_id=book)
            if position["symbol"] == symbol
        )
        if quantity == 0:
            return None

        # getattr because a verdict stand-in (a test double, or an older cached
        # object) may not carry the candle; the bar name is for the log, not for
        # the decision.
        candle = getattr(verdict, "signal_candle", None) or {}
        bar_at = candle.get("time") if isinstance(candle, dict) else None
        detail = (
            f"SuperTrend and PSAR exited {verdict.signal or 'NO SIGNAL'} on bar "
            f"{bar_at or '?'} (previous {verdict.previous_signal}, rule {exit_rule}); "
            f"closing {quantity:+d} in {symbol}"
        )
        log.warning("Signal exit: %s", detail)

        result: Optional[ExecutionResult] = None
        try:
            result = manager.square_off(
                reason="SIGNAL_FLIP",
                source="SIGNAL_EXIT",
                symbol=symbol,
                mode=mode,
                strategy_id=book,
            )
            log.warning(
                "Signal exit square-off: ok=%s %s", result.ok, result.reason,
            )
        except Exception:  # pragma: no cover - defensive
            log.exception("Signal exit square-off failed")

        closed = int((result.details or {}).get("closed_positions") or 0) if result else 0
        confirmed = bool(result and result.ok and closed > 0)
        if not confirmed:
            # Not reported and not latched: the position is still on the ledger,
            # so the next bar tries again. Saying "exited" here would hide an exit
            # that never happened.
            log.error(
                "Signal exit NOT confirmed for %s; leaving the position open and retrying",
                symbol,
            )
            with self._lock:
                self.state.last_error = (
                    f"signal exit unconfirmed: "
                    f"{result.reason if result else 'square-off raised'}"
                )
            return {
                "kind": "SIGNAL_FLIP",
                "detail": detail,
                "symbol": symbol,
                "confirmed": False,
                "square_off_ok": False,
                "square_off_reason": result.reason if result else "square-off raised",
                "closed_positions": closed,
                "paused": getattr(manager, "paused", False),
            }

        _trade_alert(
            "TRADE_CLOSED",
            f"{symbol} closed on an opposite signal: {detail}",
            severity="warning",
            symbol=symbol,
            fields={
                "strategy": book,
                "venue": mode,
                "symbol": symbol,
                "action": "EXIT",
                "quantity": quantity,
                "bar": bar_at,
                "reason": "opposite signal",
            },
        )

        with self._lock:
            self._breach = risk.Breach(
                kind="SIGNAL_FLIP",
                symbol=symbol,
                points=None,
                position_id=None,
                detail=detail,
            )
            # Forget any entry that was still owed: the latch belongs to the
            # trade that just ended, and carrying it forward would re-enter on the
            # very signal that produced the exit.
            self._set_pending_signal(None, None, run)

        payload = {
            "kind": "SIGNAL_FLIP",
            "detail": detail,
            "symbol": symbol,
            "confirmed": True,
            "square_off_ok": True,
            "square_off_reason": result.reason if result else "",
            "closed_positions": closed,
            "paused": getattr(manager, "paused", False),
            # The direction this exit wants entered on the same bar: the new one
            # under ``flip``, none under ``flat`` (there is no new direction to
            # take on a bar whose verdict is NO_SIGNAL). ``evaluate_now`` reads
            # this to place the reversal; see the docstring for why it cannot be
            # deferred to the next bar.
            "reverse_to": (
                verdict.signal if exit_rule == strategy.EXIT_FLIP else None
            ),
            "signal_candle": candle,
        }
        return payload

    def _bar_before_first_session_close(
        self, bar_at: Optional[str], seconds: int, market_open: bool
    ) -> bool:
        """True while the closed bar predates the session's first in-session close.

        The reconcile is specified against the *09:00* candle, and with 15-minute
        bars that candle closes at exactly 09:15 - the moment the exchange opens.
        So the first evaluation of a session still only has the 08:45 bar, and a
        decision taken then would be the overnight state rather than the morning
        one. Waiting for the first closed bar at or after 09:15 means the 09:00
        candle is genuinely in hand before anything is decided.

        Derived from the bar timestamps rather than from a wall clock, so it needs
        no extra resolution: `evaluate_now` already knows when the bar closed.
        """
        if not market_open:
            # Out of hours there is nothing to reconcile yet, and the bar sequence
            # that matters is the one that resumes at the open.
            return True
        closed_at = _bar_closed_at(bar_at, seconds)
        if closed_at is None:
            return False
        session_open = closed_at.replace(
            hour=MARKET_OPEN[0], minute=MARKET_OPEN[1], second=0, microsecond=0
        )
        return closed_at < session_open

    def _reconcile_overnight_position(
        self, signal: str, fresh: bool = False, run: Optional[StrategyRun] = None
    ) -> Optional[dict[str, Any]]:
        """Decide an overnight position's fate on the morning's first bar.

        The ordinary decision already asserts the position to the verdict, so the
        only thing this adds is *when* that happens for a leg carried over the
        close: not on any bar the engine happens to evaluate overnight, but on the
        first bar that closes in-session. An overnight position is real risk, and
        the morning's own bar is the state it should be judged against.

        The rule is two-way, on the position the bot actually holds:

        - **Against** the position: close it. The old leg is wrong, so it goes.
        - **With** the position: leave it. It is already where the indicators
          point, and re-entering would pay a spread to duplicate a position.

        Closing is unconditional, but *opening the opposite side* is not: it needs
        a fresh signal, exactly like any other entry. A leg that is merely still
        against an existing bearish state is closed and then left flat until the
        pair forms again - otherwise the morning would reverse into a trade on the
        very signal that has been in force all night, which is the re-entry the
        transition rule exists to prevent.

        A ``NO_SIGNAL`` bar is not a third case: it says nothing about the
        position, so the leg is carried and left to the opposite-signal exit.
        """
        manager = get_order_manager()
        mode = run.mode() if run is not None else get_execution_mode()
        book = run.id if run is not None else self.strategy_id
        symbol = manager.current_symbol()
        if not symbol:
            return None
        quantity = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(mode=mode, strategy_id=book)
            if position["symbol"] == symbol
        )
        # Only a position in the contract being traded is this method's business.
        # Zero means none here, and another contract is the rollover's concern.
        if quantity == 0:
            return None

        if signal not in {strategy.BULL, strategy.BEAR}:
            return self._note_overnight_hold(
                symbol, quantity, "the bar produced no directional signal"
            )

        target = 1 if signal == strategy.BULL else -1
        if quantity * target > 0:
            return self._note_overnight_hold(
                symbol, quantity, f"the position already agrees with {signal}"
            )

        action = BUY if target > 0 else SELL
        close_side = "long" if quantity > 0 else "short"
        # Whether the opposite side may be opened. A leg that is against an
        # *existing* state is still closed - it is wrong, and carrying it would be
        # the bigger risk - but the reversal needs a fresh signal, so the bot goes
        # flat and waits rather than trading the overnight state.
        may_open = bool(fresh)
        detail = (
            f"{close_side} {abs(quantity)} in {symbol} against {signal} on the first "
            "bar of the session; "
            + ("flipping the overnight position." if may_open
               else "closing it and waiting for a fresh signal.")
        )
        log.warning("%s: %s", action, detail)
        result = manager.square_off(
            "SIGNAL_FLIP_AT_OPEN", source="OPEN_RECONCILE", symbol=symbol, mode=mode,
            strategy_id=book,
        )
        if not result.ok:
            # Not entered blindly. If the old leg did not close, opening the other
            # side would leave both - the double exposure the order manager
            # refuses everywhere else.
            log.error(
                "Open reconcile could not close %s (%s); not opening the opposite leg",
                symbol, result.reason,
            )
            return {
                "action": action,
                "detail": detail,
                "result": result,
                "opened": False,
                "reason": f"close failed: {result.reason}",
            }

        if not may_open:
            # Closed, and deliberately left flat. Recorded so the dashboard can
            # tell this from a reconcile that never ran.
            db.record_signal(
                "FLAT", f"OPEN {detail}", True, "closed at the open; no fresh signal"
            )
            with self._lock:
                self.state.orders_sent += 1
                self.state.last_action = "FLAT at open (reconcile)"
                self.state.last_action_at = db.iso()
                self.state.last_result = {
                    "ok": True, "action": "FLAT", "reason": detail, "reconcile": True,
                }
            return {
                "action": "FLAT",
                "detail": detail,
                "result": result,
                "opened": False,
                "closed": True,
            }

        # Recorded here as well as in the executor. This runs *before* the normal
        # transition, so that could otherwise skip a flip it would have placed,
        # leaving the reconcile invisible in the signal log.
        db.record_signal(action, f"OPEN {detail}", True, "reconciled at the open")
        entry = manager.process_signal(
            {
                "action": action,
                "symbol": symbol,
                "qty_lots": settings.default_lots,
                "strategy": book,
                "gift_detail": detail,
            },
            source="OPEN_RECONCILE",
            strategy_id=book,
        )
        with self._lock:
            if entry.ok:
                self.state.orders_sent += 1
                self.state.last_action = f"{action} at open (reconcile)"
            else:
                self.state.orders_blocked += 1
                self.state.last_action = f"{action} at open blocked"
            self.state.last_action_at = db.iso()
            self.state.last_result = {
                "ok": entry.ok, "action": action, "reason": detail,
                "reconcile": True,
            }
        return {
            "action": action,
            "detail": detail,
            "result": entry,
            "opened": bool(entry.ok),
        }

    def _note_overnight_hold(self, symbol: str, quantity: int, why: str) -> dict[str, Any]:
        """Record that an overnight position was deliberately left alone.

        Returned rather than logged and forgotten so the dashboard can say *why*
        the bot did nothing on the open - "no signal change" is indistinguishable
        from a reconcile that failed to run.
        """
        side = "long" if quantity > 0 else "short"
        detail = f"{side} {abs(quantity)} in {symbol} held: {why}."
        log.info("Overnight position held: %s", detail)
        db.record_signal("HOLD", f"OPEN {detail}", True, "held at the open")
        with self._lock:
            self.state.last_action = "HOLD at open (reconcile)"
            self.state.last_action_at = db.iso()
            self.state.last_result = {
                "ok": True, "action": "HOLD", "reason": detail, "reconcile": True,
            }
        return {"action": "HOLD", "detail": detail, "opened": False, "held": True}

    # -- evaluation ------------------------------------------------------
    def evaluate_now(self, force: bool = False) -> dict[str, Any]:
        """Evaluate the active strategy's series once.

        Kept as the single-run entry point - the API's on-demand check and every
        existing caller use it - and delegates to :meth:`evaluate_run` so there
        is one implementation rather than two that can drift.
        """
        return self.evaluate_run(self.run, force=force)

    def evaluate_run(self, run: StrategyRun, force: bool = False) -> dict[str, Any]:
        """Evaluate one strategy's series and act on the verdict.

        ``force`` re-evaluates the newest bar even if it was already seen
        (used by the API for an on-demand check).

        Everything below reads through ``run`` rather than the engine's own
        attributes: the source, the params, the last bar seen and the owed-entry
        latch all belong to the strategy, not to the process.
        """
        source = run.source
        if run is self.run and isinstance(source, GiftBarSource) and source.feed is not get_feed():
            # The process feed was replaced (a reconnect, or a test stub). The
            # run must follow it rather than read a series that has stopped.
            #
            # The interval is carried across. Rebuilding without it silently
            # reset the book to the feed's own timeframe: a 15-minute GIFT book
            # became 60-minute on the first reconnect, so it reported - and
            # evaluated - the wrong series for the rest of the process's life.
            source = GiftBarSource(get_feed(), interval=source.interval)
            run.source = source
        # Kite history is authoritative; refresh it so the indicators run on
        # exchange OHLC rather than tick-built approximations.
        try:
            source.refresh_history()
        except Exception as exc:  # pragma: no cover - network
            log.warning("History refresh failed for %s: %s", run.id, exc)
        candles = source.candles()
        # The active run reads the parameters afresh, so a change to the config
        # takes effect without rebuilding the engine. Other runs keep their own
        # snapshot, because each strategy has its own settings.
        params = run.params
        if run is self.run:
            fresh = strategy_params()
            # Only overlay the keys the live config actually answers for; a run
            # built from STRATEGY_<n>_* may carry parameters the flat namespace
            # knows nothing about.
            params = {**params, **fresh} if fresh else dict(params)
        needed = required_bars()
        # The engine-level warm-up counters describe the *active* run, because
        # that is the one the hero panel shows. Writing them from every run let
        # the last strategy to evaluate win: with a GIFT book on 63 bars and a
        # NIFTY book on 310, the hero reported 310 bars next to a GIFT signal.
        if run is self.run:
            with self._lock:
                self.state.warmup_needed = needed
                self.state.warmup_have = len(candles)

        if len(candles) < needed:
            with self._lock:
                if run is self.run:
                    self.state.ready = False
                    self.state.last_error = None
                    self.state.skipped += 1
                run.state.ready = False
                run.state.bars = len(candles)
                run.state.warmup_needed = needed
                run.state.warmup_have = len(candles)
                run.state.skipped += 1
            return {
                "ok": False,
                "reason": f"warming up: {len(candles)}/{needed} {run.symbol} bars",
                "ready": False,
                "have": len(candles),
                "needed": needed,
            }

        newest = candles[-1]["time"]
        # The engine's own field stays authoritative for the active run, so a
        # caller that seeds ``_last_bar_at`` (as the tests and the API do) is
        # still honoured; other runs track their own.
        last_seen = self._last_bar_at if run is self.run else run.state.last_bar_at
        if not force and newest == last_seen:
            return {"ok": True, "reason": "no new closed bar", "skipped": True}
        run.state.last_bar_at = newest
        if run is self.run:
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
        # The lag between a bar closing and being evaluated. This is the number
        # that was silently growing while the 60-minute NIFTY book sat on a stale
        # bar, and it was impossible to see: the log named the bar but not when
        # that bar closed, so a one-hour-old bar looked perfectly normal.
        closed_at = _bar_closed_at(newest, source.seconds)
        lag_seconds = None
        if closed_at is not None:
            closed_aware = (
                closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=db.IST)
            )
            lag_seconds = round(
                (datetime.now(db.IST) - closed_aware).total_seconds(), 1
            )
        log.info(
            "Indicator values strategy=%s source=%s interval_seconds=%s candle_at=%s "
            "bar_closed_at=%s lag_seconds=%s history_bars=%d ohlc=%s indicators=%s "
            "signal=%s previous=%s fresh=%s",
            run.id,
            run.symbol,
            source.seconds,
            newest,
            closed_at.isoformat(timespec="seconds") if closed_at else None,
            lag_seconds,
            verdict.bars,
            json.dumps(ohlc, sort_keys=True),
            json.dumps(verdict.values, sort_keys=True),
            verdict.signal,
            verdict.previous_signal,
            verdict.fresh,
        )

        evaluated_at = db.iso()
        with self._lock:
            self.state.ready = True
            self.state.last_error = None
            self.state.last_bar_at = newest
            self.state.last_eval_at = evaluated_at
            self.state.last_signal = verdict.signal
            self.state.last_detail = verdict.detail
            self.state.last_conditions = verdict.conditions
            self.state.last_values = verdict.values
            self.state.evaluations += 1
            # The run's own counters. The engine's view above is what the
            # single-strategy dashboard reads and must stay; the run's is what
            # /api/strategies reads for each book, and without this it reports
            # zeros for every strategy that is not the active one - which is how
            # a working second strategy looks broken.
            run.state.ready = True
            run.state.last_error = None
            run.state.last_bar_at = newest
            run.state.last_eval_at = evaluated_at
            run.state.bar_lag_seconds = lag_seconds
            run.state.last_signal = verdict.signal
            run.state.last_detail = verdict.detail
            run.state.last_conditions = verdict.conditions
            run.state.last_values = verdict.values
            run.state.bars = len(candles)
            run.state.evaluations += 1
            run.last_verdict = verdict

        signal = verdict.signal
        self._last_signal = signal
        market_open = market_status()["is_open"]

        def decision(action: Optional[str], reason: str, **extra: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "signal": signal,
                "detail": verdict.detail,
                "action": action,
                "reason": reason,
                **extra,
            }

        # A verdict decided while the NSE was shut is dropped by the executor, and
        # the whole decision is held until the first bar that closes in-session.
        # That keeps an overnight position from being acted on against the
        # overnight state: with the default 15-minute bars, the first in-session
        # close is the 09:00 candle closing at exactly 09:15, so the decision is
        # the morning's own.
        before_first_session_close = self._bar_before_first_session_close(
            newest, source.seconds, market_open
        )

        # The owed-entry latch is recorded *before* the session gate, because the
        # gate is exactly what defers the entry. A transition that closes
        # out-of-session is the case the latch exists for: the order is dropped,
        # so the intent has to be remembered or the signal is lost.
        self._load_pending_signal(run)
        if verdict.fresh:
            self._set_pending_signal(signal, newest, run)
            log.info(
                "Fresh %s signal on bar %s (previous %s); entry owed",
                signal, newest, verdict.previous_signal,
            )
        elif signal == strategy.NO_SIGNAL:
            # The pair stopped agreeing, so the entry it was owed for is no longer
            # the state of the market. The latch carries a signal across a
            # *session* boundary, not across the signal dying.
            self._set_pending_signal(None, None, run)

        # The GIFT Nifty index book tracks its own state, one action per signal,
        # and is settled here rather than beside the futures order. It has no NSE
        # session - it prices the index, which trades ~20 hours - so gating it on
        # the 09:15 open would make the two venues disagree on *when* they acted,
        # which is the one thing that book exists not to do.
        #
        # It follows the same *transition* rule as the futures book, for the same
        # reason: a bar that merely still agrees is not a new signal, so it cannot
        # reopen a trade the index book just closed. ``execute`` is idempotent when
        # the book already holds the position, so a repeated action is harmless -
        # but a repeated action is exactly what must not be sent, because it would
        # re-enter after an exit.
        #
        # The bar that exits under the configured rule settles the book too:
        # ``FLAT`` closes whatever the index book holds, on the same bar and for
        # the same reason the futures book closes. A ``NO_SIGNAL`` bar under the
        # flip rule settles nothing - the book keeps its position through it,
        # exactly as the futures book does.
        # On a ``flip`` exit the pair has just turned the other way, so that bar
        # is a fresh transition and the book reverses into it rather than going
        # flat. Under ``flat`` the exiting bar carries NO_SIGNAL and there is no
        # new direction, so it closes. Order matters: the reversal is checked
        # first because it is both a fresh signal and an exit.
        exit_rule = (params.get("exit") or "flip").strip().lower()
        if strategy.exits(verdict, exit_rule):
            gift_action: Optional[str] = (
                BUY if signal == strategy.BULL else
                SELL if signal == strategy.BEAR else
                FLAT
            )
        elif verdict.fresh:
            gift_action = BUY if signal == strategy.BULL else SELL
        else:
            gift_action = None

        # The exit runs before any entry decision, and on the same bar the pair
        # flipped. It is not gated on the session: a position that is wrong must
        # be closable whenever the signal says so, and blocking an exit would be
        # far worse than blocking an entry.
        exit_report = self.exit_on_signal(verdict, run, params)
        # The index leg is settled from the *exit rule*, not from whether the
        # futures book happened to hold something. ``exit_on_signal`` returns
        # None when the futures leg is empty, and the settlement used to live
        # only inside the branch below - so a book whose futures leg never filled
        # (an out-of-session entry, a rejected order, a restart between the two
        # legs) kept its index position forever. Observed live: gift-15m held
        # LONG 65 on the index with no futures leg and the flat exit did nothing.
        #
        # Settling here is safe when the futures leg *is* open: the branch below
        # settles again with the same action, and ``GiftBook.execute`` is a no-op
        # when the book already holds the position it is asked for.
        if exit_report is None and strategy.exits(verdict, exit_rule):
            gift_result = self._settle_gift_book(gift_action, verdict, newest, run)
            if gift_result is not None:
                return decision(
                    FLAT,
                    f"{verdict.detail}; index leg settled, no futures position",
                    gift_book=gift_result,
                )
        if exit_report is not None:
            confirmed = exit_report.get("confirmed", True)
            # An unconfirmed exit leaves the old position on the ledger while the
            # verdict now points the other way. Falling through to the entry would
            # *reverse* into the new direction on the bar of an exit that never
            # happened - the retry is the next bar's business.
            if not confirmed:
                return decision(
                    None,
                    "exit signal, but the exit is unconfirmed; retrying next bar",
                    result=exit_report,
                    gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
                )

            reverse_to = exit_report.get("reverse_to")
            gift_result = self._settle_gift_book(gift_action, verdict, newest, run)
            if reverse_to is None:
                # ``flat``: the exiting bar is NO_SIGNAL, so there is no new
                # direction and the bot goes flat. This is the desired end state.
                return decision(FLAT, exit_report["detail"], result=exit_report,
                                gift_book=gift_result)

            # The reversal has to happen on this bar: it is the only fresh
            # transition into the new direction, and the next bar is the same
            # state persisting. Deferring it would leave the bot flat for the
            # whole of the new trend.
            entry_action = BUY if reverse_to == strategy.BULL else SELL
            direction = 1 if reverse_to == strategy.BULL else -1
            closed_at = datetime.fromisoformat(newest) + timedelta(seconds=source.seconds)

            if before_first_session_close or not market_open:
                # The futures leg cannot fill out of session. The exit above is
                # not gated on the session (a wrong position must always be
                # closable), but the reversal is an *entry*, so it is latched and
                # honoured on the first bar that can fill it - exactly like any
                # other out-of-session transition.
                self._set_pending_signal(reverse_to, newest, run)
                log.info(
                    "Flip exit closed the position; %s reversal owed until the next "
                    "in-session bar", reverse_to,
                )
                return decision(
                    FLAT,
                    f"{exit_report['detail']}; {entry_action} owed until NSE opens",
                    result=exit_report,
                    gift_book=gift_result,
                )

            with self._lock:
                self.state.signals_fired += 1
            entry = self.execute(
                entry_action,
                f"reversal after flip on bar {newest}",
                paper_reference_price=verdict.close,
                signal_timestamp=closed_at,
                max_age_seconds=self._signal_max_age(run),
                signal_candle=getattr(verdict, "signal_candle", None),
                run=run,
            )
            if isinstance(entry, dict) and entry.get("ok"):
                self._set_pending_signal(None, None, run)
            elif isinstance(entry, dict) and entry.get("deferred"):
                # The order did not reach the venue, so the reversal is still owed.
                self._set_pending_signal(reverse_to, newest, run)
            return decision(
                entry_action,
                f"{exit_report['detail']}; reversed into {entry_action} on the same bar",
                result={"exit": exit_report, "entry": entry},
                gift_book=gift_result,
            )

        # The futures decision is what waits for the first in-session bar: an
        # order placed outside the session is dropped by the executor, and an
        # overnight leg should be judged on the morning's own bar rather than on
        # the overnight state. With the default 15-minute bars that first close is
        # the 09:00 candle closing at exactly 09:15.
        if before_first_session_close:
            self._set_overnight_pending(True, run)
            return decision(
                None,
                "held until the first bar that closes in-session",
                gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
            )

        # An overnight position is reconciled before the ordinary decision, even
        # when the verdict is unchanged. It is the only path that acts on a
        # held-over leg while there is no fresh transition to key off.
        if (run.state.overnight_pending if run is not None else self._overnight_pending):
            self._set_overnight_pending(False, run)
            reconciled = self._reconcile_overnight_position(signal, verdict.fresh, run)
            if reconciled is not None:
                return decision(
                    reconciled["action"],
                    reconciled["detail"],
                    result=reconciled,
                    gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
                )

        if signal == strategy.NO_SIGNAL:
            # The pair disagrees, which is not an exit: an open position is held
            # through it until the indicators turn the other way. Nothing to
            # enter, and nothing to close.
            return decision(
                None,
                "no signal",
                gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
            )

        action = BUY if signal == strategy.BULL else SELL
        direction = 1 if signal == strategy.BULL else -1

        # Entry is gated on a *transition*, not on the state. A bar that merely
        # still agrees with the previous one is not a new signal, so it cannot
        # open a trade - which is what makes an exit final: the signal that was in
        # force when the position closed is never re-used, and the next trade
        # needs the conditions to form again from scratch.
        #
        # The transition itself was latched above, before the session gate, so a
        # signal that closed out-of-session is still owed here.
        #
        # Read from the *book*, not from `self._pending_signal`. The engine-level
        # mirror is only kept in step for `self.run` (the active run); for every
        # other book it stays None. Subscribing to the mirror here meant the gate
        # always answered "not a new signal" for every non-active strategy, so a
        # second book was configured, loaded bars, reported its signal on the
        # dashboard - and could never place an order. The latch was being written
        # correctly to the run all along; only this read was wrong.
        owed = run.pending_signal
        if owed is None:
            return decision(
                None,
                f"{signal} is the existing state, not a new signal; waiting for a transition",
                gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
            )

        # The owed direction and this bar's verdict must agree. They can differ
        # when the pair flipped again before the entry could be filled: the newer
        # transition has already replaced the latch, so this is only reachable if
        # the latch was written by an older bar.
        if owed != signal:
            return decision(
                None,
                f"owed {owed} entry superseded by {signal}; waiting for a transition",
                gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
            )

        # No separate "restart only primes" guard is needed any more. The
        # transition gate already covers it and is strictly stronger: a bot that
        # boots mid-run sees ``previous_signal == signal``, so the bar is not
        # fresh and nothing is opened on a state it merely inherited. A transition
        # that happened while the process was down cannot fire either, because
        # only the newest bar's own transition counts - a days-old flip has long
        # since become the previous bar's state.
        if not self._position_needs_action(direction, run):
            # Already positioned the way the owed entry wanted. The latch has
            # done its job, so it is cleared rather than left to fire later.
            self._set_pending_signal(None, None, run)
            return decision(
                None,
                "position already matches the verdict",
                gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
            )

        # Portfolio rules, checked last so a refusal is only ever about exposure
        # rather than about a signal that was never going to trade anyway. These
        # are the rules no single strategy can enforce: two books trading one
        # contract net to a single broker position, so an opposing entry would
        # cancel the other book's leg instead of being a separate trade.
        try:
            manager = get_order_manager()
            allowed = get_aggregator().authorise(
                run.id,
                action,
                manager.current_symbol() or "",
                int(getattr(settings, "default_lots", 1)),
                lot_size=int(getattr(settings, "lot_size", 65)),
                mode=run.mode(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Portfolio check failed (%s); refusing the entry", exc)
            allowed = None
        if allowed is not None and not allowed.allowed:
            with self._lock:
                self.state.orders_blocked += 1
                self.state.last_action = f"{action} blocked (portfolio)"
                self.state.last_action_at = db.iso()
                self.state.last_result = {
                    "ok": False, "action": action, "reason": allowed.reason,
                    "portfolio": allowed.to_dict(),
                }
            db.record_signal(
                action, f"GIFT {verdict.detail}".strip(), False, allowed.reason,
                strategy_id=run.id,
            )
            log.warning("Portfolio refused %s: %s", action, allowed.reason)
            return decision(
                None,
                allowed.reason,
                portfolio=allowed.to_dict(),
                gift_book=self._settle_gift_book(gift_action, verdict, newest, run),
            )

        with self._lock:
            self.state.signals_fired += 1
        closed_at = datetime.fromisoformat(newest) + timedelta(seconds=source.seconds)
        # The index book settles on the *same* bar with the *same* verdict, so the
        # two books differ only in venue and hours - never in when they acted. It
        # can never go live (GIFT Nifty is a non-tradeable index), and it is
        # settled first so a futures rejection cannot starve it of signals.
        gift_result = self._settle_gift_book(gift_action, verdict, newest, run)
        result = self.execute(
            action,
            verdict.detail,
            paper_reference_price=verdict.close,
            signal_timestamp=closed_at,
            max_age_seconds=self._signal_max_age(run),
            # The bar the signal came from, and the ATR it is measured against.
            # Both describe the completed candle, so the large-candle stop is
            # decided on the same bar that produced the signal. getattr because a
            # verdict stand-in (a test double, or an older cached object) may not
            # carry them, and a missing candle must fall back to the existing
            # stop rule rather than break the entry.
            signal_candle=getattr(verdict, "signal_candle", None),
            run=run,
        )
        self._report_partial_legs(action, result, gift_result, run, newest)
        # The latch is cleared only once the entry is actually on the ledger. A
        # rejected or unfilled order leaves it owed, so the next bar tries again
        # instead of the signal being silently consumed by a failed attempt.
        if isinstance(result, dict) and result.get("ok"):
            self._set_pending_signal(None, None, run)
        return {"ok": True, "signal": signal, "detail": verdict.detail, "action": action,
                "result": result, "gift_book": gift_result}

    def _report_partial_legs(
        self,
        action: str,
        futures_result: Optional[dict[str, Any]],
        gift_result: Optional[dict[str, Any]],
        run: Optional[StrategyRun],
        bar_at: str,
    ) -> Optional[dict[str, Any]]:
        """Say out loud when a two-leg strategy filled only one leg.

        A strategy carrying one signal on two venues has two independent fills,
        and either can fail on its own: the index book is priced off a quote and
        the futures order goes to a broker. Treating "the strategy acted" as one
        outcome is what makes a half-filled strategy invisible - the tile shows a
        position and the operator never learns the other leg was rejected.

        Nothing is retried here. A rejected futures order is re-attempted by the
        normal path (the owed-entry latch is deliberately left set when the
        futures leg fails), and the index book is settled per-bar by its own
        transition rule. What this adds is the *report*, so a partial fill is a
        recorded fact rather than an inference from two unrelated logs.
        """
        if run is None or len(run.legs) < 2:
            return None
        filled = []
        failed = []
        if "futures" in run.legs:
            ok = bool(isinstance(futures_result, dict) and futures_result.get("ok"))
            # A skipped futures leg is not a failure: the book was told not to
            # trade it, so there was no order to place.
            skipped = bool(isinstance(futures_result, dict) and futures_result.get("skipped"))
            (filled if ok or skipped else failed).append("futures")
        if "gift" in run.legs:
            (filled if gift_result else failed).append("gift")
        if not failed or not filled:
            # All legs acted, or none did - both are ordinary and already logged
            # by the leg that did or did not run.
            return None
        reason = (
            f"Partial execution for {run.id} on bar {bar_at}: {action} filled on "
            f"{', '.join(filled)} but not on {', '.join(failed)}. The legs are "
            f"independent; the missing one retries on its own next bar."
        )
        log.error("%s", reason)
        with self._lock:
            run.state.last_error = reason
        try:
            db.record_signal(
                action, f"PARTIAL {reason}", False, reason, strategy_id=run.id,
            )
        except Exception:  # pragma: no cover - defensive
            log.warning("Could not record the partial-leg signal for %s", run.id)
        return {"filled": filled, "failed": failed, "reason": reason}


    def _position_needs_action(
        self, direction: int, run: Optional[StrategyRun] = None
    ) -> bool:
        """True when no open position agrees with ``direction``.

        "Agrees" is the same test the reconcile uses: an open quantity whose sign
        matches the verdict's. An opposing or absent position both need the
        entry, and the entry itself closes an opposing leg first, so a flip needs
        no separate path here.
        """
        if direction == 0:
            return False
        symbol = get_order_manager().current_symbol()
        book = run.id if run is not None else self.strategy_id
        quantity = sum(
            int(position["quantity"]) * int(position["position_key"])
            for position in db.get_open_positions(
                mode=run.mode() if run is not None else get_execution_mode(),
                strategy_id=book,
            )
            if position["symbol"] == symbol
        )
        return quantity * direction <= 0

    def _settle_gift_book(
        self,
        action: Optional[str],
        verdict: strategy.StrategyVerdict,
        bar_at: str,
        run: Optional[StrategyRun] = None,
    ) -> Optional[dict[str, Any]]:
        """Route a signal into the GIFT Nifty index book, if it is switched on.

        The bar's own close is the reference price, so the index book needs no
        quote and cannot be blocked by a missing execution price. Bounded and
        non-fatal: an index-book problem must not stop the futures order.

        ``action`` is ``BUY``/``SELL`` for a bar that *became* directional and
        ``FLAT`` for the bar the pair stopped agreeing - the same transition rules
        the futures book follows, which is what keeps the two books from drifting
        apart in when they act. ``None`` means the bar changed nothing, so the
        book is left exactly as it is.
        """
        if action is None:
            return None
        # The index book is a single paper book priced on the GIFT Nifty level,
        # so only the GIFT-signalled strategy may settle it. Without this guard
        # every strategy settles the same book: a NIFTY-signalled run would
        # drive a GIFT-priced ledger, and two runs would each read the other's
        # position as their own.
        if run is not None and not isinstance(run.source, GiftBarSource):
            return None
        # And only if this strategy asked for the index leg at all. A book
        # configured ``LEGS=futures`` trades the future alone; settling a GIFT
        # ledger for it would report a position the operator never configured.
        if run is not None and not run.trades_gift:
            return None
        try:
            from gift_book import get_gift_book

            # This strategy's own book. A shared one let the 15-minute book read
            # the 60-minute book's position as its own, close it, and report P&L
            # that belonged to neither.
            book = get_gift_book(run.id if run is not None else None)
            if not book.enabled:
                return None
            # The strategy id the leg is booked under comes from the book itself
            # (``get_gift_book(run.id)``), so a leg can be traced back to the
            # strategy that opened it rather than to the process.
            return book.execute(
                action,
                verdict.close,
                bar_at=bar_at,
                detail=f"{verdict.detail} | bar {bar_at}",
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("GIFT index book settlement failed")
            with self._lock:
                self.state.last_error = f"gift index book: {exc}"
            return None

    def _signal_max_age(self, run: Optional[StrategyRun] = None) -> int:
        """How stale a signal may be before the broker refuses it.

        A signal is stamped with the close of the bar that produced it, so it
        arrives already one bar old. The limit therefore has to exceed the bar
        length, not sit under it: at 15-minute bars a fixed 300s rejects the
        engine's own fresh signals, and the harder it is to satisfy the longer
        the bar. One and a half bars covers the engine poll and execution while
        still refusing anything that outlived a full bar plus half.

        Measured from the book's own bar length, not the GIFT interval setting:
        with books on different timeframes, one global figure would expire the
        60-minute book's signals three quarters of an hour before they are stale,
        or let the 15-minute book's through long after its bar has gone.
        """
        try:
            seconds = int(run.seconds) if run is not None else settings.gift_candle_interval_seconds
        except Exception:  # pragma: no cover - defensive
            seconds = 900
        return max(300, int(seconds * 1.5))

    # -- execution -------------------------------------------------------
    def execute(
        self,
        action: str,
        detail: str = "",
        *,
        paper_reference_price: Optional[float] = None,
        signal_timestamp: Optional[datetime] = None,
        max_age_seconds: Optional[int] = None,
        signal_candle: Optional[dict[str, Any]] = None,
        run: Optional[StrategyRun] = None,
    ) -> dict[str, Any]:
        """Place a signal, using a GIFT-close fill only for after-hours PAPER.

        ``signal_candle`` is the completed bar the signal came from. It is
        forwarded for the audit trail, so the order can be traced back to the bar
        that produced it.

        ``run`` names the book the order belongs to. It is what makes two
        strategies two books rather than two views of one: without it every order
        books to the engine's default id, so the per-strategy accounts never move
        and the no-opposing-legs rule can never see a conflict.
        """
        book = run.id if run is not None else self.strategy_id
        manager = get_order_manager()
        # A book configured with only the paper index leg must not place a
        # futures order. Without this, ``LEGS=gift`` still traded the future: the
        # operator's leg list said one instrument and the bot traded another.
        # Exits are deliberately not gated this way - a position that is already
        # on the futures ledger has to be closable whatever the config now says.
        if run is not None and not run.trades_futures:
            reason = (
                f"{action} not placed: {book} executes on "
                f"{'/'.join(run.legs)} only, with no futures leg"
            )
            log.info("%s", reason)
            db.record_signal(action, detail or reason, True, reason, strategy_id=book)
            with self._lock:
                run.state.last_action = f"{action} (no futures leg)"
                run.state.last_action_at = db.iso()
                run.state.last_result = {
                    "ok": True, "action": action, "reason": reason, "skipped": True,
                }
            return {"ok": True, "action": action, "reason": reason, "skipped": True}
        # This book's mode, not the bot's. A run that did not opt into live keeps
        # its paper ledger even when the bot is armed, which is what lets the GIFT
        # book carry on unchanged beside a live NIFTY book.
        mode = run.mode() if run is not None else get_execution_mode()
        symbol = manager.current_symbol()
        market = market_status()
        paper_after_hours = mode == PAPER and not market["is_open"]

        # A contract that has stopped trading cannot fill anything. Refusing here
        # covers both modes, so a stale-symbol PAPER entry cannot silently book a
        # position in a contract that no longer exists either.
        #
        # This is a backstop, not the rollover mechanism. The engine rolls five
        # days before expiry, so `symbol` is normally the *next* month's contract
        # and its expiry is weeks away. It fires only if the symbol and the
        # calendar disagree - a stale LIVE contract from Kite's instrument dump,
        # or a clock that has moved past the close without the symbol following -
        # which is exactly when an order would otherwise be sent to a dead
        # contract.
        expiry = contract_expiry(symbol)
        if expiry is not None and _expiry_has_passed(expiry):
            self.enforce_rollover()
            reason = (
                f"{action} signal refused: {symbol} expired on {expiry.isoformat()} "
                "(last Tuesday) and has no venue left. The engine trades "
                f"{future_symbol()}; an open position in the expired contract is an "
                "expiry to resolve by hand."
            )
            with self._lock:
                self.state.orders_blocked += 1
                self.state.last_action = f"{action} blocked (expired contract)"
                self.state.last_action_at = db.iso()
                self.state.last_result = {"ok": False, "action": action, "reason": reason}
            db.record_signal(action, f"GIFT {verdict.detail}".strip(), False, reason)
            log.error("%s", reason)
            return {"ok": False, "reason": reason, "expired": True}

        # A leg left in a contract that has stopped trading blocks new entries
        # until it is squared off. The order manager enforces this too; checking
        # here means the dashboard reports the block as a rollover state rather
        # than as an unexplained rejection, and the signal is recorded once.
        rollover = self.enforce_rollover()
        if rollover.get("entries_blocked"):
            reason = (
                f"{action} signal refused: {rollover['open_symbol']} stopped trading on "
                f"{rollover['open_expiry']} and is an expiry to resolve by hand. "
                f"Square it off (PANIC) before opening {symbol}."
            )
            with self._lock:
                self.state.orders_blocked += 1
                self.state.last_action = f"{action} blocked (expiry unresolved)"
                self.state.last_action_at = db.iso()
                self.state.last_result = {"ok": False, "action": action, "reason": reason}
            db.record_signal(action, f"GIFT {verdict.detail}".strip(), False, reason)
            log.error("%s", reason)
            return {"ok": False, "reason": reason, "expired": True}

        if not market["is_open"] and not paper_after_hours:
            with self._lock:
                self.state.deferred += 1
                self.state.last_action = f"{action} deferred"
                self.state.last_action_at = db.iso()
            reason = (
                f"{action} signal from GIFT Nifty held: NSE F&O closed "
                f"({market['reason']}). Not auto-firing later."
            )
            db.record_signal(action, f"GIFT {verdict.detail}".strip(), False, reason)
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
            db.record_signal(action, f"GIFT {verdict.detail}".strip(), False, reason)
            log.error("%s", reason)
            return {"ok": False, "reason": reason}

        # This book's pause, not just the global one. A book paused from its own
        # tile must stop on its own signal - waiting for the order manager to
        # reject it would still write a "signal fired" record and count the
        # block against every book's counter.
        book_paused = getattr(manager, "book_paused", None)
        if action in (BUY, SELL) and book_paused is not None and book_paused(book):
            with self._lock:
                self.state.orders_blocked += 1
            detail_reason = getattr(manager, "book_paused_reason", lambda _b: "")
            reason = (
                f"{action} blocked: {book} paused "
                f"({detail_reason(book) or 'PAUSED'})"
            )
            db.record_signal(action, f"GIFT {verdict.detail}".strip(), False, reason)
            log.warning("%s", reason)
            return {"ok": False, "reason": reason}

        # The fill is the *future's* price, always - never the signal source's.
        # GIFT Nifty is an index that cannot be traded, so booking an entry at its
        # level writes the index/future basis straight into the position: the row
        # opens already showing a P&L that no trade produced, and the paper book
        # disagrees with the very LTP the dashboard shows for the same contract.
        # Filled and marked from one instrument, the row only ever reports what
        # the position itself did.
        #
        # `paper_reference_price` is still required for an after-hours fill, as
        # evidence that a completed candle exists to act on, but it is no longer
        # the price at which anything is booked: the order manager resolves the
        # fill from the future's own quote.
        payload: dict[str, Any] = {
            "action": action,
            "symbol": symbol,
            "qty_lots": settings.default_lots,
            # The bar this *book* acted on, not the engine's. ``_last_bar_at``
            # tracks the active run only, so a 60-minute book's order was stamped
            # with the 15-minute book's bar - and the duplicate guard then
            # compared it against the wrong row.
            "signal_id": f"GIFT-{action}-{book}-{run.state.last_bar_at if run is not None else self._last_bar_at}",
            "strategy": book,
            "source": "GIFT_NIFTY_ENGINE",
            "gift_detail": detail,
            "paper_after_hours": paper_after_hours,
            "max_age_seconds": max_age_seconds or self._signal_max_age(),
        }
        if signal_timestamp is not None:
            payload["timestamp"] = signal_timestamp.astimezone(IST).isoformat()
        # The completed signal bar, for the audit trail. Only the bar the signal
        # came from is passed - never a forming one - and only when the caller
        # had it.
        if signal_candle:
            payload["signal_candle"] = signal_candle
        # `price` is deliberately NOT set to the GIFT close. A payload price is
        # treated as an after-hours *fill* reference, so stamping the index level
        # there is what put the basis into every pre-market entry. `/api/signal`
        # callers may still supply one for the close-only webhook and mirror
        # paths, but the engine fills from the future's own quote.

        try:
            result = manager.process_signal(
                payload, source="ENGINE", strategy_id=book, mode=mode
            )
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
        # Tell the operator a trade opened. Without this the only messages are
        # problems, so a working bot is indistinguishable from a dead one - and
        # the first thing anyone asks after a signal is "did it take it?".
        if result.ok:
            # getattr throughout: this runs on the order path, and a notification
            # must never be able to fail an order that already succeeded.
            price = getattr(result, "price", None)
            symbol = getattr(result, "symbol", None)
            quantity = getattr(result, "quantity", None)
            order_ids = getattr(result, "order_ids", ()) or ()
            detail_text = f"{action} {symbol or ''} {quantity or ''}".strip()
            if price:
                detail_text += f" @ {price:.2f}"
            _trade_alert(
                "TRADE_OPENED",
                detail_text,
                severity="info",
                symbol=symbol,
                fields={
                    "strategy": book,
                    "venue": mode,
                    "symbol": symbol,
                    "action": action,
                    "quantity": quantity,
                    "price": f"{price:.2f}" if price else None,
                    "order_id": ", ".join(str(o) for o in order_ids) or None,
                    "position_id": getattr(result, "position_id", None),
                    "bar": (signal_candle or {}).get("time"),
                },
            )
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
                self.enforce_rollover()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Rollover check failed")
                with self._lock:
                    self.state.last_error = f"rollover check: {exc}"
            # Keep the shadow book in step with the live one. Cheap when the
            # mirror is off or the two already agree, so it rides the same clock
            # as the strategy rather than owning a thread.
            try:
                from paper_mirror import get_mirror

                get_mirror().reconcile()
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Mirror reconcile failed")
            # Every configured strategy is evaluated on the same tick. One book
            # raising must not stop the others: they are independent strategies,
            # and a failure in one is not a reason to go blind in the rest.
            for run in list(self.runs):
                try:
                    self.evaluate_run(run)
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("Engine evaluation failed for %s", run.id)
                    with self._lock:
                        run.state.last_error = str(exc)
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

    @staticmethod
    def _safe_run_status(run: StrategyRun) -> dict[str, Any]:
        """One run's status, degraded rather than raising."""
        try:
            return run.status()
        except Exception as exc:  # pragma: no cover - defensive
            return {
                "id": getattr(run, "id", "?"),
                "name": getattr(run, "display_name", "?"),
                "enabled": getattr(run, "enabled", False),
                "error": f"status unavailable: {exc}",
            }

    def status(self) -> dict[str, Any]:
        feed = get_feed()
        # Deliberately no history refresh here. This is the dashboard's poll
        # endpoint (every 1.5 s), and refresh_history is a synchronous Kite call
        # whenever its 30 s throttle expires - so one poll in twenty would block
        # on the network and the whole dashboard would visibly stall. The engine
        # loop and the mark loop already refresh it on their own cadence, so the
        # series this reads is at most one loop period old.
        candles, current_bar_in_progress = feed.candle_snapshot()
        closed_candles = candles[:-1] if current_bar_in_progress else candles
        # The bar the engine trades, and the one before it. Named for what they
        # are rather than for their position in the list: "previous" used to mean
        # "the last closed bar", which read as the bar before that.
        last_closed_bar_at = closed_candles[-1]["time"] if closed_candles else None
        prior_closed_bar_at = closed_candles[-2]["time"] if len(closed_candles) > 1 else None
        forming_bar_at = candles[-1]["time"] if current_bar_in_progress and candles else None

        # The dashboard polls this every 1.5s but the inputs only change when a
        # bar closes, so the verdicts are memoised on the bar timestamp. This
        # turns a poll into a dict lookup instead of two full indicator passes.
        #
        # Both verdicts are computed from *closed* bars. The engine trades the
        # last closed bar, so evaluating the forming one here made the dashboard
        # advertise signals that could never fire: a wide forming candle flipped
        # the rule checks to BULL while the engine still saw NO_SIGNAL on the bar
        # it actually acts on. The forming bar is still reported - as
        # `current_bar_in_progress` and `forming_bar_at` - but it never decides a
        # signal.
        current_verdict = self._cached_verdict("current", closed_candles, last_closed_bar_at)
        previous_verdict = self._cached_verdict(
            "previous", closed_candles[:-1], prior_closed_bar_at
        )

        market = market_status()
        with self._lock:
            payload = self.state.to_dict()
        payload["feed"] = feed.status()
        payload["current_values"] = current_verdict.values if current_verdict else None
        payload["current_conditions"] = current_verdict.conditions if current_verdict else None
        # The close the verdict was computed on. The indicator levels alone do not
        # say how far price is from flipping the rule, which is the question a
        # trader actually asks of a live signal.
        payload["current_close"] = current_verdict.close if current_verdict else None
        # The bar the verdict was computed from - the last *closed* one. The
        # forming bar's own timestamp is reported separately so the UI can label
        # it without implying the signal came from it.
        payload["current_bar_at"] = last_closed_bar_at
        payload["current_bar_in_progress"] = current_bar_in_progress
        payload["forming_bar_at"] = forming_bar_at
        payload["previous_values"] = previous_verdict.values if previous_verdict else None
        payload["previous_conditions"] = previous_verdict.conditions if previous_verdict else None
        payload["previous_bar_at"] = prior_closed_bar_at
        payload["market"] = market
        payload["execution_symbol"] = get_order_manager().current_symbol()
        mode = get_execution_mode()
        payload["mode"] = mode
        payload["will_execute"] = bool(market["is_open"] or mode == PAPER)
        payload["paused"] = get_order_manager().paused
        payload["paused_reason"] = get_order_manager().paused_reason
        payload["risk"] = risk.limits()
        # The session's realised result against the cap, so the dashboard can
        # show how much room is left rather than only that entries are blocked.
        payload["risk_breach"] = self._breach.to_dict() if self._breach else None
        # The entry the bot still owes, so the dashboard can explain a flat book
        # while the indicators point: "waiting for a fresh signal" and "waiting to
        # fill a signal that already fired" are very different states.
        payload["pending_signal"] = self._pending_signal
        # Every configured run, with its own counters and source state. The
        # top-level payload stays as it was so existing dashboard code and API
        # clients are unchanged; this is additive.
        payload["strategies"] = [
            # Per-run reporting is diagnostic, so one run's broken source must
            # not take down the whole dashboard poll.
            (self._safe_run_status(run)) for run in self.runs
        ]
        try:
            from portfolio import get_aggregator

            payload["portfolio"] = get_aggregator().exposure(get_execution_mode())
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not compute portfolio exposure: %s", exc)
            payload["portfolio"] = None
        payload["pending_signal_bar"] = self._pending_signal_bar
        # The loop refreshes this every few seconds, but the dashboard must be
        # able to warn from the very first poll - before the first tick, and even
        # if the loop thread has died, which is exactly when a rollover matters.
        if not payload.get("rollover"):
            try:
                payload["rollover"] = rollover_status()
                payload["rollover_paused"] = bool(payload["rollover"]["rolled"])
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("Rollover status unavailable: %s", exc)
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
