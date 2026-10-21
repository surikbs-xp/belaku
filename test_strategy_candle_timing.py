"""Tests for closed-candle strategy evaluation and next-candle entries."""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from core.data.handler import DataHandler
from core.strategy.supertrend_divergence import SuperTrendWeightedDivergenceStrategy


class StrategyCandleTimingTests(unittest.IsolatedAsyncioTestCase):
    def test_default_supertrend_parameters(self):
        strategy = SuperTrendWeightedDivergenceStrategy()

        self.assertEqual(strategy.parameters['timeframe'], '15m')
        self.assertEqual(strategy.parameters['atr_period'], 10)
        self.assertEqual(strategy.parameters['multiplier'], 3.0)
        self.assertEqual(strategy.parameters['pivot_length'], 2)
        self.assertEqual(strategy.parameters['divergence_sensitivity'], 0.3)
        self.assertNotIn('rsi_period', strategy.parameters)
        self.assertNotIn('rsi_weight', strategy.parameters)

    def test_divergence_score_uses_only_macd(self):
        strategy = SuperTrendWeightedDivergenceStrategy()
        with patch.object(strategy, '_calculate_macd', return_value=([0.8], [0.5])) as macd:
            score = strategy._macd_divergence_score([100.0])

        macd.assert_called_once()
        self.assertAlmostEqual(score, 0.3)
        self.assertFalse(hasattr(strategy, '_calculate_rsi'))

    def setUp(self):
        self.strategy = SuperTrendWeightedDivergenceStrategy({
            'timeframe': '3m',
            'atr_period': 2,
            'cooldown_period': 0,
            'max_tick_age_seconds': 30,
        })
        self.now = datetime.now()
        minute = self.now.minute - self.now.minute % 3
        self.trigger_start = self.now.replace(
            minute=minute,
            second=0,
            microsecond=0,
        )

    def _market_data(self, candle_count=8, include_timestamps=True):
        candles = []
        for index in range(candle_count):
            candle = {
                'open': 100.0 + index,
                'high': 101.0 + index,
                'low': 99.0 + index,
                'close': 100.5 + index,
            }
            if include_timestamps:
                candle['timestamp'] = self.trigger_start - timedelta(
                    minutes=3 * (candle_count - index - 1)
                )
            candles.append(candle)

        candles[-1].update({
            'open': 999.0,
            'high': 1000.0,
            'low': 998.0,
            'close': 999.5,
        })
        return {
            'GIFT NIFTY': {
                'candles': candles,
                'last_tick_at': self.now,
                'ltp': 1001.0,
            }
        }

    async def test_uses_closed_candle_and_enters_on_following_candle_once(self):
        market_data = self._market_data()
        with (
            patch.object(self.strategy, '_supertrend_direction', return_value=(1, 0.8)) as trend,
            patch.object(self.strategy, '_parabolic_sar_direction', return_value=(1, 90.0)),
            patch.object(self.strategy, '_macd_divergence_score', return_value=0.2),
            self.assertLogs('core.strategy.supertrend_divergence', level='INFO') as strategy_logs,
        ):
            signals = await self.strategy.analyze(market_data)
            repeated_signals = await self.strategy.analyze(market_data)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_type, 'buy')
        self.assertEqual(signals[0].price, 1001.0)
        self.assertEqual(len(trend.call_args.args[0]), 7)
        self.assertEqual(trend.call_args.args[0][-1], 106.5)
        self.assertEqual(
            signals[0].metadata['trigger_candle_timestamp'],
            self.trigger_start.isoformat(),
        )
        self.assertEqual(signals[0].metadata['indicator_close'], 106.5)
        self.assertEqual(signals[0].metadata['divergence_sensitivity'], 0.3)
        self.assertIn('strategy=SuperTrendWeightedDivergence', '\n'.join(strategy_logs.output))
        self.assertIn(
            'conditions=SuperTrend=BUY;PSAR=BUY;agree=true;feed_fresh=true;cooldown_clear=true;hourly_limit_clear=true',
            '\n'.join(strategy_logs.output),
        )
        self.assertEqual(repeated_signals, [])

    async def test_15m_strategy_uses_closed_bars_and_triggers_on_next_bar(self):
        now = datetime.now()
        trigger_start = now.replace(
            minute=now.minute - now.minute % 15,
            second=0,
            microsecond=0,
        )
        strategy = SuperTrendWeightedDivergenceStrategy({
            'timeframe': '15m',
            'cooldown_period': 0,
        })
        candles = []
        for index in range(20):
            candle = {
                'timestamp': trigger_start - timedelta(minutes=15 * (19 - index)),
                'open': 100.0 + index,
                'high': 101.0 + index,
                'low': 99.0 + index,
                'close': 100.5 + index,
            }
            candles.append(candle)
        candles[-1].update({
            'open': 999.0,
            'high': 1000.0,
            'low': 998.0,
            'close': 999.5,
        })
        market_data = {
            'GIFT NIFTY': {
                'candles': candles,
                'last_tick_at': now,
                'ltp': 1001.0,
            }
        }

        with (
            patch.object(strategy, '_supertrend_direction', return_value=(1, 0.8)) as trend,
            patch.object(strategy, '_parabolic_sar_direction', return_value=(1, 90.0)),
            patch.object(strategy, '_macd_divergence_score', return_value=0.2),
        ):
            signals = await strategy.analyze(market_data)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].timeframe, '15m')
        self.assertEqual(signals[0].price, 1001.0)
        self.assertEqual(len(trend.call_args.args[0]), 19)
        self.assertEqual(trend.call_args.args[0][-1], 118.5)
        self.assertEqual(
            signals[0].metadata['signal_candle_timestamp'],
            (trigger_start - timedelta(minutes=15)).isoformat(),
        )
        self.assertEqual(
            signals[0].metadata['trigger_candle_timestamp'],
            trigger_start.isoformat(),
        )

    async def test_does_not_signal_without_a_following_trigger_candle(self):
        market_data = self._market_data(candle_count=7)
        with (
            patch.object(self.strategy, '_supertrend_direction', return_value=(1, 0.8)),
            patch.object(self.strategy, '_parabolic_sar_direction', return_value=(1, 90.0)),
            patch.object(self.strategy, '_macd_divergence_score', return_value=0.2),
        ):
            signals = await self.strategy.analyze(market_data)

        self.assertEqual(signals, [])

    async def test_does_not_signal_when_candle_timestamps_are_missing(self):
        market_data = self._market_data(include_timestamps=False)
        with (
            patch.object(self.strategy, '_supertrend_direction', return_value=(1, 0.8)),
            patch.object(self.strategy, '_parabolic_sar_direction', return_value=(1, 90.0)),
            patch.object(self.strategy, '_macd_divergence_score', return_value=0.2),
        ):
            signals = await self.strategy.analyze(market_data)

        self.assertEqual(signals, [])

    async def test_data_snapshot_preserves_candle_timestamps(self):
        handler = DataHandler({})
        await handler.add_data_stream('GIFT NIFTY', 'NSE')
        await handler._process_new_data({
            'symbol': 'GIFT NIFTY',
            'exchange': 'NSE',
            'timestamp': self.trigger_start,
            'open_price': 100.0,
            'high_price': 101.0,
            'low_price': 99.0,
            'close_price': 100.5,
            'volume': 10,
            'timeframe': '3m',
        })

        snapshot = await handler.get_latest_snapshot()

        self.assertEqual(
            snapshot['GIFT NIFTY']['candles'][0]['timestamp'],
            self.trigger_start,
        )


if __name__ == '__main__':
    unittest.main()