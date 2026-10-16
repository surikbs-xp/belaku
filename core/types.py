"""
Shared data types for Belaku TradeBot.

This module contains shared data structures used across multiple components
of the trading bot to avoid circular imports.
"""

from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field
from enum import Enum


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
    timeframe: Optional[str] = None

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