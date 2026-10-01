"""
Zerodha exchange integration for Belaku TradeBot.

This module provides integration with Zerodha, one of India's largest
broking companies, supporting trading in Indian equities, derivatives,
commodities, and currencies.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
from dataclasses import dataclass, field
import json
import time
import hmac
import hashlib
import requests
import websockets
from enum import Enum

from ..core.engine import Order, OrderStatus, PositionSide
from .base import BaseExchange

logger = logging.getLogger(__name__)


class ZerodhaOrderStatus(Enum):
    """Zerodha order status enumeration."""
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    OPEN = "open"
    TRIGGERED = "triggered"
    EXPIRED = "expired"


class ZerodhaProductType(Enum):
    """Zerodha product type enumeration."""
    CNC = "CNC"  # Cash and Carry
    INTRADAY = "MIS"  # Mis
    MARGIN = "MARGIN"  # Margin
    CO = "CO"  # Cover Order
    BO = "BO"  # Bracket Order


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
    SELL = "SELL"pe: str
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
    m2m_pnl: float = 0.0
    overnight_pnl: float = 0.0


@dataclass
class ZerodhaOrderRequest:
    """Zerodha order request data structure."""
    symbol: str
    transaction_type: str
    quantity: int
    price: Optional[float] = None
    product: str = "CNC"
    order_type: str = "MARKET"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
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
        self.base_url = "https://api.zerodha.com"
        self.websocket_url = "wss://ws.zerodha.com"
        self.session = requests.Session()
        self.session.headers.update({
            'X-KITE-APIKEY': api_key,
            'Content-Type': 'application/json'
        })
        
        # Event handlers
        self.order_update_handlers: List[Callable] = []
        self.trade_update_handlers: List[Callable] = []
        self.position_update_handlers: List[Callable] = []
        self.market_data_handlers: Dict[str, List[Callable]] = {}
        
        # WebSocket connection
        self.ws_connection = None
        self.websocket_tasks = []

    def _generate_access_token(self) -> str:
        """
        Generate access token using API key and secret.
        
        Returns:
            Access token string
        """
        # This is a simplified version - in production, you'd need to handle
        # the actual OAuth flow with Zerodha
        timestamp = str(int(time.time()))
        message = timestamp + self.api_key + self.api_secret
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest().upper()
        
        return f"{self.api_key}:{timestamp}:{signature}"

    def _get_headers(self) -> Dict[str, str]:
        """
        Get request headers with authentication.
        
        Returns:
            Dictionary of request headers
        """
        headers = self.session.headers.copy()
        if self.access_token:
            headers['Authorization'] = f'Bearer {self.access_token}'
        return headers

    async def connect(self) -> None:
        """
        Connect to Zerodha API.
        """
        try:
            # Generate access token if not provided
            if not self.access_token:
                self.access_token = self._generate_access_token()
            
            # Test connection
            response = self.session.get(
                f"{self.base_url}/api/user/profile",
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            logger.info("Connected to Zerodha API")
            
        except Exception as e:
            logger.error(f"Failed to connect to Zerodha API: {str(e)}")
            raise

    async def disconnect(self) -> None:
        """
        Disconnect from Zerodha API.
        """
        # Cancel all WebSocket connections
        if self.websocket_tasks:
            for task in self.websocket_tasks:
                task.cancel()
            self.websocket_tasks.clear()
        
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
            response = self.session.get(
                f"{self.base_url}/api/instruments",
                params={'exchange': exchange},
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            instruments = []
            for instrument_data in response.json():
                token = ZerodhaToken(
                    symbol=instrument_data['tradingsymbol'],
                    name=instrument_data['name'],
                    exchange=instrument_data['exchange'],
                    instrument_type=instrument_data['instrumenttype'],
                    lot_size=instrument_data['lotsize'],
                    tick_size=instrument_data['ticksize'],
                    price_precision=instrument_data['priceprecision'],
                    expiry_date=datetime.fromisoformat(instrument_data['expiry']) if instrument_data.get('expiry') else None,
                    strike_price=float(instrument_data['strike']) if instrument_data.get('strike') else None,
                    option_type=instrument_data['optiontype'] if instrument_data.get('optiontype') else None
                )
                instruments.append(token)
            
            logger.info(f"Retrieved {len(instruments)} instruments from {exchange}")
            return instruments
            
        except Exception as e:
            logger.error(f"Failed to get instruments: {str(e)}")
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
            response = self.session.get(
                f"{self.base_url}/api/quote",
                params={'symbol': symbol, 'exchange': exchange},
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            return response.json()
            
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
            order_data = {
                'symbol': order_request.symbol,
                'transaction_type': order_request.transaction_type,
                'quantity': order_request.quantity,
                'product': order_request.product,
                'order_type': order_request.order_type,
                'validity': order_request.validity,
                'disclosed_quantity': order_request.disclosed_quantity,
            }
            
            if order_request.price:
                order_data['price'] = order_request.price
            
            if order_request.stop_loss:
                order_data['stop_loss'] = order_request.stop_loss
            
            if order_request.take_profit:
                order_data['take_profit'] = order_request.take_profit
            
            if order_request.trigger_price:
                order_data['trigger_price'] = order_request.trigger_price
            
            if order_request.tag:
                order_data['tag'] = order_request.tag
            
            response = self.session.post(
                f"{self.base_url}/api/order/regular",
                json=order_data,
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            order_response = response.json()
            logger.info(f"Order placed: {order_response.get('orderid', 'unknown')}")
            return order_response
            
        except Exception as e:
            logger.error(f"Failed to place order: {str(e)}")
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
            response = self.session.delete(
                f"{self.base_url}/api/order/regular/{order_id}",
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            cancel_response = response.json()
            logger.info(f"Order canceled: {order_id}")
            return cancel_response
            
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
            response = self.session.get(
                f"{self.base_url}/api/positions",
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            positions = []
            for position_data in response.json():
                position = ZerodhaPosition(
                    symbol=position_data['symbol'],
                    quantity=float(position_data['quantity']),
                    average_price=float(position_data['average_price']),
                    last_price=float(position_data['last_price']),
                    pnl=float(position_data['pnl']),
                    pnl_percent=float(position_data['pnl_percent']),
                    product_type=position_data['product'],
                    exchange=position_data['exchange'],
                    m2m_pnl=float(position_data.get('m2m_pnl', 0)),
                    overnight_pnl=float(position_data.get('overnight_pnl', 0))
                )
                positions.append(position)
            
            logger.info(f"Retrieved {len(positions)} positions")
            return positions
            
        except Exception as e:
            logger.error(f"Failed to get positions: {str(e)}")
            raise

    async def get_order_history(self, days: int = 30) -> List[Dict[str, Any]]:
        """
        Get order history from Zerodha.
        
        Args:
            days: Number of days of history to retrieve
            
        Returns:
            List of order history entries
        """
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)
            
            response = self.session.get(
                f"{self.base_url}/api/orders",
                params={
                    'from': start_date.isoformat(),
                    'to': end_date.isoformat()
                },
                headers=self._get_headers()
            )
            response.raise_for_status()
            
            logger.info(f"Retrieved {len(response.json())} orders from history")
            return response.json()
            
        except Exception as e:
            logger.error(f"Failed to get order history: {str(e)}")
            raise

    async def start_websocket(self) -> None:
        """
        Start WebSocket connection for real-time data.
        """
        try:
            # Connect to WebSocket
            self.ws_connection = await websockets.connect(self.websocket_url)
            
            # Start WebSocket tasks
            order_task = asyncio.create_task(self._handle_order_updates())
            trade_task = asyncio.create_task(self._handle_trade_updates())
            position_task = asyncio.create_task(self._handle_position_updates())
            
            self.websocket_tasks = [order_task, trade_task, position_task]
            
            logger.info("WebSocket connection started")
            
        except Exception as e:
            logger.error(f"Failed to start WebSocket: {str(e)}")
            raise

    async def stop_websocket(self) -> None:
        """
        Stop WebSocket connection.
        """
        if self.ws_connection:
            await self.ws_connection.close()
            self.ws_connection = None
        
        for task in self.websocket_tasks:
            task.cancel()
        
        self.websocket_tasks.clear()
        logger.info("WebSocket connection stopped")

    async def _handle_order_updates(self) -> None:
        """
        Handle order update WebSocket messages.
        """
        try:
            async for message in self.ws_connection:
                data = json.loads(message)
                if data.get('type') == 'order':
                    order_data = data['data']
                    await self._process_order_update(order_data)
                    
        except asyncio.CancelledError:
            logger.info("Order update handler cancelled")
        except Exception as e:
            logger.error(f"Error in order update handler: {str(e)}")

    async def _handle_trade_updates(self) -> None:
        """
        Handle trade update WebSocket messages.
        """
        try:
            async for message in self.ws_connection:
                data = json.loads(message)
                if data.get('type') == 'trade':
                    trade_data = data['data']
                    await self._process_trade_update(trade_data)
                    
        except asyncio.CancelledError:
            logger.info("Trade update handler cancelled")
        except Exception as e:
            logger.error(f"Error in trade update handler: {str(e)}")

    async def _handle_position_updates(self) -> None:
        """
        Handle position update WebSocket messages.
        """
        try:
            async for message in self.ws_connection:
                data = json.loads(message)
                if data.get('type') == 'position':
                    position_data = data['data']
                    await self._process_position_update(position_data)
                    
        except asyncio.CancelledError:
            logger.info("Position update handler cancelled")
        except Exception as e:
            logger.error(f"Error in position update handler: {str(e)}")

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
        self.giftnifty_events: List[Dict[str, Any]] = []

    async def connect(self) -> None:
        """
        Connect to Zerodha exchange.
        """
        await self.api.connect()
        
        # Load instruments
        await self._load_instruments()
        
        # Start WebSocket for real-time data
        await self.api.start_websocket()
        
        # Set up event handlers
        self.api.add_order_update_handler(self._handle_order_update)
        self.api.add_trade_update_handler(self._handle_trade_update)
        self.api.add_position_update_handler(self._handle_position_update)

    async def disconnect(self) -> None:
        """
        Disconnect from Zerodha exchange.
        """
        await self.api.stop_websocket()
        await self.api.disconnect()

    async def _load_instruments(self) -> None:
        """
        Load available instruments from Zerodha.
        """
        try:
            # Load instruments from major exchanges
            exchanges = ['NSE', 'NFO', 'BSE', 'MCX']
            
            for exchange in exchanges:
                instruments = await self.api.get_instruments(exchange)
                for instrument in instruments:
                    self.instruments[instrument.symbol] = instrument
            
            logger.info(f"Loaded {len(self.instruments)} instruments")
            
        except Exception as e:
            logger.error(f"Failed to load instruments: {str(e)}")

    async def submit_order(self, order: Order) -> str:
        """
        Submit an order to Zerodha.
        
        Args:
            order: Order to submit
            
        Returns:
            Order ID
        """
        try:
            # Convert Order to ZerodhaOrderRequest
            order_request = ZerodhaOrderRequest(
                symbol=order.symbol,
                transaction_type=ZerodhaTransactionType.BUY.value if order.side == 'buy' else ZerodhaTransactionType.SELL.value,
                quantity=int(order.quantity),
                price=order.price,
                product=ZerodhaProductType.CNC.value,
                order_type=ZerodhaOrderType.MARKET.value if order.price is None else ZerodhaOrderType.LIMIT.value,
                stop_loss=order.stop_price,
                take_profit=order.take_profit
            )
            
            # Place order
            response = await self.api.place_order(order_request)
            
            return response.get('orderid', '')
            
        except Exception as e:
            logger.error(f"Failed to submit order: {str(e)}")
            raise

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
                result[position.symbol] = {
                    'quantity': position.quantity,
                    'average_price': position.average_price,
                    'last_price': position.last_price,
                    'pnl': position.pnl,
                    'pnl_percent': position.pnl_percent,
                    'product_type': position.product_type,
                    'exchange': position.exchange,
                    'm2m_pnl': position.m2m_pnl,
                    'overnight_pnl': position.overnight_pnl
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

    async def get_historical_data(self, symbol: str, exchange: str, start_time: datetime, end_time: datetime, timeframe: str = "1minute") -> List[Dict[str, Any]]:
        """
        Get historical data from Zerodha.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            start_time: Start time
            end_time: End time
            timeframe: Timeframe (1minute, 5minute, 15minute, 1hour, 1day)
            
        Returns:
            List of historical data candles
        """
        try:
            # Zerodha API endpoint for historical data
            response = self.session.get(
                f"{self.api.base_url}/api/historical",
                params={
                    'symbol': symbol,
                    'exchange': exchange,
                    'from': start_time.isoformat(),
                    'to': end_time.isoformat(),
                    'interval': timeframe
                },
                headers=self.api._get_headers()
            )
            response.raise_for_status()
            
            return response.json()
            
        except Exception as e:
            logger.error(f"Failed to get historical data for {symbol}: {str(e)}")
            return []

    async def _handle_order_update(self, order_data: Dict[str, Any]) -> None:
        """
        Handle order update from Zerodha.
        
        Args:
            order_data: Order update data
        """
        # Process order update
        order_id = order_data.get('orderid')
        status = order_data.get('status')
        
        # Convert Zerodha status to standard status
        status_map = {
            'complete': OrderStatus.FILLED,
            'cancelled': OrderStatus.CANCELED,
            'rejected': OrderStatus.REJECTED,
            'open': OrderStatus.SUBMITTED,
            'triggered': OrderStatus.SUBMITTED,
            'expired': OrderStatus.REJECTED
        }
        
        # Update order in engine (this would be called from the engine)
        # For now, just log the update
        logger.info(f"Order update: {order_id} - {status}")

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

    async def extract_giftnifty_events(self, symbol: str = "giftnifty") -> List[Dict[str, Any]]:
        """
        Extract events for giftnifty from Zerodha.
        
        Args:
            symbol: Symbol to extract events for (default: giftnifty)
            
        Returns:
            List of giftnifty events
        """
        try:
            # Get current quote for giftnifty
            quote = await self.api.get_quote(symbol, "NSE")
            
            # Extract events from quote data
            events = []
            
            # Check for gift events
            if quote.get('gift', False):
                events.append({
                    'type': 'gift',
                    'symbol': symbol,
                    'timestamp': datetime.now(),
                    'data': quote.get('gift_data', {})
                })
            
            # Check for nifty events
            if quote.get('nifty', False):
                events.append({
                    'type': 'nifty',
                    'symbol': symbol,
                    'timestamp': datetime.now(),
                    'data': quote.get('nifty_data', {})
                })
            
            # Check for special events
            if quote.get('special_events', []):
                for event in quote['special_events']:
                    events.append({
                        'type': 'special',
                        'symbol': symbol,
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