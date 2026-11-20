"""Surikbs entry rules: exactly two indicators, SuperTrend and Parabolic SAR.

The strategy fires an entry only when both rules agree::

    buyST   = supertrend is bullish           # single SuperTrend (14, 3)
    buyPSAR = close > ta.sar(0.02, 0.02, 0.2)

    longCondition  = buyST and buyPSAR
    shortCondition = sellST and sellPSAR

There is no third indicator and no vote across several SuperTrend instances.
The pair is also the *exit*: a bar where the two rules disagree is ``NO_SIGNAL``,
and that is what closes an open position. There is no stop-loss and no profit
target - the position is held for as long as the pair keeps agreeing, and the
first bar it stops agreeing is the exit. This module applies the rules to actual
GIFT Nifty candles using ``pandas-ta-classic`` for SuperTrend and a standard
Wilder Parabolic SAR. It does not synthesize candles; order signals still use
completed bars only.

``pandas-ta-classic`` is the maintained fork of ``pandas-ta``. The original
package's latest release (``0.4.71b0``) needs numba, which caps it at Python
3.12, so it cannot be installed on the project's 3.11 interpreter at all. Both
forks produce identical SuperTrend lines and directions (measured on a 300-bar
series), so the swap does not change strategy output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

# pandas and pandas-ta-classic are imported lazily, inside the functions that
# need them. They cost ~58 MB of resident memory between them, and this module is
# imported by config.py for its constants alone - so a process that only reads
# settings was paying for the whole numeric stack. The indicator functions import
# them on first call, which happens once per closed bar.
#
# The names are still module-level so `strategy.pd` keeps working for anything
# that reaches for it; they are simply bound on first use.
pd: Any = None
ta: Any = None


def _load_numeric_stack() -> None:
    """Bind pandas and pandas-ta-classic on first use.

    Idempotent and cheap after the first call. Kept as a function rather than a
    module-level import so importing this module for its constants does not pull
    in numpy, pandas and the indicator library.
    """
    global pd, ta
    if pd is not None:
        return
    import pandas as _pd
    import pandas_ta_classic as _ta

    pd = _pd
    ta = _ta


BULL = "BULL"
BEAR = "BEAR"
# The pair disagreeing. This is a *state*, not the absence of one: it is the
# exit condition, so an open position is closed on the first bar that carries it.
NO_SIGNAL = "NO_SIGNAL"

# The one SuperTrend instance: 14-period ATR at 3x. Fixed rather than
# configurable, because a second instance would reintroduce the vote this
# strategy deliberately does not have.
SUPERTREND_ATR_LENGTH = 14
SUPERTREND_MULTIPLIER = 3.0

# The two exit rules, named so a typo becomes a visible default rather than a
# silently different strategy.
EXIT_FLIP = "flip"
EXIT_FLAT = "flat"

# Defaults mirror the input.* values in st.pine.
DEFAULTS: dict[str, Any] = {
    "psar_start": 0.02,
    "psar_increment": 0.02,
    "psar_maximum": 0.2,
    "exit": EXIT_FLIP,
    # Optional entry gates, both off by default. They suppress *entries* only:
    # an open position is still closed by the exit rule, because a filter that
    # blocked an exit would hold a losing trade through the very conditions it
    # was meant to avoid.
    "filter_adx": 0.0,
    "filter_ma": 0,
}


def required_bars(params: Optional[dict[str, Any]] = None) -> int:
    """Closed bars needed before both indicators have a value.

    SuperTrend needs ``length`` bars of ATR warm-up plus one bar to compare
    against, so the 14×3 instance requires 15 closed bars. A moving-average
    filter needs its own window on top of that, or the first bars of a series
    would silently pass a gate that had no data to test.
    """
    cfg = {**DEFAULTS, **(params or {})}
    base = SUPERTREND_ATR_LENGTH + 1
    window = int(cfg.get("filter_ma") or 0)
    return max(base, window + 1) if window else base


class InsufficientData(RuntimeError):
    """Raised when the candle series is too short to evaluate the strategy."""


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def _indicator_series(values: Sequence[float]) -> Any:
    _load_numeric_stack()
    return pd.Series(values, dtype="float64")


def _optional_floats(values: Iterable[Any]) -> list[Optional[float]]:
    _load_numeric_stack()
    return [None if pd.isna(value) else float(value) for value in values]


def psar(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.2,
) -> list[Optional[float]]:
    """Wilder Parabolic SAR as defined by Pine ``ta.sar``.

    ``pandas-ta-classic``'s ``psar`` was measured against this definition and against
    TA-Lib on a clean series: it diverged on roughly 30% of bars (and its
    ``tv`` flag has no effect), so SAR is computed here instead.

    This mirrors the published Pine reference exactly, including its
    initialization: bar 1 only seeds the trend and is never a reversal bar, and
    the loop starts at bar 2. Verified against the reference on 1119 real GIFT
    Nifty bars — identical from bar 27 onward, which is well inside the engine's
    13-bar warm-up.
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, and closes must have equal lengths")
    out: list[Optional[float]] = [None] * len(highs)
    if len(highs) < 2:
        return out

    is_below = closes[1] > closes[0]
    result = lows[0] if is_below else highs[0]
    extreme = highs[1] if is_below else lows[1]
    acceleration = start
    out[1] = result

    for i in range(2, len(highs)):
        result += acceleration * (extreme - result)
        if is_below:
            if result > lows[i]:
                is_below = False
                result = max(highs[i], extreme)
                extreme = lows[i]
                acceleration = start
            elif highs[i] > extreme:
                extreme = highs[i]
                acceleration = min(acceleration + increment, maximum)
        else:
            if result < highs[i]:
                is_below = True
                result = min(lows[i], extreme)
                extreme = highs[i]
                acceleration = start
            elif lows[i] < extreme:
                extreme = lows[i]
                acceleration = min(acceleration + increment, maximum)

        if is_below:
            result = min(result, lows[i - 1], lows[i - 2])
        else:
            result = max(result, highs[i - 1], highs[i - 2])
        out[i] = result
    return out


def supertrend_key() -> str:
    """Stable dict key for the single SuperTrend instance, e.g. ``st_14_3``."""
    return f"st_{SUPERTREND_ATR_LENGTH}_{SUPERTREND_MULTIPLIER:g}"


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    factor: float,
    length: int,
) -> tuple[list[Optional[float]], list[int]]:
    """Return pandas-ta-classic SuperTrend values with negative direction meaning bullish."""
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, and closes must have equal lengths")
    if length <= 0:
        raise ValueError("length must be positive")
    if not closes:
        return [], []

    _load_numeric_stack()
    frame = ta.supertrend(
        _indicator_series(highs),
        _indicator_series(lows),
        _indicator_series(closes),
        length=length,
        multiplier=factor,
    )
    # pandas-ta names its columns from the arguments, so the suffix must be
    # derived from the same values rather than hardcoded.
    suffix = f"{length}_{float(factor)}"
    line = _optional_floats(frame[f"SUPERT_{suffix}"])
    library_directions = frame[f"SUPERTd_{suffix}"]
    directions = [
        0 if pd.isna(value) else -int(value)
        for value in library_directions
    ]
    return line, directions


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyVerdict:
    """Outcome of evaluating the Pine conditions on the latest closed bar."""

    signal: str  # BULL | BEAR | NO_SIGNAL
    close: float
    conditions: dict[str, bool] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    bars: int = 0
    detail: str = ""
    # The verdict the bar *before* this one carried. Kept beside the signal so a
    # consumer can tell a bar that *became* directional from a bar that merely
    # still is - the difference between a new signal and an existing state.
    previous_signal: str = NO_SIGNAL
    # The completed signal candle's own OHLC. Carried on the verdict so the
    # dashboard can show the bar a decision was made on without re-reading the
    # series.
    signal_candle: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.signal in {BULL, BEAR}

    def exits(self, exit_rule: str = EXIT_FLIP) -> bool:
        """True when this bar closes a position under ``exit_rule``.

        The two rules are kept side by side because the backtest compares them on
        the same series, and because a live book must be explicit about which one
        it is running. ``flip`` holds through ``NO_SIGNAL`` and exits on a
        reversal; ``flat`` exits on the first disagreement.
        """
        return exits(self, exit_rule)

    @property
    def fresh(self) -> bool:
        """True when this bar is where the pair started agreeing.

        A transition, not a state: the same directional verdict as the previous
        bar is *not* fresh, however long it has held. This is what keeps an exit
        from being re-entered on the signal that was already in force when it
        fired.
        """
        return self.confirmed and self.signal != self.previous_signal

    @property
    def flat(self) -> bool:
        """True when the pair stopped agreeing.

        A transition, like :attr:`fresh`: the first bar that carries
        ``NO_SIGNAL`` after a directional one. Reported for the dashboard, but
        **not** the exit - see :attr:`opposite`.
        """
        return self.signal == NO_SIGNAL and self.previous_signal != NO_SIGNAL

    @property
    def opposite(self) -> bool:
        """True when the pair flipped against the direction it last held.

        This is the exit. A position is held through every ``NO_SIGNAL`` bar and
        closed only when the indicators turn the *other* way - so the trade runs
        until the trend it was taken on actually reverses, rather than being cut
        on the first indecisive bar.

        Measured over three years of real GIFT Nifty bars
        (``scripts/backtest.py``), exiting on the flip instead of on ``NO_SIGNAL``
        lowers the win rate but raises the payoff ratio: the average win grows
        faster than the win rate falls. Neither clears the 14.85 points per round
        trip on its own - see the backtest's output before trusting either.

        A transition, like :attr:`fresh`: the bar that *becomes* the opposite
        direction. A run of opposite bars is one exit, not one per bar.
        """
        return (
            self.signal in {BULL, BEAR}
            and self.previous_signal in {BULL, BEAR}
            and self.signal != self.previous_signal
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "previous_signal": self.previous_signal,
            "fresh": self.fresh,
            "flat": self.flat,
            "opposite": self.opposite,
            "close": self.close,
            "conditions": self.conditions,
            "values": self.values,
            "bars": self.bars,
            "detail": self.detail,
            "signal_candle": self.signal_candle,
        }


def _series(candles: Iterable[Any], key: str) -> list[float]:
    out: list[float] = []
    for candle in candles:
        if isinstance(candle, dict):
            value = candle.get(key)
        else:
            value = getattr(candle, key, None)
        if value is None:
            raise InsufficientData(f"candle missing {key!r}")
        out.append(float(value))
    return out


def exits(verdict: Any, exit_rule: str = EXIT_FLIP) -> bool:
    """Whether ``verdict`` closes an open position under ``exit_rule``.

    Module-level so the engine can apply the rule to any verdict-shaped object,
    including the stand-ins tests use. ``flip`` reads :attr:`StrategyVerdict.opposite`
    and ``flat`` reads :attr:`StrategyVerdict.flat`; an unknown rule is treated as
    ``flip``, the shipped default.
    """
    if (exit_rule or EXIT_FLIP).strip().lower() == EXIT_FLAT:
        return bool(getattr(verdict, "flat", False))
    return bool(getattr(verdict, "opposite", False))


def _classify(
    close: float,
    psar_val: float,
    direction: int,
) -> tuple[str, dict[str, bool], dict[str, bool]]:
    """The two rules on one bar: its signal, and each side's own conditions.

    Split out of :func:`evaluate` so the previous bar can be judged by exactly
    the same rule as the current one - a transition is only meaningful if both
    bars are read the same way.
    """
    # Negative direction is bullish, matching the internal convention.
    st_bullish = direction < 0
    st_bearish = direction > 0

    buy = {
        "supertrend": st_bullish,
        "psar": close > psar_val,
    }
    sell = {
        "supertrend": st_bearish,
        # Deliberately the complement of the BUY test rather than ``close <
        # psar_val``. A bar that closes exactly *on* the SAR satisfies neither
        # strict comparison, so the PSAR rule would abstain on a bar whose only
        # ambiguity was an exact equality. PSAR's own reversal logic can pin the
        # SAR to the close (the two-bar clamp), so this is reachable on real data.
        #
        # "Not above the SAR" is the bearish side of a partition: exactly one of
        # the two conditions always holds, so the rule can never silently abstain.
        "psar": close <= psar_val,
    }

    long_ok = all(buy.values())
    short_ok = all(sell.values())
    if long_ok and not short_ok:
        signal = BULL
    elif short_ok and not long_ok:
        signal = BEAR
    else:
        # Both rules cannot pass at once (the PSAR tests partition and the
        # SuperTrend directions are exclusive), so this is the disagreeing bar.
        # It is the exit condition: an open position is closed on it.
        signal = NO_SIGNAL
    return signal, buy, sell


def _entry_gate(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    cfg: dict[str, Any],
    last: int,
) -> dict[str, Any]:
    """Whether the optional entry filters allow a trade on this bar.

    Both filters are off by default and each is independent. ADX measures trend
    strength, so it gates on the trend being worth following; the moving average
    gates on price being on the trend's own side. Neither is a stop - they only
    decide whether a *new* position may open.

    Returns ``{"passed": bool, "adx": float|None, "ma": float|None, "reason": str}``
    so the dashboard and the log can state which gate refused rather than just
    reporting a missing signal.
    """
    adx_min = float(cfg.get("filter_adx") or 0.0)
    ma_window = int(cfg.get("filter_ma") or 0)
    out: dict[str, Any] = {"passed": True, "adx": None, "ma": None, "reason": ""}
    if not adx_min and not ma_window:
        return out

    if adx_min:
        _load_numeric_stack()
        adx_series = ta.adx(
            pd.Series(highs), pd.Series(lows), pd.Series(closes), length=14
        )
        if adx_series is None or adx_series.empty:
            out.update(passed=False, reason="ADX unavailable")
            return out
        adx_val = float(adx_series.iloc[last, 0])
        out["adx"] = round(adx_val, 4)
        if not adx_val > adx_min:
            out.update(passed=False, reason=f"ADX {adx_val:.1f} <= {adx_min:g}")
            return out

    if ma_window:
        if last + 1 < ma_window:
            out.update(passed=False, reason=f"MA{ma_window} not warm")
            return out
        _load_numeric_stack()
        window = pd.Series(closes[last + 1 - ma_window: last + 1])
        ma_val = float(window.mean())
        out["ma"] = round(ma_val, 4)
        if not closes[last] > ma_val:
            out.update(
                passed=False,
                reason=f"close {closes[last]:.2f} <= MA{ma_window} {ma_val:.2f}",
            )
            return out

    return out


def evaluate(
    candles: Sequence[Any],
    params: Optional[dict[str, Any]] = None,
) -> StrategyVerdict:
    """Evaluate the two rules on the last closed candle.

    ``candles`` is chronological OHLC data (oldest first) with ``high``,
    ``low`` and ``close`` keys. The final element must be a closed bar.

    The verdict is ``BULL`` when SuperTrend is bullish *and* price is above the
    SAR, ``BEAR`` when SuperTrend is bearish *and* price is at or below the SAR,
    and ``NO_SIGNAL`` otherwise. ``NO_SIGNAL`` is the exit: the pair no longer
    agrees, so an open position is closed rather than held.

    ``previous_signal`` carries the same rule applied to the bar before, so the
    caller can require a *transition* into a direction rather than accept a
    direction that is merely still in force - and, symmetrically, can tell the
    first disagreeing bar from a run of them.
    """
    cfg = {**DEFAULTS, **(params or {})}
    if len(candles) < 2:
        raise InsufficientData("need at least 2 candles")

    highs = _series(candles, "high")
    lows = _series(candles, "low")
    closes = _series(candles, "close")
    n = len(closes)

    need = required_bars()
    if n < need:
        raise InsufficientData(f"need {need} closed candles, got {n}")

    psar_series = psar(
        highs,
        lows,
        closes,
        float(cfg["psar_start"]),
        float(cfg["psar_increment"]),
        float(cfg["psar_maximum"]),
    )
    last = n - 1
    close = closes[last]
    psar_val = psar_series[last]

    st_line, st_direction = supertrend(
        highs, lows, closes, SUPERTREND_MULTIPLIER, SUPERTREND_ATR_LENGTH
    )
    direction = st_direction[last]
    st_value = st_line[last]

    # The completed signal bar's own OHLC, so the dashboard can show the bar a
    # decision was made on. Never the forming bar.
    signal_candle = {
        "time": candles[last].get("time") if isinstance(candles[last], dict) else None,
        "open": (
            float(candles[last]["open"])
            if isinstance(candles[last], dict) and candles[last].get("open") is not None
            else None
        ),
        "high": float(highs[last]),
        "low": float(lows[last]),
        "close": float(closes[last]),
    }

    if direction == 0 or psar_val is None or st_value is None:
        raise InsufficientData("indicators not warm on the final candle")

    # Negative direction is bullish, matching the internal convention.
    st_bullish = direction < 0
    st_bearish = direction > 0

    signal, buy, sell = _classify(close, psar_val, direction)

    # Entry gates. They can only ever turn a directional signal into NO_SIGNAL,
    # never invent one, so a filtered bar is indistinguishable from a bar where
    # the two rules disagreed - which is exactly what the exit rule already
    # understands. That is what keeps a filter from stranding an open position.
    gate = _entry_gate(closes, highs, lows, cfg, last)
    if signal != NO_SIGNAL and not gate["passed"]:
        signal = NO_SIGNAL

    # The bar before, read by the same rule. Only its *direction* is needed, so a
    # bar whose indicators were not yet warm simply cannot have carried a signal -
    # NO_SIGNAL is the right answer there rather than an error, and it makes the
    # first directional bar of a series count as a transition.
    prev_signal = NO_SIGNAL
    if last >= 1:
        prev_direction = st_direction[last - 1]
        prev_psar = psar_series[last - 1]
        prev_st = st_line[last - 1]
        if prev_direction != 0 and prev_psar is not None and prev_st is not None:
            prev_signal, _, _ = _classify(closes[last - 1], prev_psar, prev_direction)

    # The confirmation is a 2-of-2 vote across the two independent rules - the
    # SuperTrend direction and the PSAR price-position check - and both must
    # pass. Exposed as a count so the engine log and the dashboard can state it
    # directly instead of each re-deriving it from the condition flags.
    confirmations = {
        "buy": sum(1 for passed in buy.values() if passed),
        "sell": sum(1 for passed in sell.values() if passed),
        "total": len(buy),
    }

    values = {
        "psar": round(psar_val, 4),
        "supertrend": round(st_value, 4),
        "supertrend_bullish": st_bullish,
        "supertrend_bearish": st_bearish,
        "buy_confirmations": confirmations["buy"],
        "sell_confirmations": confirmations["sell"],
        "confirmations_total": confirmations["total"],
        "candle_range": round(float(highs[last]) - float(lows[last]), 4),
        "previous_signal": prev_signal,
        "filter_passed": gate["passed"],
        "filter_reason": gate["reason"],
        "filter_adx": gate["adx"],
        "filter_ma": gate["ma"],
        "supertrend_config": {
            "length": SUPERTREND_ATR_LENGTH,
            "multiplier": SUPERTREND_MULTIPLIER,
            "key": supertrend_key(),
            # Negative is bullish, matching the internal convention.
            "direction": direction,
        },
        supertrend_key(): round(st_value, 4),
    }
    detail = (
        f"close={close:.2f} psar={psar_val:.2f} "
        f"ST {'bullish' if st_bullish else 'bearish' if st_bearish else 'flat'} "
        f"confirm BUY {confirmations['buy']}/{confirmations['total']} "
        f"SELL {confirmations['sell']}/{confirmations['total']}"
    )
    if gate["reason"]:
        detail += f" | filtered: {gate['reason']}"
    return StrategyVerdict(
        signal=signal,
        close=close,
        conditions={**{f"buy_{k}": v for k, v in buy.items()}, **{f"sell_{k}": v for k, v in sell.items()}},
        values=values,
        bars=n,
        detail=detail,
        previous_signal=prev_signal,
        signal_candle=signal_candle,
    )


__all__ = [
    "BULL",
    "BEAR",
    "NO_SIGNAL",
    "DEFAULTS",
    "EXIT_FLIP",
    "EXIT_FLAT",
    "SUPERTREND_ATR_LENGTH",
    "SUPERTREND_MULTIPLIER",
    "exits",
    "InsufficientData",
    "StrategyVerdict",
    "evaluate",
    "psar",
    "required_bars",
    "supertrend",
    "supertrend_key",
]
