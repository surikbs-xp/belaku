"""Tests for broker order reconciliation and lifecycle updates."""

import unittest
from unittest.mock import Mock, patch

from kiteconnect.exceptions import KiteException

from core.engine import TradingEngine
from core.strategy.base import Signal
from core.types import Order, OrderStatus
from exchanges.zerodha import ZerodhaAPI, ZerodhaOrderRequest


class FakeExchange:
    name = 'ZERODHA'

    def __init__(self, broker_orders, cancel_succeeds=True, submission_id='broker-submitted-1', positions=None):
        self.broker_orders = broker_orders
        self.canceled_order_ids = []
        self.cancel_succeeds = cancel_succeeds
        self.submission_id = submission_id
        self.positions = positions or {}
        self.submitted_orders = []

    async def get_order_history(self):
        return self.broker_orders

    async def cancel_order(self, order_id):
        self.canceled_order_ids.append(order_id)
        if self.cancel_succeeds:
            for order in self.broker_orders:
                if order.get('order_id') == order_id:
                    order['status'] = 'CANCELLED'
        return self.cancel_succeeds

    async def submit_order(self, order):
        order_id = self.submission_id
        if self.submitted_orders:
            order_id = f'{self.submission_id}-{len(self.submitted_orders) + 1}'
        self.submitted_orders.append(order)
        self.broker_orders.append({
            'order_id': order_id,
            'tradingsymbol': order.symbol,
            'transaction_type': order.side.upper(),
            'order_type': order.order_type.upper(),
            'quantity': order.quantity,
            'filled_quantity': 0,
            'status': 'OPEN',
        })
        return order_id

    async def get_positions(self):
        return self.positions

    def order_symbol_for_signal(self, symbol):
        return symbol

    async def prepare_order(self, order):
        return None

    def risk_limit_for_order(self, order):
        if order.symbol.endswith('FUT') and order.price:
            return order.quantity * order.price * 1.1
        return None


class OrderManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_submission_tracks_broker_order_id(self):
        exchange = FakeExchange([])
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange
        order = Order(
            id='local-entry-1',
            symbol='RELIANCE',
            side='buy',
            order_type='market',
            quantity=1,
            price=100,
            exchange=exchange.name,
        )

        self.assertTrue(await engine.submit_order(order))
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.assertEqual(order.order_id, 'broker-submitted-1')
        self.assertEqual(engine.get_open_orders(), [order])

    async def test_submission_without_broker_id_is_rejected(self):
        exchange = FakeExchange([], submission_id='')
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange
        order = Order(
            id='local-entry-2',
            symbol='RELIANCE',
            side='buy',
            order_type='market',
            quantity=1,
            price=100,
            exchange=exchange.name,
        )

        self.assertFalse(await engine.submit_order(order))
        self.assertEqual(order.status, OrderStatus.REJECTED)
        self.assertIsNone(engine.get_order(order.id))

    async def test_reconcile_restores_open_orders_and_updates(self):
        exchange = FakeExchange([
            {
                'order_id': 'broker-open-1',
                'tradingsymbol': 'NIFTY26OCTFUT',
                'transaction_type': 'BUY',
                'order_type': 'MARKET',
                'quantity': 50,
                'filled_quantity': 10,
                'average_price': 23125.5,
                'status': 'OPEN',
            },
            {
                'order_id': 'broker-filled-1',
                'tradingsymbol': 'RELIANCE',
                'transaction_type': 'SELL',
                'order_type': 'LIMIT',
                'quantity': 2,
                'filled_quantity': 2,
                'average_price': 1200,
                'status': 'COMPLETE',
            },
        ])
        engine = TradingEngine({})

        await engine._reconcile_exchange_orders(exchange)
        await engine._reconcile_exchange_orders(exchange)

        open_orders = engine.get_open_orders()
        self.assertEqual(len(engine.order_history), 2)
        self.assertEqual(len(open_orders), 1)
        self.assertEqual(open_orders[0].order_id, 'broker-open-1')
        self.assertEqual(open_orders[0].status, OrderStatus.PARTIALLY_FILLED)
        self.assertEqual(open_orders[0].filled_quantity, 10)
        self.assertEqual(open_orders[0].avg_fill_price, 23125.5)
        self.assertEqual(engine.get_filled_orders()[0].order_id, 'broker-filled-1')

        await engine._handle_order_update({
            'order_id': 'broker-open-1',
            'status': 'COMPLETE',
            'filled_quantity': 50,
            'average_price': 23130,
        })
        self.assertEqual(open_orders[0].status, OrderStatus.FILLED)
        self.assertEqual(open_orders[0].filled_quantity, 50)

    async def test_reconciled_order_can_be_canceled_by_local_id(self):
        exchange = FakeExchange([{
            'order_id': 'broker-open-2',
            'tradingsymbol': 'RELIANCE',
            'transaction_type': 'BUY',
            'quantity': 1,
            'status': 'OPEN',
        }])
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange

        await engine._reconcile_exchange_orders(exchange)
        local_order_id = engine.get_open_orders()[0].id

        self.assertTrue(await engine.cancel_order(local_order_id))
        self.assertEqual(exchange.canceled_order_ids, ['broker-open-2'])
        self.assertEqual(engine.get_order(local_order_id).status, OrderStatus.SUBMITTED)
        await engine._reconcile_exchange_orders(exchange)
        self.assertEqual(engine.get_order(local_order_id).status, OrderStatus.CANCELED)

    async def test_failed_broker_cancel_keeps_order_open(self):
        exchange = FakeExchange([{
            'order_id': 'broker-open-3',
            'tradingsymbol': 'RELIANCE',
            'transaction_type': 'BUY',
            'quantity': 1,
            'status': 'OPEN',
        }], cancel_succeeds=False)
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange

        await engine._reconcile_exchange_orders(exchange)
        order = engine.get_open_orders()[0]

        self.assertFalse(await engine.cancel_order(order.id))
        self.assertEqual(order.status, OrderStatus.SUBMITTED)

    async def test_reverse_signal_enters_after_opposite_position_closes(self):
        exchange = FakeExchange(
            [],
            positions={
                'ZERODHA:RELIANCE': {
                    'quantity': 1,
                    'average_price': 100,
                    'last_price': 99,
                },
            },
        )
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange
        await engine._update_positions()

        await engine._process_signals([
            Signal(
                symbol='RELIANCE',
                signal_type='sell',
                strength=0.8,
                price=99,
                quantity=1,
                timeframe='3m',
            )
        ])
        await engine._update_positions()
        await engine._process_pending_reversals()

        self.assertEqual(len(exchange.submitted_orders), 1)
        self.assertEqual(exchange.submitted_orders[0].side, 'sell')
        self.assertEqual(exchange.submitted_orders[0].timeframe, '3m')
        self.assertTrue(hasattr(engine, '_process_pending_reversals'))
        exchange.positions['ZERODHA:RELIANCE']['quantity'] = 0
        exchange.broker_orders[0].update(status='COMPLETE', filled_quantity=1, average_price=99)
        await engine._reconcile_exchange_orders(exchange)
        await engine._update_positions()
        await engine._process_pending_reversals()

        self.assertEqual(len(exchange.submitted_orders), 2)
        self.assertEqual(exchange.submitted_orders[1].side, 'sell')
        self.assertEqual(exchange.submitted_orders[1].symbol, 'RELIANCE')
        self.assertEqual(exchange.submitted_orders[1].timeframe, '3m')

    async def test_restart_keeps_same_side_position_and_reverses_on_opposite_signal(self):
        exchange = FakeExchange(
            [{
                'order_id': 'restored-buy-1',
                'tradingsymbol': 'RELIANCE',
                'transaction_type': 'BUY',
                'order_type': 'MARKET',
                'quantity': 1,
                'filled_quantity': 1,
                'average_price': 100,
                'status': 'COMPLETE',
            }],
            positions={
                'ZERODHA:RELIANCE': {
                    'quantity': 1,
                    'average_price': 100,
                    'last_price': 101,
                },
            },
        )
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange

        await engine._reconcile_exchange_orders(exchange)
        await engine._update_positions()
        with self.assertLogs('core.engine', level='INFO') as same_side_logs:
            await engine._process_signals([Signal(
                symbol='RELIANCE',
                signal_type='buy',
                strength=0.8,
                price=101,
                quantity=2,
            )])

        self.assertEqual(exchange.submitted_orders, [])
        self.assertIn('NO_CHANGE symbol=RELIANCE', '\n'.join(same_side_logs.output))

        with self.assertLogs('core.engine', level='INFO') as reversal_logs:
            await engine._process_signals([Signal(
                symbol='RELIANCE',
                signal_type='sell',
                strength=0.8,
                price=99,
                quantity=2,
                metadata={
                    'strategy': 'SuperTrendWeightedDivergence',
                    'strategy_conditions': 'SuperTrend=SELL;PSAR=SELL;agree=true',
                    'indicator_close': 98.5,
                    'trend_direction': -1,
                    'psar_direction': -1,
                    'signal_candle_timestamp': '2026-10-19T13:42:00',
                    'trigger_candle_timestamp': '2026-10-19T13:45:00',
                },
            )])
            await engine._process_pending_reversals()

        self.assertEqual(len(exchange.submitted_orders), 1)
        self.assertEqual(exchange.submitted_orders[0].side, 'sell')
        self.assertEqual(exchange.submitted_orders[0].quantity, 1)
        reversal_output = '\n'.join(reversal_logs.output)
        self.assertIn('Processing strategy=SuperTrendWeightedDivergence', reversal_output)
        self.assertIn('conditions=SuperTrend=SELL;PSAR=SELL;agree=true', reversal_output)
        self.assertIn('[REVERSAL] QUEUED', reversal_output)
        self.assertIn('[REVERSAL] CLOSE_REQUEST', reversal_output)
        self.assertIn('[ORDER] REQUEST', reversal_output)
        self.assertIn('[REVERSAL] CLOSE_SUBMITTED', reversal_output)

        exchange.positions['ZERODHA:RELIANCE']['quantity'] = 0
        exchange.broker_orders[-1].update(
            status='COMPLETE',
            filled_quantity=1,
            average_price=99,
        )
        await engine._reconcile_exchange_orders(exchange)
        await engine._update_positions()
        with self.assertLogs('core.engine', level='INFO') as entry_logs:
            await engine._process_pending_reversals()

        self.assertEqual(len(exchange.submitted_orders), 2)
        self.assertEqual(exchange.submitted_orders[1].side, 'sell')
        self.assertEqual(exchange.submitted_orders[1].quantity, 2)
        self.assertIn('[REVERSAL] ENTRY', '\n'.join(entry_logs.output))

    async def test_signal_is_submitted_once_and_retried_once_after_rejection(self):
        exchange = FakeExchange([])
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange
        signal = Signal(
            symbol='RELIANCE',
            signal_type='buy',
            strength=0.8,
            price=100,
            quantity=1,
            metadata={'signal_id': 'strategy:RELIANCE:3m:candle-1'},
        )

        await engine._process_signals([signal])
        order = next(iter(engine.orders.values()))
        first_broker_id = order.order_id
        await engine._handle_order_update({
            'order_id': first_broker_id,
            'status': 'REJECTED',
            'filled_quantity': 0,
        })

        self.assertEqual(len(exchange.submitted_orders), 2)
        retry_order = next(
            candidate for candidate in engine.orders.values()
            if candidate.retry_count == 1
        )
        self.assertEqual(retry_order.retry_count, 1)
        second_broker_id = retry_order.order_id
        self.assertNotEqual(first_broker_id, second_broker_id)

        exchange.broker_orders[0]['status'] = 'REJECTED'
        exchange.broker_orders[1]['status'] = 'REJECTED'
        await engine._handle_order_update({
            'order_id': second_broker_id,
            'status': 'REJECTED',
            'filled_quantity': 0,
        })
        with self.assertLogs('core.engine', level='INFO') as duplicate_logs:
            await engine._process_signals([signal])

        self.assertEqual(len(exchange.submitted_orders), 2)
        self.assertEqual(retry_order.retry_count, 1)
        self.assertIn('Duplicate signal ignored', '\n'.join(duplicate_logs.output))

    async def test_immediate_kite_rejection_retries_once(self):
        class RejectOnceExchange(FakeExchange):
            def __init__(self):
                super().__init__([])
                self.submission_attempts = 0

            async def submit_order(self, order):
                self.submission_attempts += 1
                if self.submission_attempts == 1:
                    raise RuntimeError('Kite order rejected (KiteException)')
                return await super().submit_order(order)

        exchange = RejectOnceExchange()
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange
        signal = Signal(
            symbol='RELIANCE',
            signal_type='buy',
            strength=0.8,
            price=100,
            quantity=1,
            metadata={'signal_id': 'strategy:RELIANCE:3m:candle-2'},
        )

        await engine._process_signals([signal])

        order = next(iter(engine.orders.values()))
        self.assertEqual(exchange.submission_attempts, 2)
        self.assertEqual(len(exchange.submitted_orders), 1)
        self.assertEqual(order.status, OrderStatus.SUBMITTED)
        self.assertEqual(order.retry_count, 1)

class ZerodhaOrderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_logs_kite_message_without_credentials(self):
        kite_error = KiteException('Order placement is not permitted', code=403)
        api = ZerodhaAPI('test-api-key', 'test-api-secret', 'test-access-token')

        with patch.object(api.kite, 'place_order', side_effect=kite_error):
            with self.assertLogs('exchanges.zerodha', level='ERROR') as logs:
                with self.assertRaisesRegex(RuntimeError, 'KiteException'):
                    await api.place_order(ZerodhaOrderRequest(
                        symbol='NIFTY26OCTFUT',
                        transaction_type='BUY',
                        quantity=65,
                    ))

        self.assertIn('KiteException', logs.output[0])
        self.assertIn('Order placement is not permitted', logs.output[0])
        self.assertNotIn('test-access-token', logs.output[0])

    async def test_opposite_signal_cancels_working_order_before_entry(self):
        exchange = FakeExchange([{
            'order_id': 'working-buy-1',
            'tradingsymbol': 'NIFTY26OCTFUT',
            'transaction_type': 'BUY',
            'order_type': 'MARKET',
            'quantity': 50,
            'status': 'OPEN',
        }], positions={
            'ZERODHA:NIFTY26OCTFUT': {
                'quantity': 0,
                'average_price': 0,
                'last_price': 23000,
            },
        })
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange

        await engine._process_signals([
            Signal(
                symbol='NIFTY26OCTFUT',
                signal_type='sell',
                strength=0.8,
                price=23000,
                quantity=50,
            )
        ])

        self.assertEqual(exchange.canceled_order_ids, ['working-buy-1'])
        self.assertEqual(exchange.submitted_orders, [])
        await engine._update_positions()
        await engine._process_pending_reversals()

        self.assertEqual(len(exchange.submitted_orders), 1)
        self.assertEqual(exchange.submitted_orders[0].side, 'sell')

    async def test_partial_buy_is_closed_before_reverse_sell_entry(self):
        exchange = FakeExchange(
            [{
                'order_id': 'partial-buy-1',
                'tradingsymbol': 'NIFTY26OCTFUT',
                'transaction_type': 'BUY',
                'order_type': 'MARKET',
                'quantity': 50,
                'filled_quantity': 10,
                'status': 'OPEN',
            }],
            positions={
                'ZERODHA:NIFTY26OCTFUT': {
                    'quantity': 10,
                    'average_price': 100,
                    'last_price': 99,
                },
            },
        )
        engine = TradingEngine({})
        engine.exchanges[exchange.name] = exchange

        await engine._process_signals([
            Signal(
                symbol='NIFTY26OCTFUT',
                signal_type='sell',
                strength=0.8,
                price=99,
                quantity=50,
            )
        ])
        await engine._update_positions()
        await engine._process_pending_reversals()

        self.assertEqual(exchange.canceled_order_ids, ['partial-buy-1'])
        self.assertEqual(len(exchange.submitted_orders), 1)
        self.assertEqual(exchange.submitted_orders[0].side, 'sell')
        self.assertEqual(exchange.submitted_orders[0].quantity, 10)

        exchange.positions['ZERODHA:NIFTY26OCTFUT']['quantity'] = 0
        exchange.broker_orders[-1].update(
            status='COMPLETE',
            filled_quantity=10,
            average_price=99,
        )
        await engine._reconcile_exchange_orders(exchange)
        await engine._update_positions()
        await engine._process_pending_reversals()

        self.assertEqual(len(exchange.submitted_orders), 2)
        self.assertEqual(exchange.submitted_orders[1].side, 'sell')
        self.assertEqual(exchange.submitted_orders[1].quantity, 50)


if __name__ == '__main__':
    unittest.main()