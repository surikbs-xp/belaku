"""
Base strategy class for Belaku TradeBot.

This module defines the abstract base class for all trading strategies,
providing a common interface for strategy implementation and execution.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field

from ..engine import Order, PositionSide

logger = logging.getLogger(__name__)


@dataclass
class Signal:
    """Trading signal data structure."""
    symbol: str
    signal_type: str  # 'buy', 'sell', 'hold'
    strength: float  # Signal strength (0.0 to 1.0)
    price: Optional[float] = None
    quantity: Optional[float] = None
    timeframe: str = "1minute"
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        """Check if signal is a buy signal."""
        return self.signal_type == 'buy'

    @property
    def is_sell(self) -> bool:
        """Check if signal is a sell signal."""
        return self.signal_type == 'sell'

    @property
    def is_hold(self) -> bool:
        """Check if signal is a hold signal."""
        return self.signal_type == 'hold'


class BaseStrategy(ABC):
    """
    Abstract base class for trading strategies.
    
    This class defines the interface that all trading strategies must follow,
    providing methods for analysis, signal generation, and strategy management.
    """

    def __init__(self, name: str, parameters: Dict[str, Any]):
        """
        Initialize the strategy.
        
        Args:
            name: Strategy name
            parameters: Strategy parameters
        """
        self.name = name
        self.parameters = parameters
        self.enabled = True
        self.signals_generated = 0
        self.last_analysis_time = None

    @abstractmethod
    async def analyze(self, market_data: Dict[str, Any]) -> List[Signal]:
        """
        Analyze market data and generate trading signals.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            List of trading signals
        """
        pass

    @abstractmethod
    async def on_order_filled(self, order: Order) -> None:
        """
        Handle order fill event.
        
        Args:
            order: Filled order
        """
        pass

    @abstractmethod
    async def on_position_updated(self, symbol: str, position: Dict[str, Any]) -> None:
        """
        Handle position update event.
        
        Args:
            symbol: Trading symbol
            position: Position data
        """
        pass

    def enable(self) -> None:
        """Enable the strategy."""
        self.enabled = True
        logger.info(f"Strategy {self.name} enabled")

    def disable(self) -> None:
        """Disable the strategy."""
        self.enabled = False
        logger.info(f"Strategy {self.name} disabled")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get strategy statistics.
        
        Returns:
            Dictionary of strategy statistics
        """
        return {
            'name': self.name,
            'enabled': self.enabled,
            'signals_generated': self.signals_generated,
            'last_analysis_time': self.last_analysis_time,
            'parameters': self.parameters
        }

    def increment_signals_generated(self) -> None:
        """Increment signals generated counter."""
        self.signals_generated += 1
        self.last_analysis_time = datetime.now()