"""Surikbs entry rules based on the SuperTrend and PSAR sections of ``st.pine``.

The Pine script only fires ``strategy.entry`` when both entry rules agree::

    buyST   = strategyBullCount >= 2          # triple SuperTrend (10,1) (11,2) (12,3)
    buyPSAR = close > ta.sar(0.02, 0.02, 0.2)

    longCondition  = buyST and buyPSAR
    shortCondition = sellST and sellPSAR
    buySignal      = barstate.isconfirmed and longCondition and not longCondition[1]

This module applies the strategy rules to actual GIFT Nifty candles using
``pandas-ta`` for indicator calculations. It does not synthesize candles;
order signals still use completed bars only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

import pandas as pd
import pandas_ta as ta

BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"

# Defaults mirror the input.* values in st.pine.
DEFAULTS: dict[str, float] = {
    "psar_start": 0.02,
    "psar_increment": 0.02,
    "psar_maximum": 0.2,
    "st_min_agree": 2,
}

# Triple SuperTrend (factor, atr_length) as configured in st.pine section 10.
SUPERTRENDS: tuple[tuple[float, int], ...] = ((1.0, 10), (2.0, 11), (3.0, 12))


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
    """Return the library's Parabolic SAR line, merging long and short rows."""
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, and closes must have equal lengths")
    if len(highs) < 2:
        return [None] * len(highs)

    frame = ta.psar(
        _indicator_series(highs),
        _indicator_series(lows),
        _indicator_series(closes),
        af0=start,
        af=increment,
        max_af=maximum,
    )
    suffix = f"{float(start)}_{float(maximum)}"
    line = frame[f"PSARl_{suffix}"].combine_first(frame[f"PSARs_{suffix}"])
    return _optional_floats(line)


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    factor: float,
    length: int,
) -> tuple[list[Optional[float]], list[int]]:
    """Return pandas-ta SuperTrend values with negative direction meaning bullish."""
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

    highs = _series(candles, "high")
    lows = _series(candles, "low")
    closes = _series(candles, "close")
    n = len(closes)

    need = max(max(length for _, length in SUPERTRENDS) + 1, 2)
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
    for factor, length in SUPERTRENDS:
        line, direction = supertrend(highs, lows, closes, factor, length)
        directions.append(direction[last])
        st_lines[f"st_{length}_{factor:g}"] = line[last]

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
        **{k: (round(v, 4) if isinstance(v, float) and math.isfinite(v) else v) for k, v in st_lines.items()},
    }
    detail = (
        f"close={close:.2f} psar={psar_val:.2f} "
        f"ST bull={bull_count}/3 bear={bear_count}/3"
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
    "SUPERTRENDS",
    "InsufficientData",
    "StrategyVerdict",
    "evaluate",
    "psar",
    "supertrend",
]
