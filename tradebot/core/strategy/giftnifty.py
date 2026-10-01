"""
GiftNifty strategy for Belaku TradeBot.

This module implements a trading strategy specifically designed for
GiftNifty events from Zerodha, focusing on automated trading based
on GiftNifty market events and signals.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import pandas as pd
import numpy as np

from .base import BaseStrategy, Signal
from ..engine import Order, PositionSide

logger = logging.getLogger(__name__)


@dataclass
class GiftNiftyEvent:
    """GiftNifty event data structure."""
    event_type: str  # 'gift', 'nifty', 'special'
    symbol: str
    timestamp: datetime
    data: Dict[str, Any]
    strength: float = 0.0
    price_impact: float = 0.0
    confidence: float = 0.0

    @property
    def is_gift_event(self) -> bool:
        """Check if event is a gift event."""
        return self.event_type == 'gift'

    @property
    def is_nifty_event(self) -> bool:
        """Check if event is a nifty event."""
        return self.event_type == 'nifty'

    @property
    def is_special_event(self) -> bool:
        """Check if event is a special event."""
        return self.event_type == 'special'


class GiftNiftyStrategy(BaseStrategy):
    """
    Trading strategy for GiftNifty events from Zerodha.
    
    This strategy monitors GiftNifty events from Zerodha and generates
    trading signals based on the event type, strength, and market impact.
    """

    def __init__(self, parameters: Dict[str, Any] = None):
        """
        Initialize the GiftNifty strategy.
        
        Args:
            parameters: Strategy parameters
        """
        default_parameters = {
            'min_signal_strength': 0.7,
            'min_confidence': 0.8,
            'max_position_size': 100000,
            'stop_loss_percentage': 0.02,
            'take_profit_percentage': 0.05,
            'cooldown_period': 300,  # 5 minutes
            'max_signals_per_hour': 10,
            'gift_event_weight': 1.0,
            'nifty_event_weight': 0.8,
            'special_event_weight': 0.9,
        }
        
        if parameters:
            default_parameters.update(parameters)
        
        super().__init__('GiftNifty', default_parameters)
        
        # Event tracking
        self.recent_events: List[GiftNiftyEvent] = []
        self.last_signal_time = None
        self.signals_this_hour = 0
        self.last_hour = datetime.now().hour
        
        # Market data tracking
        self.price_history: Dict[str, List[Tuple[datetime, float]]] = {}
        self.volatility_data: Dict[str, List[float]] = {}
        
        # Performance tracking
        self.strategy_stats = {
            'events_processed': 0,
            'signals_generated': 0,
            'successful_trades': 0,
            'failed_trades': 0,
            'total_pnl': 0.0,
            'max_drawdown': 0.0,
        }

    async def analyze(self, market_data: Dict[str, Any]) -> List[Signal]:
        """
        Analyze market data and generate trading signals based on GiftNifty events.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            List of trading signals
        """
        if not self.enabled:
            return []
        
        # Check if it's time to generate new signals
        current_time = datetime.now()
        if self.last_signal_time:
            time_diff = (current_time - self.last_signal_time).total_seconds()
            if time_diff < self.parameters['cooldown_period']:
                return []
        
        # Check signal limit per hour
        if current_time.hour != self.last_hour:
            self.signals_this_hour = 0
            self.last_hour = current_time.hour
        
        if self.signals_this_hour >= self.parameters['max_signals_per_hour']:
            logger.info(f"Reached maximum signals per hour ({self.signals_this_hour})")
            return []
        
        signals = []
        
        # Process recent GiftNifty events
        for event in self.recent_events:
            if not self._should_process_event(event):
                continue
            
            # Generate signal based on event
            signal = self._event_to_signal(event, market_data)
            if signal:
                signals.append(signal)
                self.increment_signals_generated()
                self.signals_this_hour += 1
        
        # Clear processed events
        self.recent_events.clear()
        
        # Update last signal time
        if signals:
            self.last_signal_time = current_time
        
        logger.info(f"Generated {len(signals)} signals from GiftNifty events")
        return signals

    async def on_order_filled(self, order: Order) -> None:
        """
        Handle order fill event.
        
        Args:
            order: Filled order
        """
        logger.info(f"Order filled: {order.id} - {order.symbol} {order.side} {order.quantity}")
        
        # Update strategy statistics
        self.strategy_stats['successful_trades'] += 1
        
        # Calculate P&L (simplified)
        if order.avg_fill_price:
            # This would be calculated based on entry and exit prices
            pnl = 0.0  # Placeholder
            self.strategy_stats['total_pnl'] += pnl

    async def on_position_updated(self, symbol: str, position: Dict[str, Any]) -> None:
        """
        Handle position update event.
        
        Args:
            symbol: Trading symbol
            position: Position data
        """
        logger.info(f"Position updated: {symbol} - {position}")
        
        # Update volatility data
        if symbol not in self.volatility_data:
            self.volatility_data[symbol] = []
        
        # Calculate volatility based on position changes
        if 'quantity' in position and 'price' in position:
            volatility = abs(position.get('price_change', 0)) / position['price'] if position['price'] > 0 else 0
            self.volatility_data[symbol].append(volatility)
            
            # Keep only recent volatility data
            if len(self.volatility_data[symbol]) > 100:
                self.volatility_data[symbol] = self.volatility_data[symbol][-100:]

    def add_event(self, event: GiftNiftyEvent) -> None:
        """
        Add a GiftNifty event to be processed.
        
        Args:
            event: GiftNifty event
        """
        self.recent_events.append(event)
        self.strategy_stats['events_processed'] += 1
        logger.info(f"Added GiftNifty event: {event.event_type} - {event.symbol}")

    def _should_process_event(self, event: GiftNiftyEvent) -> bool:
        """
        Check if an event should be processed for signal generation.
        
        Args:
            event: GiftNifty event
            
        Returns:
            True if event should be processed, False otherwise
        """
        # Check event type weight
        event_weights = {
            'gift': self.parameters['gift_event_weight'],
            'nifty': self.parameters['nifty_event_weight'],
            'special': self.parameters['special_event_weight']
        }
        
        weight = event_weights.get(event.event_type, 0.5)
        
        # Check minimum strength and confidence
        if event.strength < self.parameters['min_signal_strength']:
            return False
        
        if event.confidence < self.parameters['min_confidence']:
            return False
        
        # Check price impact
        if abs(event.price_impact) < 0.001:  # 0.1% minimum impact
            return False
        
        return True

    def _event_to_signal(self, event: GiftNiftyEvent, market_data: Dict[str, Any]) -> Optional[Signal]:
        """
        Convert a GiftNifty event to a trading signal.
        
        Args:
            event: GiftNifty event
            market_data: Market data dictionary
            
        Returns:
            Trading signal or None if event cannot be converted
        """
        try:
            # Determine signal type based on event
            if event.is_gift_event:
                signal_type = 'buy'
            elif event.is_nifty_event:
                # Nifty events could be buy or sell depending on context
                signal_type = 'buy'  # Default to buy for now
            elif event.is_special_event:
                # Special events could be either direction
                signal_type = 'buy'  # Default to buy for now
            else:
                return None
            
            # Calculate position size based on event strength and confidence
            base_size = self.parameters['max_position_size'] * event.strength * event.confidence
            
            # Adjust for price impact
            price_impact_factor = min(abs(event.price_impact) * 10, 1.0)
            position_size = base_size * (1 - price_impact_factor)
            
            # Get current price from market data
            symbol = event.symbol
            current_price = market_data.get(symbol, {}).get('price', 0)
            
            if current_price <= 0:
                logger.warning(f"Invalid price for {symbol}: {current_price}")
                return None
            
            # Calculate quantity
            quantity = position_size / current_price
            
            # Create signal
            signal = Signal(
                symbol=symbol,
                signal_type=signal_type,
                strength=event.strength * event.confidence,
                price=current_price,
                quantity=quantity,
                timeframe='1minute',
                metadata={
                    'event_type': event.event_type,
                    'event_data': event.data,
                    'price_impact': event.price_impact,
                    'confidence': event.confidence,
                    'strategy': self.name
                }
            )
            
            logger.info(f"Generated signal: {signal_type} {symbol} - strength: {signal.strength:.2f}, quantity: {quantity:.2f}")
            return signal
            
        except Exception as e:
            logger.error(f"Error converting event to signal: {str(e)}")
            return None

    def get_strategy_stats(self) -> Dict[str, Any]:
        """
        Get strategy statistics.
        
        Returns:
            Dictionary of strategy statistics
        """
        stats = self.get_stats()
        stats['strategy_stats'] = self.strategy_stats.copy()
        
        # Calculate additional metrics
        if self.strategy_stats['events_processed'] > 0:
            stats['success_rate'] = self.strategy_stats['successful_trades'] / (
                self.strategy_stats['successful_trades'] + self.strategy_stats['failed_trades']
            ) if (self.strategy_stats['successful_trades'] + self.strategy_stats['failed_trades']) > 0 else 0
        else:
            stats['success_rate'] = 0
        
        return stats

    def reset_hourly_stats(self) -> None:
        """Reset hourly statistics."""
        self.signals_this_hour = 0
        logger.info("Reset hourly statistics")