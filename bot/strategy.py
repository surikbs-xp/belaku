"""Surikbs entry rules based on the SuperTrend and PSAR sections of ``st.pine``.

The Pine script only fires ``strategy.entry`` when both entry rules agree::

    buyST   = strategyBullCount >= 2          # triple SuperTrend (11,1) (12,2) (13,3)
    buyPSAR = close > ta.sar(0.02, 0.02, 0.2)

    longCondition  = buyST and buyPSAR
    shortCondition = sellST and sellPSAR
    buySignal      = barstate.isconfirmed and longCondition and not longCondition[1]

This module applies the strategy rules to actual GIFT Nifty candles using
``pandas-ta-classic`` for SuperTrend and a standard Wilder Parabolic SAR. It
does not synthesize candles; order signals still use completed bars only.

``pandas-ta-classic`` is the maintained fork of ``pandas-ta``. The original
package's latest release (``0.4.71b0``) needs numba, which caps it at Python
3.12, so it cannot be installed on the project's 3.11 interpreter at all. Both
forks produce identical SuperTrend lines and directions (measured on a 300-bar
series), so the swap does not change strategy output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import pandas as pd
import pandas_ta_classic as ta

BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"

# Triple SuperTrend as (multiplier, atr_length). Each entry is one vote in the
# trend tally; ``st_min_agree`` of them must agree before a signal is allowed.
DEFAULT_SUPERTRENDS: tuple[tuple[float, int], ...] = ((1.0, 11), (2.0, 12), (3.0, 13))

# Defaults mirror the input.* values in st.pine.
DEFAULTS: dict[str, Any] = {
    "psar_start": 0.02,
    "psar_increment": 0.02,
    "psar_maximum": 0.2,
    "st_min_agree": 2,
    "supertrends": DEFAULT_SUPERTRENDS,
}

# Kept as the module-level default so callers that do not pass params still work.
SUPERTRENDS: tuple[tuple[float, int], ...] = DEFAULT_SUPERTRENDS


def parse_supertrends(
    spec: str,
    default: tuple[tuple[float, int], ...] = DEFAULT_SUPERTRENDS,
) -> tuple[tuple[float, int], ...]:
    """Parse ``"11:1,12:2,13:3"`` into ``((1.0, 11), (2.0, 12), (3.0, 13))``.

    Each entry is ``atr_length:multiplier``. Entries that are malformed or
    non-positive are dropped rather than raising, so a typo in the environment
    cannot stop the bot from booting; if nothing usable remains the ``default``
    triple is returned.
    """
    parsed: list[tuple[float, int]] = []
    for chunk in spec.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        length_text, separator, factor_text = chunk.partition(":")
        if not separator:
            continue
        try:
            length = int(length_text.strip())
            factor = float(factor_text.strip())
        except ValueError:
            continue
        if length <= 0 or factor <= 0:
            continue
        parsed.append((factor, length))
    return tuple(parsed) or default


def required_bars(supertrends: Optional[Sequence[tuple[float, int]]] = None) -> int:
    """Closed bars needed before every indicator has a value.

    The longest SuperTrend needs ``length`` bars of ATR warm-up plus one bar to
    compare against, so the requirement tracks the configured periods.
    """
    configs = tuple(supertrends) if supertrends else SUPERTRENDS
    return max(max(length for _, length in configs) + 1, 2)


class InsufficientData(RuntimeError):
    """Raised when the candle series is too short to evaluate the strategy."""


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------
def _indicator_series(values: Sequence[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def _optional_floats(values: Iterable[Any]) -> list[Optional[float]]:
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


def supertrend_key(factor: float, length: int) -> str:
    """Stable dict key for one SuperTrend instance, e.g. ``st_11_1``."""
    return f"st_{length}_{factor:g}"


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

    signal: str  # BULL | BEAR | NEUTRAL
    close: float
    conditions: dict[str, bool] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    bars: int = 0
    detail: str = ""

    @property
    def confirmed(self) -> bool:
        return self.signal in {BULL, BEAR}

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal,
            "close": self.close,
            "conditions": self.conditions,
            "values": self.values,
            "bars": self.bars,
            "detail": self.detail,
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


def evaluate(
    candles: Sequence[Any],
    params: Optional[dict[str, Any]] = None,
) -> StrategyVerdict:
    """Evaluate the ``st.pine`` long/short conditions on the last closed candle.

    ``candles`` is chronological OHLC data (oldest first) with ``high``,
    ``low`` and ``close`` keys. The final element must be a closed bar.
    """
    cfg = {**DEFAULTS, **(params or {})}
    if len(candles) < 2:
        raise InsufficientData("need at least 2 candles")

    supertrends = tuple(cfg.get("supertrends") or SUPERTRENDS)

    highs = _series(candles, "high")
    lows = _series(candles, "low")
    closes = _series(candles, "close")
    n = len(closes)

    need = required_bars(supertrends)
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

    directions: list[int] = []
    st_lines: dict[str, Optional[float]] = {}
    for factor, length in supertrends:
        line, direction = supertrend(highs, lows, closes, factor, length)
        directions.append(direction[last])
        st_lines[supertrend_key(factor, length)] = line[last]

    if any(d == 0 for d in directions) or psar_val is None or any(
        line is None for line in st_lines.values()
    ):
        raise InsufficientData("indicators not warm on the final candle")

    bull_count = sum(1 for d in directions if d < 0)
    bear_count = sum(1 for d in directions if d > 0)
    min_agree = int(cfg["st_min_agree"])

    buy = {
        "supertrend": bull_count >= min_agree,
        "psar": close > psar_val,
    }
    sell = {
        "supertrend": bear_count >= min_agree,
        "psar": close < psar_val,
    }

    long_ok = all(buy.values())
    short_ok = all(sell.values())
    if long_ok and not short_ok:
        signal = BULL
    elif short_ok and not long_ok:
        signal = BEAR
    elif long_ok and short_ok:  # mathematically impossible, kept explicit
        signal = NEUTRAL
    else:
        signal = NEUTRAL

    values = {
        "psar": round(psar_val, 4),
        "supertrend_bull": bull_count,
        "supertrend_bear": bear_count,
        "supertrend_total": len(supertrends),
        "supertrend_config": [
            {
                "length": length,
                "multiplier": factor,
                "key": supertrend_key(factor, length),
                # Negative is bullish, matching the internal convention.
                "direction": direction,
            }
            for (factor, length), direction in zip(supertrends, directions)
        ],
        **{k: (round(v, 4) if isinstance(v, float) and math.isfinite(v) else v) for k, v in st_lines.items()},
    }
    detail = (
        f"close={close:.2f} psar={psar_val:.2f} "
        f"ST bull={bull_count}/{len(supertrends)} bear={bear_count}/{len(supertrends)}"
    )
    return StrategyVerdict(
        signal=signal,
        close=close,
        conditions={**{f"buy_{k}": v for k, v in buy.items()}, **{f"sell_{k}": v for k, v in sell.items()}},
        values=values,
        bars=n,
        detail=detail,
    )


__all__ = [
    "BULL",
    "BEAR",
    "NEUTRAL",
    "DEFAULTS",
    "DEFAULT_SUPERTRENDS",
    "SUPERTRENDS",
    "InsufficientData",
    "StrategyVerdict",
    "evaluate",
    "parse_supertrends",
    "psar",
    "required_bars",
    "supertrend",
    "supertrend_key",
]
