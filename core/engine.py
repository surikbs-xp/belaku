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

from core.strategy.base import BaseStrategy, Signal
from core.risk.manager import RiskManager
from core.data.handler import DataHandler
from exchanges.base import BaseExchange
from core.types import Order, PositionSide, OrderStatus, Position

logger = logging.getLogger(__name__)


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
        logger.info("[ENGINE] Starting trading engine")
        self.running = True
        
        # Initialize exchanges
        logger.info("[ENGINE] Connecting %d exchange(s)", len(self.exchanges))
        for exchange in self.exchanges.values():
            logger.info("[ENGINE] Connecting exchange: %s", exchange.name)
            await exchange.connect()
            logger.info("[ENGINE] Exchange connected: %s", exchange.name)
        
        # Start data handler
        logger.info("[ENGINE] Starting data handler")
        await self.data_handler.start()
        logger.info("[ENGINE] Data handler started")
        
        # Start main trading loop
        logger.info("[ENGINE] Launching trading loop task")
        asyncio.create_task(self._trading_loop())
        
        logger.info("[ENGINE] Trading engine started successfully")

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
        logger.info("[ORDER] Attempting to submit order %s for %s %s %s", order.id, order.side, order.quantity, order.symbol)
        try:
            # Validate order
            if not await self._validate_order(order):
                logger.error("[ORDER] Validation failed for %s", order.id)
                return False
            
            # Check risk management
            if not await self.risk_manager.check_order(order):
                logger.error("[ORDER] Risk check failed for order %s", order.id)
                return False
            
            # Submit to exchange
            exchange = self.exchanges.get(order.exchange)
            if not exchange:
                logger.error("[ORDER] Exchange %s not found for order %s", order.exchange, order.id)
                return False
            
            logger.info("[ORDER] Sending to exchange %s", exchange.name)
            exchange_order_id = await exchange.submit_order(order)
            order.order_id = exchange_order_id
            order.status = OrderStatus.SUBMITTED
            
            # Store order
            self.orders[order.id] = order
            self.order_history.append(order)
            
            logger.info("[ORDER] Submitted successfully: %s - %s %s %s", order.id, order.symbol, order.side, order.quantity)
            return True
            
        except Exception as e:
            logger.error("[ORDER] Error submitting order %s: %s", order.id, str(e))
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
        logger.info("[LOOP] Trading loop started")
        while self.running:
            try:
                # Get current market data
                logger.info("[LOOP] Fetching latest market snapshot")
                market_data = await self.data_handler.get_latest_snapshot()
                market_status = self.data_handler.is_market_active('NSE')
                logger.info("[MARKET] NSE market active=%s", market_status)
                logger.info("[LOOP] Snapshot received for %d symbol(s)", len(market_data))

                if not market_data:
                    if market_status:
                        logger.warning("[MARKET] No live price data yet; seeding debug candle feed")
                        await self.data_handler.seed_debug_market_data(symbol='NIFTY', exchange='NSE')
                        market_data = await self.data_handler.get_latest_snapshot()
                        logger.info("[LOOP] Snapshot after debug seed: %d symbol(s)", len(market_data))
                    else:
                        logger.info("[MARKET] Market is closed; strategy evaluation skipped")
                        await asyncio.sleep(self.config.get('loop_interval', 1.0))
                        continue
                
                # Execute strategies
                for strategy in self.strategies:
                    logger.info("[LOOP] Running strategy: %s", strategy.name)
                    signals = await strategy.analyze(market_data)
                    logger.info("[LOOP] Strategy %s produced %d signal(s)", strategy.name, len(signals))
                    await self._process_signals(signals)
                
                # Update positions
                logger.info("[LOOP] Refreshing positions from exchange")
                await self._update_positions()
                
                # Check for stop-loss and take-profit
                logger.info("[LOOP] Checking exit conditions")
                await self._check_exit_conditions()
                
                # Sleep for next iteration
                logger.info("[LOOP] Sleeping for %.2f seconds", self.config.get('loop_interval', 1.0))
                await asyncio.sleep(self.config.get('loop_interval', 1.0))
                
            except Exception as e:
                logger.error("[LOOP] Error in trading loop: %s", str(e))
                await asyncio.sleep(5.0)  # Wait before retrying

    async def _process_signals(self, signals: List[Signal]) -> None:
        """
        Process trading signals from strategies.
        
        Args:
            signals: List of trading signals
        """
        if not signals:
            logger.info("[SIGNAL] No signals to process")
            return

        for signal in signals:
            try:
                logger.info("[SIGNAL] Processing signal: %s %s @ %.2f strength=%.2f", signal.signal_type, signal.symbol, signal.price or 0.0, signal.strength)
                # Create order from signal
                order = self._create_order_from_signal(signal)
                if order:
                    logger.info("[SIGNAL] Created order %s from signal for %s", order.id, signal.symbol)
                    # Submit order
                    await self.submit_order(order)
                else:
                    logger.warning("[SIGNAL] Could not create order from signal for %s", signal.symbol)
            except Exception as e:
                logger.error("[SIGNAL] Error processing signal for %s: %s", signal.symbol if signal else 'unknown', str(e))

    async def _update_positions(self) -> None:
        """
        Update current positions from exchanges.

        Exchange payloads are keyed by ``EXCHANGE:SYMBOL`` and carry a signed net
        quantity, so the direction of a position comes from the sign.
        """
        for exchange in self.exchanges.values():
            exchange_positions = await exchange.get_positions()
            
            for position_key, position_data in exchange_positions.items():
                quantity = float(position_data['quantity'])
                if not quantity:
                    continue
                
                symbol = position_key.split(':', 1)[-1]
                average_price = float(position_data.get('average_price') or 0.0)
                current_price = float(position_data.get('last_price') or average_price)
                side = PositionSide.LONG if quantity > 0 else PositionSide.SHORT
                
                position = self.positions.get(symbol)
                if position:
                    # Update existing position
                    position.side = side
                    position.quantity = abs(quantity)
                    position.update_pnl(current_price)
                else:
                    # Create new position
                    position = Position(
                        symbol=symbol,
                        side=side,
                        quantity=abs(quantity),
                        entry_price=average_price or current_price,
                        current_price=current_price,
                        exchange=exchange.name
                    )
                    position.update_pnl(current_price)
                    self.positions[symbol] = position

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

    def _create_order_from_signal(self, signal: Signal) -> Optional[Order]:
        """
        Create an order from a trading signal.
        
        Args:
            signal: Trading signal
            
        Returns:
            Order object or None if signal cannot be converted
        """
        if signal.signal_type not in ('buy', 'sell') or not signal.quantity or signal.quantity <= 0:
            return None
        
        exchange = self._default_exchange_name()
        if exchange is None:
            logger.error(
                f"Cannot route signal for {signal.symbol}: register exactly one exchange"
            )
            return None
        
        return Order(
            id=f"signal_{signal.symbol}_{int(time.time())}",
            symbol=signal.symbol,
            side=signal.signal_type,
            order_type='market',
            quantity=signal.quantity,
            price=signal.price,
            exchange=exchange
        )

    def _default_exchange_name(self) -> Optional[str]:
        """Return the only registered exchange name, if there is exactly one."""
        if len(self.exchanges) == 1:
            return next(iter(self.exchanges))
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