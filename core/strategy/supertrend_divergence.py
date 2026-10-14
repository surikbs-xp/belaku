"""
Pure SuperTrend + weighted divergence strategy.

This strategy uses:
- SuperTrend direction as the primary trend filter
- weighted RSI + MACD divergence to confirm trend quality
- a single final signal which is either buy or sell
"""

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from core.strategy.base import BaseStrategy, Signal

logger = logging.getLogger(__name__)


class SuperTrendWeightedDivergenceStrategy(BaseStrategy):
    """Trend-following strategy driven by SuperTrend and weighted divergence."""

    def __init__(self, parameters: Optional[Dict[str, Any]] = None):
        default_parameters = {
            'atr_period': 10,
            'multiplier': 3.0,
            'rsi_period': 14,
            'rsi_weight': 0.6,
            'macd_weight': 0.4,
            'min_strength': 0.55,
            'cooldown_period': 60,
            'max_position_size': 100000,
            'max_signals_per_hour': 10,
        }

        if parameters:
            default_parameters.update(parameters)

        super().__init__('SuperTrendWeightedDivergence', default_parameters)
        self.last_signal_time: Optional[datetime] = None
        self.signals_this_hour = 0
        self.last_hour = datetime.now().hour

    async def analyze(self, market_data: Dict[str, Any]) -> List[Signal]:
        """Generate Trading signals based on SuperTrend with weighted divergence."""
        if not self.enabled:
            return []

        now = datetime.now()
        if self.last_signal_time:
            elapsed = (now - self.last_signal_time).total_seconds()
            if elapsed < self.parameters.get('cooldown_period', 60):
                return []

        if now.hour != self.last_hour:
            self.signals_this_hour = 0
            self.last_hour = now.hour

        if self.signals_this_hour >= self.parameters.get('max_signals_per_hour', 10):
            return []

        signals: List[Signal] = []
        for symbol, payload in market_data.items():
            if not symbol or symbol == 'symbol':
                continue

            candles = self._extract_candles(payload)
            if len(candles) < max(self.parameters.get('rsi_period', 14), self.parameters.get('atr_period', 10) + 5):
                continue

            closes = np.asarray([float(c['close']) for c in candles], dtype=float)
            highs = np.asarray([float(c['high']) for c in candles], dtype=float)
            lows = np.asarray([float(c['low']) for c in candles], dtype=float)

            trend_direction, trend_strength = self._supertrend_direction(closes, highs, lows)
            divergence_score = self._weighted_divergence(closes)
            current_price = float(closes[-1])

            logger.info(
                "[ST] symbol=%s price=%.2f trend=%s trend_strength=%.4f divergence=%.4f",
                symbol,
                current_price,
                'BUY' if trend_direction > 0 else 'SELL' if trend_direction < 0 else 'FLAT',
                trend_strength,
                divergence_score,
            )

            if not self._should_generate_signal(trend_direction, divergence_score, trend_strength):
                logger.info("[ST] symbol=%s rejected: no valid trading setup", symbol)
                continue

            signal_type, strength = self._build_signal(trend_direction, divergence_score, trend_strength)
            if strength < self.parameters.get('min_strength', 0.55):
                logger.info("[ST] symbol=%s rejected: strength %.4f below threshold %.4f", symbol, strength, self.parameters.get('min_strength', 0.55))
                continue

            quantity = float(self.parameters.get('max_position_size', 100000)) / current_price if current_price > 0 else 0.0
            logger.info(
                "[ST] FINAL signal symbol=%s action=%s price=%.2f strength=%.4f divergence=%.4f",
                symbol,
                signal_type.upper(),
                current_price,
                strength,
                divergence_score,
            )
            signal = Signal(
                symbol=symbol,
                signal_type=signal_type,
                strength=min(1.0, max(0.0, strength)),
                price=current_price,
                quantity=quantity,
                timeframe='1minute',
                metadata={
                    'strategy': self.name,
                    'trend_direction': trend_direction,
                    'trend_strength': trend_strength,
                    'divergence_score': divergence_score,
                    'atr_period': self.parameters.get('atr_period', 10),
                    'multiplier': self.parameters.get('multiplier', 3.0),
                }
            )
            signals.append(signal)
            self.signals_this_hour += 1
            self.last_signal_time = now
            self.increment_signals_generated()

        return signals

    async def on_order_filled(self, order: Any) -> None:
        logger.info("Order filled for %s: %s %s %s", order.symbol, order.side, order.quantity, order.id)

    async def on_position_updated(self, symbol: str, position: Dict[str, Any]) -> None:
        logger.info("Position updated for %s: %s", symbol, position)

    def _extract_candles(self, payload: Any) -> List[Dict[str, float]]:
        if isinstance(payload, dict):
            for key in ('candles', 'history', 'ohlc', 'bars', 'data'):
                if key in payload:
                    return self._normalize_candles(payload[key])
            if all(key in payload for key in ('open', 'high', 'low', 'close')):
                return [self._normalize_candle(payload)]
            return []

        if isinstance(payload, list):
            return self._normalize_candles(payload)

        return []

    def _normalize_candles(self, raw_candles: Sequence[Any]) -> List[Dict[str, float]]:
        candles: List[Dict[str, float]] = []
        for candle in raw_candles:
            if isinstance(candle, dict):
                candles.append(self._normalize_candle(candle))
            elif isinstance(candle, (list, tuple)) and len(candle) >= 4:
                candles.append({
                    'open': float(candle[0]),
                    'high': float(candle[1]),
                    'low': float(candle[2]),
                    'close': float(candle[3]),
                })
        return candles

    def _normalize_candle(self, candle: Dict[str, Any]) -> Dict[str, float]:
        return {
            'open': float(candle.get('open', candle.get('o', 0.0))),
            'high': float(candle.get('high', candle.get('h', 0.0))),
            'low': float(candle.get('low', candle.get('l', 0.0))),
            'close': float(candle.get('close', candle.get('c', 0.0))),
        }

    def _supertrend_direction(self, closes: np.ndarray, highs: np.ndarray, lows: np.ndarray) -> Tuple[int, float]:
        atr_period = int(self.parameters.get('atr_period', 10))
        multiplier = float(self.parameters.get('multiplier', 3.0))

        if len(closes) < atr_period + 1:
            return 0, 0.0

        atr = self._calculate_atr(highs, lows, closes, atr_period)
        if atr.size == 0:
            return 0, 0.0

        hl2 = (highs + lows) / 2.0
        basic_upper = hl2 + (multiplier * atr)
        basic_lower = hl2 - (multiplier * atr)

        final_upper = np.empty_like(basic_upper)
        final_lower = np.empty_like(basic_lower)
        final_upper[0] = basic_upper[0]
        final_lower[0] = basic_lower[0]

        for idx in range(1, len(closes)):
            final_upper[idx] = basic_upper[idx] if basic_upper[idx] < final_upper[idx - 1] or not math.isfinite(final_upper[idx - 1]) else final_upper[idx - 1]
            final_lower[idx] = basic_lower[idx] if basic_lower[idx] > final_lower[idx - 1] or not math.isfinite(final_lower[idx - 1]) else final_lower[idx - 1]

        if closes[-1] > final_upper[-1]:
            direction = 1
        elif closes[-1] < final_lower[-1]:
            direction = -1
        else:
            direction = 1 if closes[-1] >= closes[-2] else -1

        trend_strength = float(abs(closes[-1] - closes[-2]) / max(abs(closes[-2]), 1.0))
        return direction, min(1.0, trend_strength)

    def _weighted_divergence(self, closes: np.ndarray) -> float:
        rsi_period = int(self.parameters.get('rsi_period', 14))
        rsi_weight = float(self.parameters.get('rsi_weight', 0.6))
        macd_weight = float(self.parameters.get('macd_weight', 0.4))

        if len(closes) < rsi_period + 10:
            return 0.0

        rsi = self._calculate_rsi(closes, rsi_period)
        macd_line, signal_line = self._calculate_macd(closes)

        if len(rsi) == 0 or len(macd_line) == 0 or len(signal_line) == 0:
            return 0.0

        rsi_score = float((rsi[-1] - 50.0) / 50.0)
        macd_score = float((macd_line[-1] - signal_line[-1]) / max(abs(macd_line[-1]), abs(signal_line[-1]), 1.0))
        combined = ((rsi_weight * rsi_score) + (macd_weight * macd_score)) / max(rsi_weight + macd_weight, 1e-6)
        return float(np.clip(combined, -1.0, 1.0))

    def _should_generate_signal(self, trend_direction: int, divergence_score: float, trend_strength: float) -> bool:
        if trend_direction == 0:
            return False

        if abs(divergence_score) < 0.15 and trend_strength < 0.01:
            return False

        if trend_direction > 0 and divergence_score <= 0:
            return False

        if trend_direction < 0 and divergence_score >= 0:
            return False

        return True

    def _build_signal(self, trend_direction: int, divergence_score: float, trend_strength: float) -> Tuple[str, float]:
        agreement = 1.0 if np.sign(divergence_score) == np.sign(trend_direction) else 0.35
        directional_strength = abs(divergence_score) * 0.7 + trend_strength * 0.3
        strength = min(1.0, max(0.0, directional_strength * agreement))

        if trend_direction > 0:
            return 'buy', strength
        return 'sell', strength

    def _calculate_atr(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int) -> np.ndarray:
        true_ranges = np.maximum(np.maximum(highs - lows, np.abs(highs - np.roll(closes, 1))), np.abs(lows - np.roll(closes, 1)))
        true_ranges[0] = highs[0] - lows[0]

        atr = np.full_like(closes, 0.0, dtype=float)
        atr[period - 1] = np.mean(true_ranges[0:period])
        for idx in range(period, len(closes)):
            atr[idx] = ((atr[idx - 1] * (period - 1)) + true_ranges[idx]) / period
        return atr

    def _calculate_rsi(self, closes: np.ndarray, period: int) -> np.ndarray:
        if len(closes) < period + 1:
            return np.asarray([], dtype=float)

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        avg_gain = np.full(len(closes), np.nan, dtype=float)
        avg_loss = np.full(len(closes), np.nan, dtype=float)
        avg_gain[period] = np.mean(gains[:period])
        avg_loss[period] = np.mean(losses[:period])

        for idx in range(period + 1, len(closes)):
            avg_gain[idx] = ((avg_gain[idx - 1] * (period - 1)) + gains[idx - 1]) / period
            avg_loss[idx] = ((avg_loss[idx - 1] * (period - 1)) + losses[idx - 1]) / period

        rsi = np.full(len(closes), 50.0, dtype=float)
        for idx in range(period, len(closes)):
            if avg_loss[idx] == 0:
                rsi[idx] = 100.0
            else:
                rs = avg_gain[idx] / avg_loss[idx]
                rsi[idx] = 100.0 - (100.0 / (1.0 + rs))

        return rsi

    def _calculate_macd(self, closes: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        fast_ema = self._ema(closes, 12)
        slow_ema = self._ema(closes, 26)
        macd = fast_ema - slow_ema
        signal = self._ema(macd, 9)
        return macd, signal

    def _ema(self, values: np.ndarray, period: int) -> np.ndarray:
        if len(values) == 0:
            return np.asarray([], dtype=float)
        multiplier = 2.0 / (period + 1.0)
        ema = np.zeros_like(values, dtype=float)
        ema[0] = values[0]
        for idx in range(1, len(values)):
            ema[idx] = (values[idx] - ema[idx - 1]) * multiplier + ema[idx - 1]
        return ema
