"""One strategy's book: its id, its bar source, its params and its own state.

Before this existed, ``StrategyEngine`` held every piece of per-strategy state as
an attribute: the last bar seen, the owed-entry latch, the overnight flag, the
dashboard counters. That worked because there was exactly one strategy. A second
one needs the same fields, separately, and the only honest way to have that is an
object per strategy rather than a parallel set of attributes on the engine.

What belongs here and what does not:

* **Here** - anything whose value would differ between two strategies. The id, the
  source it reads, the indicator params, the latch, the overnight state, the
  counters the dashboard shows.
* **Not here** - anything that is the same for both, because it describes the
  process rather than a book: the lock, the thread, the stop event, the order
  manager, the ledger.

The owed-entry latch is the reason this class is not cosmetic. Its keys in the
``settings`` table used to be global (``engine_pending_signal``), so two
strategies would have overwritten each other's owed entry on every bar - one
book's deferred signal silently becoming the other's. The keys are now derived
from the strategy id.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import database as db
import strategy
from bar_source import BarSource
from config import settings

log = logging.getLogger("strategy_run")

# Prefix for the persisted latch keys. The strategy id is appended, so every book
# has its own pair and no two can collide.
PENDING_SIGNAL_KEY = "engine_pending_signal"
PENDING_SIGNAL_BAR_KEY = "engine_pending_signal_bar"
# Per-strategy LIVE/PAPER override, persisted so a restart keeps the operator's
# choice. The value is the book's own mode; absence means "follow the config".
MODE_KEY = "strategy_mode"


@dataclass
class RunState:
    """Counters and last-verdict view for one strategy, shown on the dashboard."""

    ready: bool = False
    bars: int = 0
    warmup_needed: int = 0
    warmup_have: int = 0
    last_bar_at: Optional[str] = None
    last_eval_at: Optional[str] = None
    # How long after the evaluated bar closed it was actually evaluated. A healthy
    # book is a few seconds; a value approaching the interval means the bar was
    # not seen when it closed, which is exactly the delay that let a flip go
    # unactioned. Reported rather than logged only, because a stale series looks
    # identical to a quiet market on every other field.
    bar_lag_seconds: Optional[float] = None
    last_signal: str = strategy.NO_SIGNAL
    last_detail: str = ""
    last_conditions: dict[str, bool] = field(default_factory=dict)
    last_values: dict[str, Any] = field(default_factory=dict)
    last_action: Optional[str] = None
    last_action_at: Optional[str] = None
    last_result: Optional[dict[str, Any]] = None
    last_error: Optional[str] = None
    evaluations: int = 0
    signals_fired: int = 0
    orders_sent: int = 0
    orders_blocked: int = 0
    deferred: int = 0
    skipped: int = 0
    # True while the newest closed bar is still pre-open, so the reconcile waits
    # for the first bar that closes in-session before deciding.
    overnight_pending: bool = False
    # The position the last exit closed, so the dashboard can show it.
    last_exit: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "bars": self.bars,
            "warmup_needed": self.warmup_needed,
            "warmup_have": self.warmup_have,
            "last_bar_at": self.last_bar_at,
            "last_eval_at": self.last_eval_at,
            "bar_lag_seconds": self.bar_lag_seconds,
            "last_signal": self.last_signal,
            "last_detail": self.last_detail,
            "last_conditions": self.last_conditions,
            "last_values": self.last_values,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "evaluations": self.evaluations,
            "signals_fired": self.signals_fired,
            "orders_sent": self.orders_sent,
            "orders_blocked": self.orders_blocked,
            "deferred": self.deferred,
            "skipped": self.skipped,
            "overnight_pending": self.overnight_pending,
            "last_exit": self.last_exit,
        }


class StrategyRun:
    """A strategy, its data source, and everything that is true only of it."""

    def __init__(
        self,
        strategy_id: str,
        source: BarSource,
        *,
        params: Optional[dict[str, Any]] = None,
        lots: Optional[int] = None,
        display_name: Optional[str] = None,
        enabled: bool = True,
        live: bool = False,
        legs: Optional[tuple[str, ...]] = None,
        stop_rules: Any = None,
    ) -> None:
        self.id = (strategy_id or db.DEFAULT_STRATEGY_ID).strip() or db.DEFAULT_STRATEGY_ID
        self.source = source
        self.params = dict(params or {})
        self.lots = int(lots) if lots is not None else int(settings.default_lots)
        self.display_name = display_name or self.id
        self.enabled = bool(enabled)
        # The instruments this book places orders on. A signal is generated from
        # one series; which venues carry it is a separate decision, and one
        # strategy may want both.
        #
        # Unset defaults to what that book did before legs existed, which is not
        # the same for both sources: a GIFT-sourced book has always settled the
        # paper index book *and* placed the futures order, while a NIFTY-sourced
        # one never had an index leg to settle. Defaulting both to futures alone
        # would silently switch off an existing deployment's GIFT book.
        self.legs: tuple[str, ...] = tuple(legs) if legs else self._default_legs()
        # Whether this book trades real money when the bot is in LIVE. Defaulted
        # off for the same reason as the config default: an unset flag must not
        # be what arms a book, and the GIFT book cannot be filled as itself.
        self.live = bool(live)
        # This book's protective stop. ``None`` means the signal-only exit it had
        # before stops existed, so an unconfigured book is unchanged.
        self.stop_rules = stop_rules
        self.state = RunState()

        self._pending_signal: Optional[str] = None
        self._pending_signal_bar: Optional[str] = None
        self._pending_loaded = False

    # -- identity --------------------------------------------------------
    @property
    def pending_key(self) -> str:
        """Latch key for this book. Distinct per strategy, by construction."""
        return f"{PENDING_SIGNAL_KEY}:{self.id}"

    @property
    def pending_bar_key(self) -> str:
        return f"{PENDING_SIGNAL_BAR_KEY}:{self.id}"

    @property
    def mode_key(self) -> str:
        """Persisted key for this book's own LIVE/PAPER choice."""
        return f"{MODE_KEY}:{self.id}"

    def stored_mode(self) -> Optional[str]:
        """The mode this book was last switched to, or ``None`` if never set.

        Read from the ledger so a restart resumes the operator's choice rather
        than silently reverting to the config default - a book that was live
        coming back as paper (or the reverse) is exactly the surprise this
        persistence exists to prevent.
        """
        try:
            value = db.get_settings().get(self.mode_key)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not read the stored mode for %s: %s", self.id, exc)
            return None
        from config import LIVE, PAPER

        return value if value in {LIVE, PAPER} else None

    def set_mode(self, mode: str) -> str:
        """Persist this book's own mode. Returns the mode now in force."""
        from config import LIVE, PAPER

        target = str(mode).strip().upper()
        if target not in {LIVE, PAPER}:
            raise ValueError(f"mode must be one of {[PAPER, LIVE]}")
        db.set_settings({self.mode_key: target})
        self.live = target == LIVE
        log.warning("Book %s mode -> %s", self.id, target)
        return target

    def mode(self, global_mode: Optional[str] = None) -> str:
        """The mode *this book* trades in.

        Each strategy has its own LIVE/PAPER switch, independent of the other and
        of the process-wide ``EXECUTION_MODE``. The stored choice wins; a book
        that has never been switched falls back to its configured ``live`` flag,
        and a book with neither stays in paper - the safe direction, because an
        unset flag must never be what puts real money on a signal.

        ``global_mode`` is accepted for callers that still pass the bot's mode,
        but it no longer overrides the book: the whole point of the per-strategy
        switch is that one book can be live while the other is paper.
        """
        from config import LIVE, PAPER

        stored = self.stored_mode()
        if stored is not None:
            return stored
        return LIVE if self.live else PAPER

    @property
    def seconds(self) -> int:
        return int(self.source.seconds)

    @property
    def symbol(self) -> str:
        return getattr(self.source, "key", self.id)

    # -- execution legs --------------------------------------------------
    def _default_legs(self) -> tuple[str, ...]:
        """The legs a book had before ``LEGS`` existed.

        Source-dependent on purpose: a GIFT-sourced book has always settled the
        paper index book and placed the futures order, a NIFTY-sourced one never
        had an index leg. One shared default would change existing behaviour.
        """
        return ("gift", "futures") if self.reads_gift else ("futures",)

    @property
    def reads_gift(self) -> bool:
        """Whether this book's signal series is GIFT Nifty."""
        try:
            from bar_source import GiftBarSource

            return isinstance(self.source, GiftBarSource)
        except Exception:  # pragma: no cover - defensive
            return False

    @property
    def trades_futures(self) -> bool:
        """Whether this book places NFO futures orders."""
        return "futures" in self.legs

    @property
    def trades_gift(self) -> bool:
        """Whether this book settles the paper GIFT index leg.

        Paper-only by construction: GIFT Nifty is a quote-only index on Kite, so
        this leg can never reach a broker no matter what ``live`` says.
        """
        return "gift" in self.legs

    # -- owed-entry latch ------------------------------------------------
    def load_pending_signal(self) -> None:
        """Read this book's owed entry from the ledger, once per process."""
        if self._pending_loaded:
            return
        self._pending_loaded = True
        try:
            stored = db.get_settings()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not read the pending-signal latch for %s: %s", self.id, exc)
            return
        # A ledger written before the latch was per-strategy has the bare key.
        # Only the default book may adopt it, or a second strategy would resume an
        # entry that was never its own.
        value = stored.get(self.pending_key)
        if value is None and self.id == db.DEFAULT_STRATEGY_ID:
            value = stored.get(PENDING_SIGNAL_KEY)
        if value in {strategy.BULL, strategy.BEAR}:
            self._pending_signal = value
            self._pending_signal_bar = (
                stored.get(self.pending_bar_key)
                or (stored.get(PENDING_SIGNAL_BAR_KEY)
                    if self.id == db.DEFAULT_STRATEGY_ID else None)
                or None
            )
            log.info(
                "Resuming owed %s entry for %s from bar %s",
                value, self.id, self._pending_signal_bar,
            )

    def set_pending_signal(
        self, signal: Optional[str], bar_at: Optional[str] = None
    ) -> None:
        """Record (or clear) the direction this book's entry is still owed for.

        Written through to SQLite so the intent survives a restart, under a key
        that includes the strategy id so two books cannot overwrite each other.
        """
        if signal == self._pending_signal and (
            signal is None or bar_at == self._pending_signal_bar
        ):
            return
        self._pending_signal = signal
        self._pending_signal_bar = bar_at if signal is not None else None
        try:
            if signal is None:
                keys = [self.pending_key, self.pending_bar_key]
                if self.id == db.DEFAULT_STRATEGY_ID:
                    # Clear the legacy pair too, or a stale pre-upgrade value
                    # would be adopted again on the next restart.
                    keys += [PENDING_SIGNAL_KEY, PENDING_SIGNAL_BAR_KEY]
                db.delete_settings(keys)
            else:
                db.set_settings(
                    {
                        self.pending_key: signal,
                        self.pending_bar_key: self._pending_signal_bar or "",
                    }
                )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not persist the latch for %s: %s", self.id, exc)

    @property
    def pending_signal(self) -> Optional[str]:
        return self._pending_signal

    @property
    def pending_signal_bar(self) -> Optional[str]:
        return self._pending_signal_bar

    # -- evaluation ------------------------------------------------------
    def evaluate(self) -> Optional[str]:
        """The verdict on the newest closed bar, or ``None`` if not ready.

        Returns the signal string rather than the verdict so the caller can key
        off exactly what the engine acts on; the full verdict is left on
        ``self.last_verdict`` for the dashboard and for the exit rule.
        """
        candles = self.source.candles()
        needed = strategy.required_bars()
        self.state.warmup_needed = needed
        self.state.warmup_have = len(candles)
        self.state.bars = len(candles)
        if len(candles) < needed:
            self.state.ready = False
            return None
        try:
            verdict = strategy.evaluate(candles, self.params)
        except strategy.InsufficientData as exc:
            self.state.ready = False
            self.state.last_error = str(exc)
            return None
        self.last_verdict = verdict
        self.state.ready = True
        self.state.last_error = None
        self.state.last_bar_at = candles[-1]["time"]
        self.state.last_signal = verdict.signal
        self.state.last_detail = verdict.detail
        self.state.last_conditions = verdict.conditions
        self.state.last_values = verdict.values
        self.state.evaluations += 1
        return verdict.signal

    last_verdict: Optional[strategy.StrategyVerdict] = None

    # -- dashboard -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        state = self.state.to_dict()
        # Pause state belongs on the book: the tile carries the toggle, so the
        # poll that repaints it must say whether this book is paused and why.
        try:
            from order_manager import get_order_manager

            manager = get_order_manager()
            state["paused"] = manager.book_paused(self.id)
            state["paused_reason"] = manager.book_paused_reason(self.id)
        except Exception:  # pragma: no cover - defensive
            state["paused"] = False
            state["paused_reason"] = ""
        # The dashboard's Position panel read `state.open_units`, which was never
        # written by anything - so every book read "Flat" whether it was or not.
        # Filled here from the ledger, scoped to this book *and this book's own
        # mode*: with one book live and the other in paper, an unscoped count
        # would report the live leg on the paper band.
        state["open_units"] = self.open_units()
        return {
            "id": self.id,
            "name": self.display_name,
            "enabled": self.enabled,
            "symbol": self.symbol,
            "seconds": self.seconds,
            "lots": self.lots,
            "exit": self.params.get("exit", strategy.EXIT_FLIP),
            # Which instruments carry this book's signals. The dashboard needs
            # this to show one card per strategy with a row per execution leg
            # rather than pretending each strategy is one position.
            "legs": list(self.legs),
            # The protective stop, so the dashboard can show the level and the
            # rules behind it. ``stop_loss`` of zero means the book has no stop.
            "stop_loss": getattr(self.stop_rules, "initial_points", 0.0) if self.stop_rules else 0.0,
            "stop_rules": self.stop_rules.to_dict() if self.stop_rules else None,
            # The entry gates belong in the payload: a strategy labelled "ADX>15"
            # that is silently running unfiltered is worse than one with no
            # filter at all, because the label is what the operator trusts.
            "filter_adx": self.params.get("filter_adx", 0.0),
            "filter_ma": self.params.get("filter_ma", 0),
            # The mode this book would trade if the bot were armed, and the mode
            # it is trading now. The dashboard needs both: "live" on a bot still
            # in PAPER would otherwise read as "this is placing real orders".
            "live": self.live,
            "mode": self.mode(),
            # The close the verdict was computed on. Without it the levels in
            # state.last_values cannot be turned into a distance-to-flip, which
            # is the only thing that makes them actionable.
            "last_close": getattr(self.last_verdict, "close", None),
            "state": state,
            "source": self.source.status(),
        }

    def open_units(self) -> int:
        """Signed units this book holds in its own ledger and its own mode."""
        try:
            return sum(
                int(position["quantity"]) * int(position["position_key"])
                for position in db.get_open_positions(
                    mode=self.mode(), strategy_id=self.id
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not read %s's open units: %s", self.id, exc)
            return 0


__all__ = ["StrategyRun", "RunState", "PENDING_SIGNAL_KEY", "PENDING_SIGNAL_BAR_KEY"]
