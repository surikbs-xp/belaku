"""
Base exchange class for Belaku TradeBot.

This module defines the abstract base class for all exchange integrations,
providing a common interface for trading operations across different exchanges.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Dict, List, Optional, Any

from ..core.engine import Order

logger = logging.getLogger(__name__)


class BaseExchange(ABC):
    """
    Abstract base class for exchange integrations.
    
    This class defines the interface that all exchange implementations must follow,
    ensuring consistency across different trading platforms.
    """

    def __init__(self, name: str):
        """
        Initialize the base exchange.
        
        Args:
            name: Exchange name (e.g., 'BINANCE', 'COINBASE', 'ZERODHA')
        """
        self.name = name
        self.connected = False
        self.last_ping = None
        self.error_count = 0
        self.max_errors = 10

    @abstractmethod
    async def connect(self) -> None:
        """
        Connect to the exchange.
        
        This method should establish a connection to the exchange API
        and WebSocket for real-time data.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Disconnect from the exchange.
        
        This method should gracefully close all connections to the exchange.
        """
        pass

    @abstractmethod
    async def submit_order(self, order: Order) -> str:
        """
        Submit an order to the exchange.
        
        Args:
            order: Order to submit
            
        Returns:
            Order ID
        """
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order on the exchange.
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if order was canceled successfully, False otherwise
        """
        pass

    @abstractmethod
    async def get_positions(self) -> Dict[str, Any]:
        """
        Get positions from the exchange.
        
        Returns:
            Dictionary of positions
        """
        pass

    async def update_positions(self) -> None:
        """
        Update positions from the exchange.
        
        This method is called by the trading engine to update positions.
        Default implementation does nothing - subclasses should override.
        """
        pass

    async def get_historical_data(self, symbol: str, exchange: str, start_time: datetime, end_time: datetime, timeframe: str = "1minute") -> List[Dict[str, Any]]:
        """
        Get historical data from the exchange.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            start_time: Start time
            end_time: End time
            timeframe: Timeframe (1minute, 5minute, 15minute, 1hour, 1day)
            
        Returns:
            List of historical data candles
        """
        # Default implementation - subclasses should override
        return []

    async def ping(self) -> bool:
        """
        Ping the exchange to check connectivity.
        
        Returns:
            True if ping successful, False otherwise
        """
        try:
            # Default implementation - subclasses should override
            self.last_ping = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Ping failed for {self.name}: {str(e)}")
            self.error_count += 1
            return False

    def is_healthy(self) -> bool:
        """
        Check if the exchange connection is healthy.
        
        Returns:
            True if exchange is healthy, False otherwise
        """
        if self.error_count >= self.max_errors:
            return False
        
        # Check if last ping was recent (within 5 minutes)
        if self.last_ping:
            return (datetime.now() - self.last_ping).total_seconds() < 300
        
        return self.connected

    def log_error(self, error: Exception) -> None:
        """
        Log an error and increment error count.
        
        Args:
            error: Exception that occurred
        """
        logger.error(f"Error in {self.name}: {str(error)}")
        self.error_count += 1

    def reset_error_count(self) -> None:
        """
        Reset error count.
        """
        self.error_count = 0