"""No-network tests for the official Kite Connect adapter."""

import asyncio
import unittest
from datetime import date, datetime
from unittest.mock import Mock, patch

from exchanges.zerodha import ZerodhaAPI, ZerodhaOrderRequest
from get_token import exchange_request_token


class KiteConnectAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_place_order_uses_sdk_and_passes_order_tag(self):
        api = ZerodhaAPI('api-key', 'api-secret', 'access-token')
        with patch.object(api.kite, 'place_order', return_value='broker-order-1') as place_order:
            result = await api.place_order(ZerodhaOrderRequest(
                symbol='NIFTY26OCTFUT',
                exchange='NFO',
                transaction_type='BUY',
                quantity=65,
                product='NRML',
                order_type='MARKET',
                tag='3m',
            ))

        self.assertEqual(result, {'order_id': 'broker-order-1'})
        self.assertEqual(place_order.call_args.kwargs['variety'], api.kite.VARIETY_REGULAR)
        self.assertEqual(place_order.call_args.kwargs['tradingsymbol'], 'NIFTY26OCTFUT')
        self.assertEqual(place_order.call_args.kwargs['quantity'], 65)
        self.assertEqual(place_order.call_args.kwargs['tag'], '3m')
        self.assertEqual(place_order.call_args.kwargs['market_protection'], -1)

    async def test_limit_order_does_not_set_market_protection(self):
        api = ZerodhaAPI('api-key', 'api-secret', 'access-token')
        with patch.object(api.kite, 'place_order', return_value='broker-order-2') as place_order:
            await api.place_order(ZerodhaOrderRequest(
                symbol='RELIANCE',
                transaction_type='BUY',
                quantity=1,
                order_type='LIMIT',
                price=2500,
            ))

        self.assertIsNone(place_order.call_args.kwargs['market_protection'])

    async def test_instruments_convert_sdk_expiry_date(self):
        api = ZerodhaAPI('api-key', 'api-secret', 'access-token')
        sdk_instrument = {
            'tradingsymbol': 'NIFTY26OCTFUT',
            'name': 'NIFTY',
            'exchange': 'NFO',
            'instrument_token': 123,
            'instrument_type': 'FUT',
            'lot_size': 65,
            'tick_size': 0.05,
            'expiry': date(2026, 10, 29),
            'strike': 0.0,
        }
        with patch.object(api.kite, 'instruments', return_value=[
            sdk_instrument,
            {**sdk_instrument, 'tradingsymbol': 'NIFTY-INDEX', 'expiry': ''},
        ]):
            instruments = await api.get_instruments('NFO')

        self.assertEqual(instruments[0].expiry_date, datetime(2026, 10, 29))
        self.assertEqual(instruments[0].instrument_token, 123)
        self.assertIsNone(instruments[1].expiry_date)

    async def test_historical_data_maps_short_interval_to_kite_interval(self):
        api = ZerodhaAPI('api-key', 'api-secret', 'access-token')
        start = datetime(2026, 10, 16, 9, 15)
        end = datetime(2026, 10, 16, 10, 15)
        with patch.object(api.kite, 'historical_data', return_value=[]) as historical_data:
            await api.get_historical_data(123, start, end, '3m')

        historical_data.assert_called_once_with(123, start, end, '3minute')

    async def test_ticker_callback_normalizes_sdk_tick(self):
        api = ZerodhaAPI('api-key', 'api-secret', 'access-token')
        api._loop = asyncio.get_running_loop()
        received = asyncio.Event()
        ticks = []

        async def handler(tick):
            ticks.append(tick)
            received.set()

        api.add_tick_handler(handler)
        exchange_timestamp = datetime(2026, 10, 16, 10, 30)
        api._on_ticker_ticks(Mock(), [{
            'instrument_token': 123,
            'last_price': 23123.5,
            'volume_traded': 900,
            'exchange_timestamp': exchange_timestamp,
            'ohlc': {'open': 23000, 'high': 23200, 'low': 22900, 'close': 23050},
        }])
        await asyncio.wait_for(received.wait(), timeout=1)

        self.assertEqual(ticks[0]['ltp'], 23123.5)
        self.assertEqual(ticks[0]['volume'], 900)
        self.assertEqual(ticks[0]['timestamp'], exchange_timestamp)
        self.assertEqual(ticks[0]['open'], 23000)

    async def test_subscribe_uses_ticker_mode_on_reactor_thread(self):
        api = ZerodhaAPI('api-key', 'api-secret', 'access-token')
        ticker = Mock()
        ticker.MODE_LTP = 'ltp'
        api.ticker = ticker
        api._ticker_connected = True

        with patch('exchanges.zerodha.reactor.callFromThread') as call_from_thread:
            await api.subscribe_market_data(123, mode='ltp')

        self.assertEqual(api._subscription_modes[123], 'ltp')
        call_from_thread.assert_called_once_with(api._subscribe_ticker, ticker, 123, 'ltp')

    def test_oauth_session_uses_sdk_generate_session(self):
        with patch('get_token.KiteConnect') as kite_connect:
            kite_connect.return_value.generate_session.return_value = {
                'access_token': 'new-access-token',
            }

            access_token = exchange_request_token('api-key', 'api-secret', 'request-token')

        self.assertEqual(access_token, 'new-access-token')
        kite_connect.return_value.generate_session.assert_called_once_with(
            'request-token',
            api_secret='api-secret',
        )


if __name__ == '__main__':
    unittest.main()