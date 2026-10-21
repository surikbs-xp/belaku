"""
Zerodha exchange integration for Belaku TradeBot.

This module provides integration with Zerodha, one of India's largest
broking companies, supporting trading in Indian equities, derivatives,
commodities, and currencies.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum

from kiteconnect import KiteConnect, KiteTicker
from kiteconnect.exceptions import KiteException
from twisted.internet import reactor

from core.types import Order, OrderStatus, PositionSide
from exchanges.base import BaseExchange

logger = logging.getLogger(__name__)

# Every Kite REST call gets a hard deadline so a stalled socket cannot freeze the event loop.
REQUEST_TIMEOUT = 15


class ZerodhaOrderStatus(Enum):
    """Zerodha order status enumeration."""
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    OPEN = "open"
    TRIGGERED = "triggered"
    EXPIRED = "expired"


class ZerodhaProductType(Enum):
    """Kite margin products."""
    CNC = "CNC"    # Cash and Carry, equity delivery
    NRML = "NRML"  # Normal, carry-forward futures and options
    MIS = "MIS"    # Margin Intraday Squareoff


class ZerodhaOrderType(Enum):
    """Zerodha order type enumeration."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOPLOSS"
    STOP_LIMIT = "STOPLIMIT"
    ICEBERG = "ICEBERG"


class ZerodhaTransactionType(Enum):
    """Zerodha transaction type enumeration."""
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class ZerodhaToken:
    """Zerodha token/instrument data structure."""
    symbol: str
    name: str
    exchange: str
    instrument_token: int
    instrument_type: str
    lot_size: int
    tick_size: float
    price_precision: int
    expiry_date: Optional[datetime] = None
    strike_price: Optional[float] = None
    option_type: Optional[str] = None


@dataclass
class ZerodhaPosition:
    """Zerodha position data structure."""
    symbol: str
    quantity: float
    average_price: float
    last_price: float
    pnl: float
    pnl_percent: float
    product_type: str
    exchange: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class ZerodhaOrderRequest:
    """Zerodha order request data structure."""
    symbol: str
    transaction_type: str
    quantity: int
    exchange: str = "NSE"
    price: Optional[float] = None
    product: str = "CNC"
    order_type: str = "MARKET"
    validity: str = "DAY"
    disclosed_quantity: int = 0
    trigger_price: Optional[float] = None
    tag: Optional[str] = None


class ZerodhaAPI:
    """
    Zerodha API client for trading operations.
    
    This class handles all API interactions with Zerodha's REST API
    and WebSocket API for real-time market data and order updates.
    """

    def __init__(self, api_key: str, api_secret: str, access_token: Optional[str] = None):
        """
        Initialize Zerodha API client.
        
        Args:
            api_key: Zerodha API key
            api_secret: Zerodha API secret
            access_token: Zerodha access token (optional, will be obtained if not provided)
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.kite = KiteConnect(
            api_key=api_key,
            access_token=access_token,
            timeout=REQUEST_TIMEOUT,
        )
        
        # Event handlers
        self.order_update_handlers: List[Callable] = []
        self.trade_update_handlers: List[Callable] = []
        self.position_update_handlers: List[Callable] = []
        self.market_data_handlers: Dict[str, List[Callable]] = {}
        self.tick_handlers: List[Callable] = []
        
        # WebSocket connection
        self.ticker: Optional[KiteTicker] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._websocket_ready: Optional[asyncio.Event] = None
        self._ticker_connected = False
        self._subscription_modes: Dict[int, str] = {}

    async def connect(self) -> None:
        """
        Connect to Zerodha API.
        """
        try:
            if not self.access_token:
                raise ValueError("Kite access token is required; start main.py to authorize")
            
            # Test connection
            await asyncio.to_thread(self.kite.profile)
            
            logger.info("Connected to Zerodha API")
            
        except Exception as e:
            logger.error(f"Failed to connect to Zerodha API: {str(e)}")
            raise

    async def disconnect(self) -> None:
        """
        Disconnect from Zerodha API.
        """
        await self.stop_websocket()
        logger.info("Disconnected from Zerodha API")

    async def get_instruments(self, exchange: str = "NSE") -> List[ZerodhaToken]:
        """
        Get list of available instruments from Zerodha.
        
        Args:
            exchange: Exchange name (NSE, BSE, NFO, BFO, CDS, MCX)
            
        Returns:
            List of ZerodhaToken objects
        """
        try:
            instrument_rows = await asyncio.to_thread(self.kite.instruments, exchange)
            instruments = []
            for instrument_data in instrument_rows:
                tick_size = float(instrument_data['tick_size'])
                tick_text = f"{tick_size:.10f}".rstrip('0').rstrip('.')
                price_precision = len(tick_text.partition('.')[2])
                expiry = instrument_data.get('expiry')
                if not expiry:
                    expiry = None
                elif isinstance(expiry, date) and not isinstance(expiry, datetime):
                    expiry = datetime.combine(expiry, datetime.min.time())
                elif isinstance(expiry, str) and expiry:
                    expiry = datetime.fromisoformat(expiry)
                instrument_type = instrument_data['instrument_type']
                strike = instrument_data['strike']
                instruments.append(ZerodhaToken(
                    symbol=instrument_data['tradingsymbol'],
                    name=instrument_data['name'],
                    exchange=instrument_data['exchange'],
                    instrument_token=int(instrument_data['instrument_token']),
                    instrument_type=instrument_type,
                    lot_size=int(instrument_data['lot_size']),
                    tick_size=tick_size,
                    price_precision=price_precision,
                    expiry_date=expiry,
                    strike_price=float(strike) if strike else None,
                    option_type=instrument_type if instrument_type in {'CE', 'PE'} else None,
                ))

            logger.info("Retrieved %d instruments from %s", len(instruments), exchange)
            return instruments
        except Exception as error:
            logger.error("Failed to get instruments: %s", error)
            raise

    async def get_quote(self, symbol: str, exchange: str = "NSE") -> Dict[str, Any]:
        """
        Get current quote for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            
        Returns:
            Dictionary containing quote data
        """
        try:
            instrument_key = f"{exchange}:{symbol}"
            quotes = await asyncio.to_thread(self.kite.quote, instrument_key)
            return quotes.get(instrument_key, {})
            
        except Exception as e:
            logger.error(f"Failed to get quote for {symbol}: {str(e)}")
            raise

    async def place_order(self, order_request: ZerodhaOrderRequest) -> Dict[str, Any]:
        """
        Place an order with Zerodha.
        
        Args:
            order_request: Order request data
            
        Returns:
            Dictionary containing order response
        """
        try:
            order_id = await asyncio.to_thread(
                self.kite.place_order,
                variety=self.kite.VARIETY_REGULAR,
                exchange=order_request.exchange,
                tradingsymbol=order_request.symbol,
                transaction_type=order_request.transaction_type,
                quantity=int(order_request.quantity),
                product=order_request.product,
                order_type=order_request.order_type,
                price=order_request.price,
                validity=order_request.validity,
                disclosed_quantity=order_request.disclosed_quantity,
                trigger_price=order_request.trigger_price,
                tag=order_request.tag,
                market_protection=(
                    -1 if order_request.order_type.upper() == 'MARKET' else None
                ),
            )
            logger.info(f"Order placed: {order_id or 'unknown'}")
            return {'order_id': order_id}
        except KiteException as error:
            logger.error("Failed to place order with Kite: %s: %s", type(error).__name__, error)
            raise RuntimeError(
                f"Kite order rejected ({type(error).__name__}): {error}"
            ) from error
        except Exception as error:
            logger.error("Failed to place order: %s", error)
            raise

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an existing order.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            Dictionary containing cancellation response
        """
        try:
            canceled_order_id = await asyncio.to_thread(
                self.kite.cancel_order,
                variety=self.kite.VARIETY_REGULAR,
                order_id=order_id,
            )
            logger.info(f"Order canceled: {order_id}")
            return {'order_id': canceled_order_id}
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {str(e)}")
            raise

    async def get_positions(self) -> List[ZerodhaPosition]:
        """
        Get current positions from Zerodha.
        
        Returns:
            List of ZerodhaPosition objects
        """
        try:
            response = await asyncio.to_thread(self.kite.positions)
            positions = []
            for position_data in response.get('net', []):
                average_price = float(position_data.get('average_price') or 0)
                last_price = float(position_data.get('last_price') or 0)
                pnl_percent = (
                    (last_price - average_price) / average_price * 100
                    if average_price else 0.0
                )
                positions.append(ZerodhaPosition(
                    symbol=position_data['tradingsymbol'],
                    quantity=float(position_data['quantity']),
                    average_price=average_price,
                    last_price=last_price,
                    pnl=float(position_data.get('pnl') or 0),
                    pnl_percent=pnl_percent,
                    product_type=position_data.get('product', ''),
                    exchange=position_data.get('exchange', '')
                ))
            
            logger.debug("Retrieved %d positions", len(positions))
            return positions
            
        except Exception as e:
            logger.error(f"Failed to get positions: {str(e)}")
            raise

    async def get_order_history(self) -> List[Dict[str, Any]]:
        """
        Get today's orders from Zerodha.
        
        Kite only keeps the order book for the current trading day.
            
        Returns:
            List of order history entries
        """
        try:
            orders = await asyncio.to_thread(self.kite.orders)
            logger.info(f"Retrieved {len(orders)} orders from history")
            return orders
            
        except Exception as e:
            logger.error(f"Failed to get order history: {str(e)}")
            raise

    async def get_historical_data(
        self,
        instrument_token: int,
        start_time: datetime,
        end_time: datetime,
        timeframe: str,
    ) -> List[Dict[str, Any]]:
        """Retrieve and normalize candles using KiteConnect's historical API."""
        interval_aliases = {
            '1m': 'minute', '1minute': 'minute',
            '3m': '3minute', '5m': '5minute', '10m': '10minute',
            '15m': '15minute', '30m': '30minute', '1h': '60minute',
            '1d': 'day', '1day': 'day',
        }
        return await asyncio.to_thread(
            self.kite.historical_data,
            instrument_token,
            start_time,
            end_time,
            interval_aliases.get(timeframe.lower(), timeframe),
        )

    async def start_websocket(self) -> None:
        if not self.access_token:
            raise ValueError("Kite access token is required for the WebSocket")

        self._loop = asyncio.get_running_loop()
        self._websocket_ready = asyncio.Event()
        self.ticker = KiteTicker(self.api_key, self.access_token)
        self.ticker.on_connect = self._on_ticker_connect
        self.ticker.on_close = self._on_ticker_close
        self.ticker.on_error = self._on_ticker_error
        self.ticker.on_reconnect = self._on_ticker_reconnect
        self.ticker.on_noreconnect = self._on_ticker_no_reconnect
        self.ticker.on_ticks = self._on_ticker_ticks
        self.ticker.on_order_update = self._on_ticker_order_update
        self.ticker.connect(threaded=True)

        try:
            await asyncio.wait_for(self._websocket_ready.wait(), timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError as error:
            await self.stop_websocket()
            raise TimeoutError("Timed out connecting to KiteTicker") from error

        logger.info("KiteTicker WebSocket connected")

    def _schedule_callback(self, coroutine: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            coroutine.close()
            return

        future = asyncio.run_coroutine_threadsafe(coroutine, loop)

        def log_callback_error(completed: Any) -> None:
            try:
                completed.result()
            except Exception as error:
                logger.error("KiteTicker callback failed: %s", error)

        future.add_done_callback(log_callback_error)

    def _on_ticker_connect(self, ticker: KiteTicker, response: Any) -> None:
        self._ticker_connected = True
        for token, mode in self._subscription_modes.items():
            self._subscribe_ticker(ticker, token, mode)
        if self._loop and self._websocket_ready:
            self._loop.call_soon_threadsafe(self._websocket_ready.set)

    def _on_ticker_close(self, ticker: KiteTicker, code: Any, reason: Any) -> None:
        self._ticker_connected = False
        logger.warning("KiteTicker closed: code=%s reason=%s", code, reason)

    def _on_ticker_error(self, ticker: KiteTicker, code: Any, reason: Any) -> None:
        logger.error("KiteTicker error: code=%s reason=%s", code, reason)

    def _on_ticker_reconnect(self, ticker: KiteTicker, attempts: int) -> None:
        self._ticker_connected = False
        logger.warning("KiteTicker reconnect attempt %s", attempts)

    def _on_ticker_no_reconnect(self, ticker: KiteTicker) -> None:
        self._ticker_connected = False
        logger.error("KiteTicker exhausted its reconnect attempts")

    def _on_ticker_ticks(self, ticker: KiteTicker, ticks: List[Dict[str, Any]]) -> None:
        for source_tick in ticks:
            ohlc = source_tick.get('ohlc') or {}
            tick = {
                'instrument_token': source_tick['instrument_token'],
                'ltp': source_tick.get('last_price'),
                'volume': source_tick.get('volume_traded', 0),
                'timestamp': source_tick.get('exchange_timestamp') or source_tick.get('last_trade_time'),
            }
            tick.update({key: ohlc[key] for key in ('open', 'high', 'low', 'close') if key in ohlc})
            self._schedule_callback(self._process_market_data_tick(tick))

    def _on_ticker_order_update(self, ticker: KiteTicker, order_data: Dict[str, Any]) -> None:
        self._schedule_callback(self._process_order_update(order_data))

    def _subscribe_ticker(self, ticker: KiteTicker, instrument_token: int, mode: str) -> None:
        if instrument_token not in ticker.subscribed_tokens:
            ticker.subscribe([instrument_token])
        ticker.set_mode(mode, [instrument_token])

    async def subscribe_market_data(self, instrument_token: int, mode: str = 'full') -> None:
        """Subscribe using KiteTicker's supported streaming modes."""
        if self.ticker is None:
            raise RuntimeError("KiteTicker is not initialized")
        modes = {
            'full': self.ticker.MODE_FULL,
            'quote': self.ticker.MODE_QUOTE,
            'ltp': self.ticker.MODE_LTP,
        }
        normalized_mode = modes.get(mode.lower())
        if normalized_mode is None:
            raise ValueError(f"Unsupported KiteTicker mode: {mode}")

        self._subscription_modes[instrument_token] = normalized_mode
        if self._ticker_connected:
            reactor.callFromThread(
                self._subscribe_ticker,
                self.ticker,
                instrument_token,
                normalized_mode,
            )

    def add_tick_handler(self, handler: Callable) -> None:
        """Register an async callback for normalized KiteTicker ticks."""
        self.tick_handlers.append(handler)

    async def _process_market_data_tick(self, tick: Dict[str, Any]) -> None:
        for handler in self.tick_handlers:
            try:
                await handler(tick)
            except Exception as error:
                logger.error("Error in market tick handler: %s", error)

    async def stop_websocket(self) -> None:
        ticker = self.ticker
        self._ticker_connected = False
        if ticker is not None:
            reactor.callFromThread(ticker.close)
            self.ticker = None
        logger.info("KiteTicker WebSocket stopped")

    async def _process_order_update(self, order_data: Dict[str, Any]) -> None:
        """
        Process order update from WebSocket.
        
        Args:
            order_data: Order update data
        """
        # Notify all order update handlers
        for handler in self.order_update_handlers:
            try:
                await handler(order_data)
            except Exception as e:
                logger.error(f"Error in order update handler: {str(e)}")

    async def _process_trade_update(self, trade_data: Dict[str, Any]) -> None:
        """
        Process trade update from WebSocket.
        
        Args:
            trade_data: Trade update data
        """
        # Notify all trade update handlers
        for handler in self.trade_update_handlers:
            try:
                await handler(trade_data)
            except Exception as e:
                logger.error(f"Error in trade update handler: {str(e)}")

    async def _process_position_update(self, position_data: Dict[str, Any]) -> None:
        """
        Process position update from WebSocket.
        
        Args:
            position_data: Position update data
        """
        # Notify all position update handlers
        for handler in self.position_update_handlers:
            try:
                await handler(position_data)
            except Exception as e:
                logger.error(f"Error in position update handler: {str(e)}")

    def add_order_update_handler(self, handler: Callable) -> None:
        """
        Add order update handler.
        
        Args:
            handler: Async function to handle order updates
        """
        self.order_update_handlers.append(handler)

    def add_trade_update_handler(self, handler: Callable) -> None:
        """
        Add trade update handler.
        
        Args:
            handler: Async function to handle trade updates
        """
        self.trade_update_handlers.append(handler)

    def add_position_update_handler(self, handler: Callable) -> None:
        """
        Add position update handler.
        
        Args:
            handler: Async function to handle position updates
        """
        self.position_update_handlers.append(handler)

    def add_market_data_handler(self, symbol: str, handler: Callable) -> None:
        """
        Add market data handler for a specific symbol.
        
        Args:
            symbol: Trading symbol
            handler: Async function to handle market data
        """
        if symbol not in self.market_data_handlers:
            self.market_data_handlers[symbol] = []
        self.market_data_handlers[symbol].append(handler)

    async def _handle_market_data(self, symbol: str, data: Dict[str, Any]) -> None:
        """
        Handle market data update.
        
        Args:
            symbol: Trading symbol
            data: Market data
        """
        # Notify all market data handlers for this symbol
        for handler in self.market_data_handlers.get(symbol, []):
            try:
                await handler(data)
            except Exception as e:
                logger.error(f"Error in market data handler for {symbol}: {str(e)}")


class ZerodhaExchange(BaseExchange):
    """
    Zerodha exchange implementation for Belaku TradeBot.
    
    This class provides the exchange interface for Zerodha, implementing
    all required methods for trading operations.
    """

    def __init__(self, name: str, api_key: str, api_secret: str, access_token: Optional[str] = None):
        """
        Initialize Zerodha exchange.
        
        Args:
            name: Exchange name (e.g., 'ZERODHA')
            api_key: Zerodha API key
            api_secret: Zerodha API secret
            access_token: Zerodha access token (optional)
        """
        super().__init__(name)
        self.api = ZerodhaAPI(api_key, api_secret, access_token)
        self.instruments: Dict[str, ZerodhaToken] = {}
        self.order_update_handlers: List[Callable] = []
        self.giftnifty_events: List[Dict[str, Any]] = []
        self._giftnifty_instrument: Optional[ZerodhaToken] = None
        self._giftnifty_data_handler: Optional[Any] = None
        self._giftnifty_timeframe = '15m'
        self._giftnifty_bar: Optional[Dict[str, Any]] = None
        self._giftnifty_last_volume: Optional[int] = None
        self._nifty_future_instrument: Optional[ZerodhaToken] = None
        self._nifty_future_ltp: Optional[float] = None
        self._nifty_future_lots = 1

    async def connect(self) -> None:
        """
        Connect to Zerodha exchange.
        """
        await self.api.connect()
        
        # Load instruments
        await self._load_instruments()
        
        # Set up event handlers
        self.api.add_order_update_handler(self._handle_order_update)
        self.api.add_trade_update_handler(self._handle_trade_update)
        self.api.add_position_update_handler(self._handle_position_update)

        # Start WebSocket only after handlers are ready to receive updates.
        await self.api.start_websocket()

    async def disconnect(self) -> None:
        """
        Disconnect from Zerodha exchange.
        """
        await self.api.stop_websocket()
        await self.api.disconnect()

    def add_order_update_handler(self, handler: Callable) -> None:
        self.order_update_handlers.append(handler)

    async def _load_instruments(self) -> None:
        """
        Load available instruments from Zerodha.
        """
        try:
            # Load instruments from major exchanges
            exchanges = ['NSE', 'NFO', 'BSE', 'MCX', 'NSEIX']
            
            for exchange in exchanges:
                instruments = await self.api.get_instruments(exchange)
                for instrument in instruments:
                    self.instruments[f"{instrument.exchange}:{instrument.symbol}"] = instrument
            
            logger.info(f"Loaded {len(self.instruments)} instruments")
            
        except Exception as e:
            logger.error(f"Failed to load instruments: {str(e)}")
            raise

    async def submit_order(self, order: Order) -> str:
        """
        Submit an order to Zerodha.
        
        Args:
            order: Order to submit
            
        Returns:
            Order ID
        """
        try:
            instrument = self.instruments.get(f"{order.exchange}:{order.symbol}")
            if instrument is None:
                instrument = next(
                    (candidate for candidate in self.instruments.values()
                     if candidate.symbol == order.symbol),
                    None,
                )
            if instrument is None:
                raise ValueError(
                    f"Unknown instrument {order.exchange}:{order.symbol}; "
                    "cannot determine the Kite exchange for this order"
                )
            if instrument.lot_size <= 0 or instrument.tick_size <= 0:
                raise ValueError(
                    f"{order.exchange}:{order.symbol} is a non-tradable index; "
                    "Kite can provide market data but cannot accept orders for it"
                )
            
            # Convert Order to ZerodhaOrderRequest
            is_market_order = order.order_type.lower() == 'market'
            order_request = ZerodhaOrderRequest(
                symbol=order.symbol,
                exchange=instrument.exchange,
                transaction_type=ZerodhaTransactionType.BUY.value if order.side == 'buy' else ZerodhaTransactionType.SELL.value,
                quantity=int(order.quantity),
                price=None if is_market_order else order.price,
                product=(
                    ZerodhaProductType.NRML.value
                    if instrument.instrument_type in {'FUT', 'CE', 'PE'}
                    else ZerodhaProductType.CNC.value
                ),
                order_type=ZerodhaOrderType.MARKET.value if is_market_order else ZerodhaOrderType.LIMIT.value,
                trigger_price=order.stop_price,
                tag=order.timeframe,
            )
            
            # Place order
            response = await self.api.place_order(order_request)
            
            return response.get('order_id', '')
            
        except Exception as e:
            logger.error(f"Failed to submit order: {str(e)}")
            raise

    async def get_order_history(self) -> List[Dict[str, Any]]:
        """Return Zerodha's current-day order book for engine reconciliation."""
        return await self.api.get_order_history()

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order on Zerodha.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if order was canceled successfully, False otherwise
        """
        try:
            await self.api.cancel_order(order_id)
            return True
            
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {str(e)}")
            return False

    async def get_positions(self) -> Dict[str, Any]:
        """
        Get positions from Zerodha.
        
        Returns:
            Dictionary of positions
        """
        try:
            positions = await self.api.get_positions()
            
            # Convert to standard format
            result = {}
            for position in positions:
                result[f"{position.exchange}:{position.symbol}"] = {
                    'quantity': position.quantity,
                    'average_price': position.average_price,
                    'last_price': position.last_price,
                    'pnl': position.pnl,
                    'pnl_percent': position.pnl_percent,
                    'product_type': position.product_type,
                    'exchange': position.exchange
                }
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to get positions: {str(e)}")
            return {}

    async def update_positions(self) -> None:
        """
        Update positions from Zerodha.
        """
        # This method is called by the trading engine to update positions
        pass

    @staticmethod
    def _kite_interval(timeframe: str) -> str:
        intervals = {
            '1m': 'minute', '1minute': 'minute',
            '3m': '3minute', '5m': '5minute', '10m': '10minute',
            '15m': '15minute', '30m': '30minute', '1h': '60minute',
        }
        try:
            return intervals[timeframe.lower()]
        except KeyError as error:
            raise ValueError(f"Unsupported Kite candle timeframe: {timeframe}") from error

    async def setup_strategy_market_data(self, data_handler: Any, timeframe: str = '15m') -> None:
        """Load GIFT NIFTY history and subscribe its index to live ticks."""
        instrument = self._find_giftnifty_instrument()
        if instrument is None:
            raise RuntimeError("No GIFT NIFTY index or futures instrument was found in Kite instruments")
        future = self._find_front_month_nifty_future()
        if future is None:
            raise RuntimeError("No unexpired NIFTY futures contract was found in Kite instruments")

        interval = self._kite_interval(timeframe)
        bar_minutes = 1 if interval == 'minute' else int(interval.removesuffix('minute'))
        now = datetime.now()
        self._giftnifty_instrument = instrument
        self._giftnifty_data_handler = data_handler
        self._giftnifty_timeframe = timeframe
        self._nifty_future_instrument = future
        await data_handler.add_data_stream(instrument.symbol, instrument.exchange, max_size=1000)

        candles = await self.get_historical_data(
            instrument.symbol,
            instrument.exchange,
            now - timedelta(days=30),
            now,
            interval,
        )
        if not candles:
            raise RuntimeError(f"No historical candles returned for {instrument.exchange}:{instrument.symbol}")

        for candle in candles:
            timestamp = candle['timestamp']
            if isinstance(timestamp, str):
                timestamp = datetime.fromisoformat(timestamp)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone().replace(tzinfo=None)
            await data_handler._process_new_data({
                'symbol': instrument.symbol,
                'exchange': instrument.exchange,
                'timestamp': timestamp,
                'open_price': candle['open'],
                'high_price': candle['high'],
                'low_price': candle['low'],
                'close_price': candle['close'],
                'volume': candle['volume'],
                'timeframe': timeframe,
            })

        latest = candles[-1]
        latest_timestamp = latest['timestamp']
        if isinstance(latest_timestamp, str):
            latest_timestamp = datetime.fromisoformat(latest_timestamp)
        if latest_timestamp.tzinfo is not None:
            latest_timestamp = latest_timestamp.astimezone().replace(tzinfo=None)
        bucket_start = self._giftnifty_bucket_start(latest_timestamp, bar_minutes)
        if bucket_start == self._giftnifty_bucket_start(now, bar_minutes):
            self._giftnifty_bar = {
                'timestamp': bucket_start,
                'open_price': latest['open'],
                'high_price': latest['high'],
                'low_price': latest['low'],
                'close_price': latest['close'],
                'volume': latest['volume'],
            }

        self.api.add_tick_handler(self._handle_giftnifty_tick)
        await self.api.subscribe_market_data(instrument.instrument_token, mode='full')
        self.api.add_tick_handler(self._handle_nifty_future_tick)
        await self.api.subscribe_market_data(future.instrument_token, mode='ltp')
        logger.info(
            "Subscribed SuperTrend strategy to %s:%s token=%s timeframe=%s with %d historical candle(s); orders route to %s:%s",
            instrument.exchange,
            instrument.symbol,
            instrument.instrument_token,
            timeframe,
            len(candles),
            future.exchange,
            future.symbol,
        )

    @staticmethod
    def _giftnifty_bucket_start(timestamp: datetime, minutes: int) -> datetime:
        bucket_minute = timestamp.minute - timestamp.minute % minutes
        return timestamp.replace(minute=bucket_minute, second=0, microsecond=0)

    async def _handle_giftnifty_tick(self, tick: Dict[str, Any]) -> None:
        instrument = self._giftnifty_instrument
        data_handler = self._giftnifty_data_handler
        if instrument is None or data_handler is None:
            return
        if tick.get('instrument_token') != instrument.instrument_token:
            return

        ltp = float(tick['ltp'])
        timestamp = tick.get('timestamp', datetime.now())
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone().replace(tzinfo=None)
        interval = self._kite_interval(self._giftnifty_timeframe)
        bar_minutes = 1 if interval == 'minute' else int(interval.removesuffix('minute'))
        bucket_start = self._giftnifty_bucket_start(timestamp, bar_minutes)

        if self._giftnifty_bar is None or self._giftnifty_bar['timestamp'] != bucket_start:
            self._giftnifty_bar = {
                'timestamp': bucket_start,
                'open_price': ltp,
                'high_price': ltp,
                'low_price': ltp,
                'close_price': ltp,
                'volume': 0,
            }
        else:
            self._giftnifty_bar['high_price'] = max(self._giftnifty_bar['high_price'], ltp)
            self._giftnifty_bar['low_price'] = min(self._giftnifty_bar['low_price'], ltp)
            self._giftnifty_bar['close_price'] = ltp

        tick_volume = int(tick.get('volume', self._giftnifty_last_volume or 0))
        volume_delta = (
            0 if self._giftnifty_last_volume is None
            else max(0, tick_volume - self._giftnifty_last_volume)
        )
        self._giftnifty_bar['volume'] += volume_delta
        self._giftnifty_last_volume = tick_volume
        logger.debug(
            "[LIVE] %s:%s timeframe=%s LTP=%.2f candle_OHLC=%.2f/%.2f/%.2f/%.2f",
            instrument.exchange,
            instrument.symbol,
            self._giftnifty_timeframe,
            ltp,
            self._giftnifty_bar['open_price'],
            self._giftnifty_bar['high_price'],
            self._giftnifty_bar['low_price'],
            self._giftnifty_bar['close_price'],
        )
        await data_handler._process_new_data({
            'symbol': instrument.symbol,
            'exchange': instrument.exchange,
            **self._giftnifty_bar,
            'timeframe': self._giftnifty_timeframe,
            'indicators': {'last_tick_at': datetime.now()},
        })

    async def _handle_nifty_future_tick(self, tick: Dict[str, Any]) -> None:
        instrument = self._nifty_future_instrument
        if instrument and tick.get('instrument_token') == instrument.instrument_token:
            self._nifty_future_ltp = float(tick['ltp'])
            logger.debug(
                "[LIVE] %s:%s LTP=%.2f lot_size=%s",
                instrument.exchange,
                instrument.symbol,
                self._nifty_future_ltp,
                instrument.lot_size,
            )

    def _find_front_month_nifty_future(self) -> Optional[ZerodhaToken]:
        """Return the nearest unexpired NIFTY index futures contract."""
        today = datetime.now().date()
        excluded_prefixes = ('GIFTNIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT')
        candidates = [
            instrument for instrument in self.instruments.values()
            if instrument.exchange.upper() == 'NFO'
            and instrument.instrument_type == 'FUT'
            and instrument.symbol.upper().startswith('NIFTY')
            and not instrument.symbol.upper().startswith(excluded_prefixes)
            and instrument.expiry_date is not None
            and instrument.expiry_date.date() >= today
            and instrument.lot_size > 0
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda instrument: instrument.expiry_date)

    def order_symbol_for_signal(self, signal_symbol: str) -> Optional[str]:
        normalized = ''.join(character for character in signal_symbol.upper() if character.isalnum())
        if normalized == 'GIFTNIFTY':
            instrument = self._nifty_future_instrument or self._find_front_month_nifty_future()
            return instrument.symbol if instrument else None
        return signal_symbol

    async def prepare_order(self, order: Order) -> None:
        """Route GIFT NIFTY signals to one current NIFTY futures lot."""
        signal_symbol = ''.join(character for character in order.symbol.upper() if character.isalnum())
        if signal_symbol != 'GIFTNIFTY':
            return

        instrument = self._nifty_future_instrument or self._find_front_month_nifty_future()
        if instrument is None:
            raise RuntimeError("Cannot route GIFT NIFTY signal: no unexpired NIFTY futures contract")

        ltp = self._nifty_future_ltp
        if ltp is None or ltp <= 0:
            quote = await self.api.get_quote(instrument.symbol, instrument.exchange)
            ltp = float(quote.get('last_price') or 0)
        if ltp <= 0:
            raise RuntimeError(f"No valid LTP available for {instrument.exchange}:{instrument.symbol}")

        order.symbol = instrument.symbol
        order.quantity = float(instrument.lot_size * self._nifty_future_lots)
        order.price = ltp
        order.order_type = 'market'
        logger.info(
            "Mapped GIFT NIFTY %s signal to %s:%s quantity=%s price_reference=%.2f",
            order.side.upper(),
            instrument.exchange,
            instrument.symbol,
            order.quantity,
            ltp,
        )

    def risk_limit_for_order(self, order: Order) -> Optional[float]:
        """Return a 10% buffer over one mapped futures order's notional."""
        instrument = self._nifty_future_instrument
        if (
            instrument is None
            or order.symbol != instrument.symbol
            or order.quantity != instrument.lot_size * self._nifty_future_lots
            or order.price is None
            or order.price <= 0
        ):
            return None
        return order.quantity * order.price * 1.1

    async def get_historical_data(self, symbol: str, exchange: str, start_time: datetime, end_time: datetime, timeframe: str = "15m") -> List[Dict[str, Any]]:
        """
        Get historical data from Zerodha.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            start_time: Start time
            end_time: End time
            timeframe: Timeframe (1m, 5m, 15m, 1h, 1d)
            
        Returns:
            List of historical data candles
        """
        try:
            instrument = self.instruments.get(f"{exchange}:{symbol}")
            if instrument is None:
                logger.error(f"Unknown instrument for historical data: {exchange}:{symbol}")
                return []
            
            candles = await self.api.get_historical_data(
                instrument.instrument_token,
                start_time,
                end_time,
                timeframe,
            )
            return [
                {
                    'timestamp': candle['date'],
                    'open': candle['open'],
                    'high': candle['high'],
                    'low': candle['low'],
                    'close': candle['close'],
                    'volume': candle['volume'],
                }
                for candle in candles
            ]
            
        except Exception as e:
            logger.error(f"Failed to get historical data for {symbol}: {str(e)}")
            return []

    async def _handle_order_update(self, order_data: Dict[str, Any]) -> None:
        """
        Handle order update from Zerodha.
        
        Args:
            order_data: Order update data
        """
        order_id = order_data.get('order_id') or order_data.get('orderid')
        logger.info(
            "[ORDER UPDATE] id=%s status=%s filled=%s pending=%s average_price=%s",
            order_id,
            order_data.get('status'),
            order_data.get('filled_quantity'),
            order_data.get('pending_quantity'),
            order_data.get('average_price'),
        )
        for handler in self.order_update_handlers:
            try:
                await handler(order_data)
            except Exception as error:
                logger.error("Engine order update handler failed: %s", error)

    async def _handle_trade_update(self, trade_data: Dict[str, Any]) -> None:
        """
        Handle trade update from Zerodha.
        
        Args:
            trade_data: Trade update data
        """
        # Process trade update
        logger.info(f"Trade update: {trade_data}")

    async def _handle_position_update(self, position_data: Dict[str, Any]) -> None:
        """
        Handle position update from Zerodha.
        
        Args:
            position_data: Position update data
        """
        # Process position update
        logger.info(f"Position update: {position_data}")

    def _find_giftnifty_instrument(self) -> Optional[ZerodhaToken]:
        """Prefer the GIFT NIFTY index; fall back to a listed futures contract."""
        index_candidates = [
            instrument for instrument in self.instruments.values()
            if instrument.exchange.upper() == 'NSEIX'
            and instrument.instrument_type == 'EQ'
            and any(
                ''.join(character for character in value.upper() if character.isalnum()) == 'GIFTNIFTY'
                for value in (instrument.symbol, instrument.name)
            )
        ]
        if index_candidates:
            return index_candidates[0]

        candidates = [
            instrument for instrument in self.instruments.values()
            if instrument.symbol.upper().replace(' ', '').startswith('GIFTNIFTY')
            and instrument.instrument_type == 'FUT'
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda instrument: instrument.expiry_date or datetime.max,
        )

    async def extract_giftnifty_events(self) -> List[Dict[str, Any]]:
        """
        Extract events for the GiftNifty futures contract from Zerodha.
            
        Returns:
            List of giftnifty events
        """
        try:
            instrument = self._find_giftnifty_instrument()
            if instrument is None:
                logger.debug("Kite lists no GiftNifty futures instrument; skipping event extraction")
                return []
            
            # Get current quote for giftnifty
            quote = await self.api.get_quote(instrument.symbol, instrument.exchange)
            
            # Extract events from quote data
            events = []
            
            # Check for gift events
            if quote.get('gift', False):
                events.append({
                    'type': 'gift',
                    'symbol': instrument.symbol,
                    'timestamp': datetime.now(),
                    'data': quote.get('gift_data', {})
                })
            
            # Check for nifty events
            if quote.get('nifty', False):
                events.append({
                    'type': 'nifty',
                    'symbol': instrument.symbol,
                    'timestamp': datetime.now(),
                    'data': quote.get('nifty_data', {})
                })
            
            # Check for special events
            if quote.get('special_events', []):
                for event in quote['special_events']:
                    events.append({
                        'type': 'special',
                        'symbol': instrument.symbol,
                        'timestamp': datetime.now(),
                        'data': event
                    })
            
            # Store events
            self.giftnifty_events.extend(events)
            
            logger.info(f"Extracted {len(events)} giftnifty events")
            return events
            
        except Exception as e:
            logger.error(f"Failed to extract giftnifty events: {str(e)}")
            return []

    async def get_giftnifty_events(self) -> List[Dict[str, Any]]:
        """
        Get all stored giftnifty events.
        
        Returns:
            List of giftnifty events
        """
        return self.giftnifty_events.copy()

    async def clear_giftnifty_events(self) -> None:
        """
        Clear stored giftnifty events.
        """
        self.giftnifty_events.clear()
        logger.info("Cleared giftnifty events")