"""Surikbs entry filter - a faithful port of the ``st.pine`` strategy block.

The Pine script only fires ``strategy.entry`` when five independent conditions
agree (st.pine sections 12-14)::

    buyST   = strategyBullCount >= 2          # triple SuperTrend (10,1) (11,2) (12,3)
    buyRSI  = ta.rsi(close, 14) < 20
    buyTSV  = math.sum((close - close[1]) * volume, 13) > 0
    buyPSAR = close > ta.sar(0.02, 0.02, 0.2)

    longCondition  = buyST and buyRSI and buyTSV and buyPSAR
    shortCondition = sellST and sellRSI and sellTSV and sellPSAR
    buySignal      = barstate.isconfirmed and longCondition and not longCondition[1]

This module is the strategy: :mod:`engine` feeds it GIFT Nifty candles built
from live ticks and acts on the verdict. There is no external signal source to
reconcile against.

The indicator maths is implemented here with the same semantics as Pine:

* ``ta.rsi``   -> Wilder smoothing.
* ``ta.sar``   -> Wilder's parabolic SAR with Pine's 2-bar flip rule.
* ``ta.supertrend`` -> classic SuperTrend, direction negative = bullish.
* ``tsv``      -> rolling sum of ``(close - close[1]) * volume``.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger("strategy")

BULL = "BULL"
BEAR = "BEAR"
NEUTRAL = "NEUTRAL"

# Defaults mirror the input.* values in st.pine.
DEFAULTS: dict[str, float] = {
    "rsi_length": 14,
    "rsi_buy_max": 20.0,
    "rsi_sell_min": 80.0,
    "tsv_length": 13,
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
def sma(values: Sequence[float], length: int) -> list[Optional[float]]:
    if length <= 0:
        raise ValueError("length must be positive")
    out: list[Optional[float]] = [None] * len(values)
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= length:
            running -= values[i - length]
        if i >= length - 1:
            out[i] = running / length
    return out


def rsi(values: Sequence[float], length: int = 14) -> list[Optional[float]]:
    """Wilder's RSI, matching ``ta.rsi``."""
    out: list[Optional[float]] = [None] * len(values)
    if length <= 0 or len(values) <= length:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / length
    avg_loss = losses / length
    out[length] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(length + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (length - 1) + max(change, 0.0)) / length
        avg_loss = (avg_loss * (length - 1) + max(-change, 0.0)) / length
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    length: int,
) -> list[Optional[float]]:
    """Wilder's ATR as used by ``ta.atr``."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if length <= 0 or n < length:
        return out
    trs: list[float] = [0.0] * n
    for i in range(n):
        if i == 0:
            trs[i] = highs[i] - lows[i]
        else:
            trs[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
    seed = sum(trs[:length]) / length
    out[length - 1] = seed
    prev = seed
    for i in range(length, n):
        prev = (prev * (length - 1) + trs[i]) / length
        out[i] = prev
    return out


def psar(
    highs: Sequence[float],
    lows: Sequence[float],
    start: float = 0.02,
    increment: float = 0.02,
    maximum: float = 0.2,
) -> list[Optional[float]]:
    """Wilder's parabolic SAR (``ta.sar``), direction carried in the sign."""
    n = len(highs)
    out: list[Optional[float]] = [None] * n
    if n < 2:
        return out
    rising = True
    extreme = highs[0]
    sar = lows[0]
    accel = start
    for i in range(1, n):
        prior = sar
        sar = prior + accel * (extreme - prior)
        if rising:
            sar = min(sar, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < sar:
                rising = False
                sar = extreme
                extreme = lows[i]
                accel = start
            elif highs[i] > extreme:
                extreme = highs[i]
                accel = min(accel + increment, maximum)
        else:
            sar = max(sar, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > sar:
                rising = True
                sar = extreme
                extreme = highs[i]
                accel = start
            elif lows[i] < extreme:
                extreme = lows[i]
                accel = min(accel + increment, maximum)
        out[i] = sar
    return out


def supertrend(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    factor: float,
    length: int,
) -> tuple[list[Optional[float]], list[int]]:
    """Classic SuperTrend. Direction is ``-1`` bullish, ``1`` bearish (Pine)."""
    n = len(closes)
    atrs = atr(highs, lows, closes, length)
    line: list[Optional[float]] = [None] * n
    direction: list[int] = [0] * n
    upper = 0.0
    lower = 0.0
    trend = 1
    started = False
    for i in range(n):
        a = atrs[i]
        if a is None:
            continue
        hl2 = (highs[i] + lows[i]) / 2.0
        band_top = hl2 + factor * a
        band_bot = hl2 - factor * a
        if not started:
            upper, lower = band_top, band_bot
            trend = 1 if closes[i] <= band_top else -1
            started = True
        else:
            upper = band_top if (band_top < upper or closes[i - 1] > upper) else upper
            lower = band_bot if (band_bot > lower or closes[i - 1] < lower) else lower
            if trend == 1 and closes[i] > upper:
                trend = -1
            elif trend == -1 and closes[i] < lower:
                trend = 1
        direction[i] = trend
        line[i] = lower if trend == -1 else upper
    return line, direction


def tsv(closes: Sequence[float], volumes: Sequence[float], length: int) -> list[Optional[float]]:
    """``math.sum((close - close[1]) * volume, tsvLength)`` from st.pine."""
    n = len(closes)
    out: list[Optional[float]] = [None] * n
    if length <= 0:
        return out
    contributions = [0.0] * n
    for i in range(1, n):
        contributions[i] = (closes[i] - closes[i - 1]) * volumes[i]
    running = 0.0
    for i in range(1, n):
        running += contributions[i]
        if i > length:
            running -= contributions[i - length]
        if i >= length:
            out[i] = running
    return out


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
    *,
    warmup: int = 5,
) -> StrategyVerdict:
    """Evaluate the ``st.pine`` long/short conditions on the last closed candle.

    ``candles`` is chronological OHLCV data (oldest first) with ``open``,
    ``high``, ``low``, ``close`` and ``volume`` keys. The final element must be
    a closed bar.
    """
    cfg = {**DEFAULTS, **(params or {})}
    if len(candles) < 2:
        raise InsufficientData("need at least 2 candles")

    highs = _series(candles, "high")
    lows = _series(candles, "low")
    closes = _series(candles, "close")
    volumes = _series(candles, "volume")
    n = len(closes)

    rsi_len = int(cfg["rsi_length"])
    tsv_len = int(cfg["tsv_length"])

    rsi_series = rsi(closes, rsi_len)
    tsv_series = tsv(closes, volumes, tsv_len)
    psar_series = psar(
        highs, lows, float(cfg["psar_start"]), float(cfg["psar_increment"]), float(cfg["psar_maximum"])
    )

    need = max(rsi_len + 1, tsv_len + 1) + warmup
    if n < need:
        raise InsufficientData(f"need ~{need} closed candles, got {n}")

    last = n - 1
    close = closes[last]
    rsi_val = rsi_series[last]
    tsv_val = tsv_series[last]
    psar_val = psar_series[last]

    directions: list[int] = []
    st_lines: dict[str, Optional[float]] = {}
    for factor, length in SUPERTRENDS:
        line, direction = supertrend(highs, lows, closes, factor, length)
        directions.append(direction[last])
        st_lines[f"st_{length}_{factor:g}"] = line[last]

    if any(d == 0 for d in directions) or None in (rsi_val, tsv_val, psar_val):
        raise InsufficientData("indicators not warm on the final candle")

    bull_count = sum(1 for d in directions if d < 0)
    bear_count = sum(1 for d in directions if d > 0)
    min_agree = int(cfg["st_min_agree"])

    buy = {
        "supertrend": bull_count >= min_agree,
        "rsi": float(rsi_val) < float(cfg["rsi_buy_max"]),  # type: ignore[arg-type]
        "tsv": float(tsv_val) > 0,  # type: ignore[arg-type]
        "psar": close > float(psar_val),  # type: ignore[arg-type]
    }
    sell = {
        "supertrend": bear_count >= min_agree,
        "rsi": float(rsi_val) > float(cfg["rsi_sell_min"]),  # type: ignore[arg-type]
        "tsv": float(tsv_val) < 0,  # type: ignore[arg-type]
        "psar": close < float(psar_val),  # type: ignore[arg-type]
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
        "rsi": round(float(rsi_val), 2),  # type: ignore[arg-type]
        "tsv": round(float(tsv_val), 2),  # type: ignore[arg-type]
        "psar": round(float(psar_val), 4),  # type: ignore[arg-type]
        "supertrend_bull": bull_count,
        "supertrend_bear": bear_count,
        **{k: (round(v, 4) if isinstance(v, float) and math.isfinite(v) else v) for k, v in st_lines.items()},
    }
    detail = (
        f"close={close:.2f} rsi={float(rsi_val):.2f} tsv={float(tsv_val):.1f} "
        f"psar={float(psar_val):.2f} "
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
    "sma",
    "rsi",
    "atr",
    "psar",
    "supertrend",
    "tsv",
]
