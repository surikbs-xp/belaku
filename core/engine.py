"""
Core trading engine for Belaku TradeBot.

This module implements the main trading engine that orchestrates all trading activities,
including order execution, position management, and strategy integration.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
import json
import time

from .strategy.base import BaseStrategy
from .risk.manager import RiskManager
from .data.handler import DataHandler
from .exchanges.base import BaseExchange

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """Order status enumeration."""
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class PositionSide(Enum):
    """Position side enumeration."""
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


@dataclass
class Order:
    """Order data structure."""
    id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    avg_fill_price: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)
    exchange: Optional[str] = None
    order_id: Optional[str] = None
    client_order_id: Optional[str] = None

    @property
    def remaining_quantity(self) -> float:
        """Get remaining quantity to fill."""
        return self.quantity - self.filled_quantity

    @property
    def is_filled(self) -> bool:
        """Check if order is completely filled."""
        return self.status == OrderStatus.FILLED

    @property
    def is_cancelable(self) -> bool:
        """Check if order can be canceled."""
        return self.status in [OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED]


@dataclass
class Position:
    """Position data structure."""
    symbol: str
    side: PositionSide
    quantity: float
    entry_price: float
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    exchange: Optional[str] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    trailing_stop: Optional[float] = None

    @property
    def market_value(self) -> float:
        """Get current market value of position."""
        if self.current_price is None:
            return 0.0
        return self.quantity * self.current_price

    @property
    def break_even_price(self) -> float:
        """Calculate break-even price."""
        return self.entry_price

    def update_pnl(self, current_price: float) -> None:
        """Update P&L based on current price."""
        if self.side == PositionSide.LONG:
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity
        self.current_price = current_price


class TradingEngine:
    """
    Main trading engine that manages orders, positions, and strategy execution.
    
    This class serves as the central coordinator for all trading activities,
    integrating strategies, risk management, and exchange connections.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the trading engine.
        
        Args:
            config: Configuration dictionary containing engine settings
        """
        self.config = config
        self.orders: Dict[str, Order] = {}
        self.positions: Dict[str, Position] = {}
        self.strategies: List[BaseStrategy] = []
        self.risk_manager = RiskManager(config.get('risk', {}))
        self.data_handler = DataHandler(config.get('data', {}))
        self.exchanges: Dict[str, BaseExchange] = {}
        self.running = False
        self.order_history: List[Order] = []
        self.position_history: List[Position] = []
        
        # Performance tracking
        self.stats = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_volume': 0.0,
            'total_pnl': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0,
        }
        
        logger.info("Trading engine initialized")

    def add_strategy(self, strategy: BaseStrategy) -> None:
        """
        Add a trading strategy to the engine.
        
        Args:
            strategy: Strategy instance to add
        """
        self.strategies.append(strategy)
        logger.info(f"Added strategy: {strategy.name}")

    def add_exchange(self, exchange: BaseExchange) -> None:
        """
        Add an exchange connection to the engine.
        
        Args:
            exchange: Exchange instance to add
        """
        self.exchanges[exchange.name] = exchange
        logger.info(f"Added exchange: {exchange.name}")

    async def start(self) -> None:
        """
        Start the trading engine.
        
        This method initializes all components and begins the main trading loop.
        """
        logger.info("Starting trading engine")
        self.running = True
        
        # Initialize exchanges
        for exchange in self.exchanges.values():
            await exchange.connect()
        
        # Start data handler
        await self.data_handler.start()
        
        # Start main trading loop
        asyncio.create_task(self._trading_loop())
        
        logger.info("Trading engine started successfully")

    async def stop(self) -> None:
        """
        Stop the trading engine.
        
        This method gracefully shuts down all components.
        """
        logger.info("Stopping trading engine")
        self.running = False
        
        # Stop data handler
        await self.data_handler.stop()
        
        # Disconnect exchanges
        for exchange in self.exchanges.values():
            await exchange.disconnect()
        
        logger.info("Trading engine stopped")

    async def submit_order(self, order: Order) -> bool:
        """
        Submit an order for execution.
        
        Args:
            order: Order to submit
            
        Returns:
            True if order was submitted successfully, False otherwise
        """
        try:
            # Validate order
            if not await self._validate_order(order):
                logger.error(f"Order validation failed for {order.id}")
                return False
            
            # Check risk management
            if not await self.risk_manager.check_order(order):
                logger.error(f"Risk check failed for order {order.id}")
                return False
            
            # Submit to exchange
            exchange = self.exchanges.get(order.exchange)
            if not exchange:
                logger.error(f"Exchange {order.exchange} not found")
                return False
            
            exchange_order_id = await exchange.submit_order(order)
            order.order_id = exchange_order_id
            order.status = OrderStatus.SUBMITTED
            
            # Store order
            self.orders[order.id] = order
            self.order_history.append(order)
            
            logger.info(f"Order submitted: {order.id} - {order.symbol} {order.side} {order.quantity}")
            return True
            
        except Exception as e:
            logger.error(f"Error submitting order {order.id}: {str(e)}")
            order.status = OrderStatus.REJECTED
            return False

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an existing order.
        
        Args:
            order_id: ID of the order to cancel
            
        Returns:
            True if order was canceled successfully, False otherwise
        """
        order = self.orders.get(order_id)
        if not order:
            logger.error(f"Order {order_id} not found")
            return False
        
        if not order.is_cancelable:
            logger.error(f"Order {order_id} cannot be canceled (status: {order.status})")
            return False
        
        try:
            exchange = self.exchanges.get(order.exchange)
            if exchange:
                await exchange.cancel_order(order.order_id)
            
            order.status = OrderStatus.CANCELED
            logger.info(f"Order canceled: {order_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error canceling order {order_id}: {str(e)}")
            return False

    async def get_positions(self) -> Dict[str, Position]:
        """
        Get current positions.
        
        Returns:
            Dictionary of positions keyed by symbol
        """
        # Update positions from exchanges
        for exchange in self.exchanges.values():
            await exchange.update_positions()
        
        return self.positions

    async def close_position(self, symbol: str, quantity: float, side: str) -> bool:
        """
        Close an existing position.
        
        Args:
            symbol: Symbol to close
            quantity: Quantity to close
            side: Position side (long/short)
            
        Returns:
            True if position was closed successfully, False otherwise
        """
        try:
            # Get current position
            position = self.positions.get(symbol)
            if not position:
                logger.error(f"Position for {symbol} not found")
                return False
            
            # Determine closing side
            close_side = "sell" if position.side == PositionSide.LONG else "buy"
            
            # Submit closing order
            order = Order(
                id=f"close_{symbol}_{int(time.time())}",
                symbol=symbol,
                side=close_side,
                order_type="market",
                quantity=quantity,
                exchange=position.exchange
            )
            
            success = await self.submit_order(order)
            if success:
                # Update position
                position.quantity -= quantity
                if position.quantity <= 0:
                    position.side = PositionSide.FLAT
                    position.quantity = 0
                
                logger.info(f"Position closed: {symbol} {quantity}")
            
            return success
            
        except Exception as e:
            logger.error(f"Error closing position {symbol}: {str(e)}")
            return False

    async def _trading_loop(self) -> None:
        """
        Main trading loop that runs continuously while the engine is active.
        
        This method handles strategy execution, order management, and position updates.
        """
        while self.running:
            try:
                # Get current market data
                market_data = await self.data_handler.get_latest_data()
                
                # Execute strategies
                for strategy in self.strategies:
                    signals = await strategy.analyze(market_data)
                    await self._process_signals(signals)
                
                # Update positions
                await self._update_positions()
                
                # Check for stop-loss and take-profit
                await self._check_exit_conditions()
                
                # Sleep for next iteration
                await asyncio.sleep(self.config.get('loop_interval', 1.0))
                
            except Exception as e:
                logger.error(f"Error in trading loop: {str(e)}")
                await asyncio.sleep(5.0)  # Wait before retrying

    async def _process_signals(self, signals: List[Dict[str, Any]]) -> None:
        """
        Process trading signals from strategies.
        
        Args:
            signals: List of trading signals
        """
        for signal in signals:
            try:
                # Create order from signal
                order = self._create_order_from_signal(signal)
                if order:
                    # Submit order
                    await self.submit_order(order)
            except Exception as e:
                logger.error(f"Error processing signal: {str(e)}")

    async def _update_positions(self) -> None:
        """
        Update current positions from exchanges.
        """
        for exchange in self.exchanges.values():
            exchange_positions = await exchange.get_positions()
            
            for symbol, position_data in exchange_positions.items():
                if symbol in self.positions:
                    # Update existing position
                    position = self.positions[symbol]
                    position.quantity = position_data['quantity']
                    position.current_price = position_data['price']
                    position.update_pnl(position_data['price'])
                else:
                    # Create new position
                    side = PositionSide.LONG if position_data['side'] == 'buy' else PositionSide.SHORT
                    position = Position(
                        symbol=symbol,
                        side=side,
                        quantity=position_data['quantity'],
                        entry_price=position_data['entry_price'],
                        exchange=exchange.name
                    )
                    self.positions[symbol] = position
                    self.position_history.append(position)

    async def _check_exit_conditions(self) -> None:
        """
        Check for stop-loss and take-profit conditions.
        """
        for symbol, position in self.positions.items():
            if position.current_price is None:
                continue
            
            # Check stop-loss
            if position.stop_loss:
                if position.side == PositionSide.LONG and position.current_price <= position.stop_loss:
                    await self.close_position(symbol, position.quantity, position.side.value)
                elif position.side == PositionSide.SHORT and position.current_price >= position.stop_loss:
                    await self.close_position(symbol, position.quantity, position.side.value)
            
            # Check take-profit
            if position.take_profit:
                if position.side == PositionSide.LONG and position.current_price >= position.take_profit:
                    await self.close_position(symbol, position.quantity, position.side.value)
                elif position.side == PositionSide.SHORT and position.current_price <= position.take_profit:
                    await self.close_position(symbol, position.quantity, position.side.value)

    async def _validate_order(self, order: Order) -> bool:
        """
        Validate an order before submission.
        
        Args:
            order: Order to validate
            
        Returns:
            True if order is valid, False otherwise
        """
        # Check required fields
        if not order.symbol or not order.side or not order.order_type:
            return False
        
        # Check quantity
        if order.quantity <= 0:
            return False
        
        # Check price for limit orders
        if order.order_type == 'limit' and (order.price is None or order.price <= 0):
            return False
        
        # Check stop price for stop orders
        if order.order_type in ['stop', 'stop_limit'] and (order.stop_price is None or order.stop_price <= 0):
            return False
        
        return True

    def _create_order_from_signal(self, signal: Dict[str, Any]) -> Optional[Order]:
        """
        Create an order from a trading signal.
        
        Args:
            signal: Trading signal dictionary
            
        Returns:
            Order object or None if signal cannot be converted
        """
        try:
            signal_type = signal.get('type', '')
            symbol = signal.get('symbol', '')
            quantity = signal.get('quantity', 0.0)
            price = signal.get('price')
            
            if signal_type == 'buy' and quantity > 0:
                return Order(
                    id=f"signal_{symbol}_{int(time.time())}",
                    symbol=symbol,
                    side='buy',
                    order_type='market',
                    quantity=quantity,
                    price=price
                )
            elif signal_type == 'sell' and quantity > 0:
                return Order(
                    id=f"signal_{symbol}_{int(time.time())}",
                    symbol=symbol,
                    side='sell',
                    order_type='market',
                    quantity=quantity,
                    price=price
                )
            
            return None
            
        except Exception as e:
            logger.error(f"Error creating order from signal: {str(e)}")
            return None

    def get_stats(self) -> Dict[str, Any]:
        """
        Get trading statistics.
        
        Returns:
            Dictionary of trading statistics
        """
        return self.stats.copy()

    def get_order(self, order_id: str) -> Optional[Order]:
        """
        Get an order by ID.
        
        Args:
            order_id: Order ID to retrieve
            
        Returns:
            Order object or None if not found
        """
        return self.orders.get(order_id)

    def get_open_orders(self) -> List[Order]:
        """
        Get all open orders.
        
        Returns:
            List of open orders
        """
        return [order for order in self.orders.values() 
                if order.status in [OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED]]

    def get_filled_orders(self) -> List[Order]:
        """
        Get all filled orders.
        
        Returns:
            List of filled orders
        """
        return [order for order in self.orders.values() if order.status == OrderStatus.FILLED]