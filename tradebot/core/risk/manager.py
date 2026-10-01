"""
Risk manager for Belaku TradeBot.

This module implements risk management functionality for the trading bot,
including position sizing, stop-loss, take-profit, and portfolio optimization.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
import numpy as np

from ..engine import Order, PositionSide

logger = logging.getLogger(__name__)


@dataclass
class RiskMetrics:
    """Risk metrics data structure."""
    symbol: str
    exchange: str
    timestamp: datetime = field(default_factory=datetime.now)
    volatility: float = 0.0
    beta: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    var_95: float = 0.0  # Value at Risk at 95% confidence
    expected_shortfall: float = 0.0
    liquidity_score: float = 0.0

    @property
    def risk_score(self) -> float:
        """Calculate overall risk score (0-1, higher is riskier)."""
        # Simple risk score calculation
        risk_factors = [
            min(self.volatility / 0.5, 1.0),  # Cap at 50% volatility
            min(abs(self.beta), 1.0),
            min(self.max_drawdown / 0.2, 1.0),  # Cap at 20% drawdown
            min(self.var_95 / 0.1, 1.0),  # Cap at 10% VaR
        ]
        return sum(risk_factors) / len(risk_factors)


@dataclass
class PositionSize:
    """Position size data structure."""
    symbol: str
    exchange: str
    quantity: float
    entry_price: float
    stop_loss_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    risk_amount: float = 0.0
    expected_return: float = 0.0
    risk_reward_ratio: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def risk_per_share(self) -> float:
        """Calculate risk per share."""
        if self.stop_loss_price is None:
            return 0.0
        return abs(self.entry_price - self.stop_loss_price)

    @property
    def total_risk(self) -> float:
        """Calculate total risk in currency."""
        return self.risk_per_share * self.quantity

    @property
    def expected_profit(self) -> float:
        """Calculate expected profit."""
        if self.take_profit_price is None:
            return 0.0
        return abs(self.take_profit_price - self.entry_price) * self.quantity


class RiskManager:
    """
    Risk manager for managing trading risks.
    
    This class implements comprehensive risk management functionality,
    including position sizing, stop-loss, take-profit, portfolio optimization,
    and risk metrics calculation.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the risk manager.
        
        Args:
            config: Configuration dictionary containing risk management settings
        """
        self.config = config
        self.risk_metrics: Dict[str, RiskMetrics] = {}
        self.position_sizes: Dict[str, PositionSize] = {}
        self.risk_limits: Dict[str, float] = {}
        self.portfolio_risk: Dict[str, Any] = {}
        self.running = False
        
        # Risk calculation parameters
        self.risk_free_rate = 0.02  # 2% risk-free rate
        self.market_volatility = 0.2  # 20% market volatility
        self.correlation_matrix: Dict[str, Dict[str, float]] = {}
        
        # Performance tracking
        self.stats = {
            'risk_checks_passed': 0,
            'risk_checks_failed': 0,
            'position_sizing_decisions': 0,
            'risk_alerts_triggered': 0,
            'portfolio_rebalances': 0,
        }
        
        logger.info("Risk manager initialized")

    async def start(self) -> None:
        """
        Start the risk manager.
        
        This method initializes risk monitoring and starts risk calculation loops.
        """
        logger.info("Starting risk manager")
        self.running = True
        
        # Initialize risk limits
        await self._initialize_risk_limits()
        
        # Start risk monitoring loop
        asyncio.create_task(self._risk_monitoring_loop())
        
        logger.info("Risk manager started successfully")

    async def stop(self) -> None:
        """
        Stop the risk manager.
        
        This method gracefully shuts down risk monitoring.
        """
        logger.info("Stopping risk manager")
        self.running = False
        
        logger.info("Risk manager stopped")

    async def check_order(self, order: Order) -> bool:
        """
        Check if an order meets risk criteria.
        
        Args:
            order: Order to check
            
        Returns:
            True if order is allowed, False otherwise
        """
        try:
            # Check individual risk criteria
            if not await self._check_order_size(order):
                logger.warning(f"Order size check failed for {order.symbol}")
                return False
            
            if not await self._check_position_limit(order):
                logger.warning(f"Position limit check failed for {order.symbol}")
                return False
            
            if not await self._check_volatility_limit(order):
                logger.warning(f"Volatility limit check failed for {order.symbol}")
                return False
            
            if not await self._check_correlation_limit(order):
                logger.warning(f"Correlation limit check failed for {order.symbol}")
                return False
            
            # Update statistics
            self.stats['risk_checks_passed'] += 1
            
            return True
            
        except Exception as e:
            logger.error(f"Error checking order {order.id}: {str(e)}")
            self.stats['risk_checks_failed'] += 1
            return False

    async def calculate_position_size(self, symbol: str, exchange: str, entry_price: float, 
                                    stop_loss_price: Optional[float] = None, 
                                    take_profit_price: Optional[float] = None,
                                    account_balance: float = 100000.0) -> PositionSize:
        """
        Calculate optimal position size for a trade.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            entry_price: Entry price
            stop_loss_price: Stop loss price
            take_profit_price: Take profit price
            account_balance: Account balance
            
        Returns:
            PositionSize object
        """
        try:
            # Get risk metrics for the symbol
            risk_metrics = self.risk_metrics.get(f"{symbol}_{exchange}")
            
            # Calculate position size based on risk management rules
            if stop_loss_price is None:
                # Use default stop loss percentage
                stop_loss_percentage = self.config.get('stop_loss_percentage', 0.02)
                stop_loss_price = entry_price * (1 - stop_loss_percentage)
            
            if take_profit_price is None:
                # Use default take profit percentage
                take_profit_percentage = self.config.get('take_profit_percentage', 0.05)
                take_profit_price = entry_price * (1 + take_profit_percentage)
            
            # Calculate risk per share
            risk_per_share = abs(entry_price - stop_loss_price)
            
            # Calculate position size based on risk tolerance
            risk_tolerance = self.config.get('risk_tolerance', 0.02)  # 2% risk per trade
            max_risk_amount = account_balance * risk_tolerance
            
            # Calculate position size
            quantity = max_risk_amount / risk_per_share if risk_per_share > 0 else 0
            
            # Apply position size limits
            max_position_size = self.config.get('max_position_size', 100000)
            position_value = quantity * entry_price
            
            if position_value > max_position_size:
                quantity = max_position_size / entry_price
            
            # Calculate expected return and risk-reward ratio
            expected_return = abs(take_profit_price - entry_price) * quantity
            risk_reward_ratio = expected_return / (risk_per_share * quantity) if risk_per_share > 0 else 0
            
            # Create position size object
            position_size = PositionSize(
                symbol=symbol,
                exchange=exchange,
                quantity=quantity,
                entry_price=entry_price,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                risk_amount=risk_per_share * quantity,
                expected_return=expected_return,
                risk_reward_ratio=risk_reward_ratio
            )
            
            # Store position size
            self.position_sizes[f"{symbol}_{exchange}"] = position_size
            
            # Update statistics
            self.stats['position_sizing_decisions'] += 1
            
            logger.info(f"Calculated position size for {symbol}: {quantity:.2f} shares")
            return position_size
            
        except Exception as e:
            logger.error(f"Error calculating position size for {symbol}: {str(e)}")
            raise

    async def update_risk_metrics(self, symbol: str, exchange: str, price_data: Dict[str, Any]) -> None:
        """
        Update risk metrics for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            price_data: Price data dictionary
        """
        try:
            # Get existing risk metrics
            key = f"{symbol}_{exchange}"
            risk_metrics = self.risk_metrics.get(key)
            
            if risk_metrics is None:
                risk_metrics = RiskMetrics(symbol=symbol, exchange=exchange)
                self.risk_metrics[key] = risk_metrics
            
            # Update volatility
            if 'prices' in price_data and len(price_data['prices']) >= 2:
                prices = price_data['prices']
                returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
                
                if returns:
                    volatility = np.std(returns) * np.sqrt(252)  # Annualized volatility
                    risk_metrics.volatility = volatility
            
            # Update other metrics based on price data
            if 'volume' in price_data:
                # Calculate liquidity score based on volume
                risk_metrics.liquidity_score = min(price_data['volume'] / 1000000, 1.0)  # Cap at 1M volume
            
            # Calculate beta (simplified)
            if 'beta' in price_data:
                risk_metrics.beta = price_data['beta']
            
            # Calculate Sharpe ratio (simplified)
            if 'returns' in price_data and 'std_dev' in price_data:
                risk_metrics.sharpe_ratio = (price_data['returns'] - self.risk_free_rate) / price_data['std_dev']
            
            # Calculate VaR (Value at Risk) - simplified
            if 'prices' in price_data and len(price_data['prices']) >= 20:
                prices = price_data['prices']
                returns = [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]
                
                if returns:
                    var_95 = np.percentile(returns, 5) * np.sqrt(252)  # 95% VaR annualized
                    risk_metrics.var_95 = abs(var_95)
                    
                    # Calculate expected shortfall
                    expected_shortfall = np.mean([r for r in returns if r < var_95]) * np.sqrt(252)
                    risk_metrics.expected_shortfall = abs(expected_shortfall)
            
            # Calculate max drawdown (simplified)
            if 'prices' in price_data and len(price_data['prices']) >= 2:
                prices = price_data['prices']
                running_max = np.maximum.accumulate(prices)
                drawdowns = (prices - running_max) / running_max
                risk_metrics.max_drawdown = abs(np.min(drawdowns))
            
            logger.info(f"Updated risk metrics for {symbol}: volatility={risk_metrics.volatility:.2%}")
            
        except Exception as e:
            logger.error(f"Error updating risk metrics for {symbol}: {str(e)}")

    async def check_portfolio_risk(self, positions: Dict[str, Any]) -> Dict[str, Any]:
        """
        Check overall portfolio risk.
        
        Args:
            positions: Dictionary of positions
            
        Returns:
            Dictionary of portfolio risk metrics
        """
        try:
            # Calculate portfolio metrics
            total_value = sum(pos.get('quantity', 0) * pos.get('price', 0) for pos in positions.values())
            
            # Calculate portfolio volatility (simplified)
            portfolio_volatility = 0.0
            for symbol, position in positions.items():
                risk_metrics = self.risk_metrics.get(f"{symbol}_{position.get('exchange', 'UNKNOWN')}")
                if risk_metrics:
                    portfolio_volatility += risk_metrics.volatility * (position.get('quantity', 0) * position.get('price', 0) / total_value)
            
            # Calculate portfolio beta
            portfolio_beta = 0.0
            for symbol, position in positions.items():
                risk_metrics = self.risk_metrics.get(f"{symbol}_{position.get('exchange', 'UNKNOWN')}")
                if risk_metrics:
                    portfolio_beta += risk_metrics.beta * (position.get('quantity', 0) * position.get('price', 0) / total_value)
            
            # Calculate portfolio Sharpe ratio
            portfolio_sharpe = (self.config.get('expected_return', 0.1) - self.risk_free_rate) / portfolio_volatility if portfolio_volatility > 0 else 0
            
            # Calculate portfolio VaR
            portfolio_var = 0.0
            for symbol, position in positions.items():
                risk_metrics = self.risk_metrics.get(f"{symbol}_{position.get('exchange', 'UNKNOWN')}")
                if risk_metrics:
                    portfolio_var += risk_metrics.var_95 * (position.get('quantity', 0) * position.get('price', 0) / total_value)
            
            # Calculate portfolio expected shortfall
            portfolio_expected_shortfall = 0.0
            for symbol, position in positions.items():
                risk_metrics = self.risk_metrics.get(f"{symbol}_{position.get('exchange', 'UNKNOWN')}")
                if risk_metrics:
                    portfolio_expected_shortfall += risk_metrics.expected_shortfall * (position.get('quantity', 0) * position.get('price', 0) / total_value)
            
            # Calculate portfolio max drawdown
            portfolio_max_drawdown = 0.0
            for symbol, position in positions.items():
                risk_metrics = self.risk_metrics.get(f"{symbol}_{position.get('exchange', 'UNKNOWN')}")
                if risk_metrics:
                    portfolio_max_drawdown = max(portfolio_max_drawdown, risk_metrics.max_drawdown)
            
            # Create portfolio risk metrics
            portfolio_risk = {
                'total_value': total_value,
                'portfolio_volatility': portfolio_volatility,
                'portfolio_beta': portfolio_beta,
                'portfolio_sharpe_ratio': portfolio_sharpe,
                'portfolio_var_95': portfolio_var,
                'portfolio_expected_shortfall': portfolio_expected_shortfall,
                'portfolio_max_drawdown': portfolio_max_drawdown,
                'number_of_positions': len(positions),
                'concentration_risk': self._calculate_concentration_risk(positions),
                'diversification_score': self._calculate_diversification_score(positions),
            }
            
            # Store portfolio risk
            self.portfolio_risk = portfolio_risk
            
            # Check if portfolio risk exceeds limits
            await self._check_portfolio_risk_limits(portfolio_risk)
            
            logger.info(f"Portfolio risk calculated: volatility={portfolio_volatility:.2%}, beta={portfolio_beta:.2f}")
            return portfolio_risk
            
        except Exception as e:
            logger.error(f"Error checking portfolio risk: {str(e)}")
            return {}

    async def rebalance_portfolio(self, positions: Dict[str, Any], account_balance: float) -> List[PositionSize]:
        """
        Rebalance portfolio based on risk management rules.
        
        Args:
            positions: Current positions
            account_balance: Account balance
            
        Returns:
            List of new position sizes
        """
        try:
            # Calculate target allocation based on risk metrics
            target_allocations = await self._calculate_target_allocations(positions)
            
            # Generate rebalancing recommendations
            rebalancing_actions = []
            
            for symbol, position in positions.items():
                current_value = position.get('quantity', 0) * position.get('price', 0)
                target_value = account_balance * target_allocations.get(symbol, 0)
                
                if abs(current_value - target_value) / account_balance > 0.01:  # 1% threshold
                    # Generate rebalancing action
                    if target_value > current_value:
                        # Buy more
                        additional_value = target_value - current_value
                        entry_price = position.get('price', 0)
                        if entry_price > 0:
                            quantity = additional_value / entry_price
                            position_size = await self.calculate_position_size(
                                symbol=symbol,
                                exchange=position.get('exchange', 'UNKNOWN'),
                                entry_price=entry_price,
                                account_balance=account_balance
                            )
                            rebalancing_actions.append(position_size)
                    else:
                        # Sell some
                        excess_value = current_value - target_value
                        entry_price = position.get('price', 0)
                        if entry_price > 0:
                            quantity = excess_value / entry_price
                            # Create position size for selling
                            position_size = PositionSize(
                                symbol=symbol,
                                exchange=position.get('exchange', 'UNKNOWN'),
                                quantity=-quantity,  # Negative quantity indicates selling
                                entry_price=entry_price,
                                stop_loss_price=entry_price * 0.98,  # 2% stop loss
                                take_profit_price=entry_price * 1.02,  # 2% take profit
                                risk_amount=entry_price * 0.02 * quantity,
                                expected_return=entry_price * 0.02 * quantity,
                                risk_reward_ratio=1.0
                            )
                            rebalancing_actions.append(position_size)
            
            # Update statistics
            self.stats['portfolio_rebalances'] += 1
            
            logger.info(f"Generated {len(rebalancing_actions)} rebalancing actions")
            return rebalancing_actions
            
        except Exception as e:
            logger.error(f"Error rebalancing portfolio: {str(e)}")
            return []

    async def _initialize_risk_limits(self) -> None:
        """
        Initialize risk limits.
        """
        self.risk_limits = {
            'max_position_size': self.config.get('max_position_size', 100000),
            'max_portfolio_volatility': self.config.get('max_portfolio_volatility', 0.3),
            'max_portfolio_beta': self.config.get('max_portfolio_beta', 2.0),
            'max_portfolio_drawdown': self.config.get('max_portfolio_drawdown', 0.2),
            'max_portfolio_var': self.config.get('max_portfolio_var', 0.1),
            'max_correlation': self.config.get('max_correlation', 0.7),
        }
        
        logger.info("Initialized risk limits")

    async def _check_order_size(self, order: Order) -> bool:
        """
        Check if order size is within limits.
        
        Args:
            order: Order to check
            
        Returns:
            True if order size is allowed, False otherwise
        """
        # Calculate order value
        order_value = order.quantity * (order.price or 0)
        
        # Check against max position size
        max_position_size = self.risk_limits.get('max_position_size', 100000)
        
        return order_value <= max_position_size

    async def _check_position_limit(self, order: Order) -> bool:
        """
        Check if position limit is not exceeded.
        
        Args:
            order: Order to check
            
        Returns:
            True if position limit is not exceeded, False otherwise
        """
        # This would check against current positions
        # For now, return True
        return True

    async def _check_volatility_limit(self, order: Order) -> bool:
        """
        Check if volatility limit is not exceeded.
        
        Args:
            order: Order to check
            
        Returns:
            True if volatility limit is not exceeded, False otherwise
        """
        # Get risk metrics for the symbol
        key = f"{order.symbol}_{order.exchange}"
        risk_metrics = self.risk_metrics.get(key)
        
        if risk_metrics is None:
            return True
        
        # Check against max volatility limit
        max_volatility = self.risk_limits.get('max_portfolio_volatility', 0.3)
        
        return risk_metrics.volatility <= max_volatility

    async def _check_correlation_limit(self, order: Order) -> bool:
        """
        Check if correlation limit is not exceeded.
        
        Args:
            order: Order to check
            
        Returns:
            True if correlation limit is not exceeded, False otherwise
        """
        # This would check correlation with existing positions
        # For now, return True
        return True

    async def _check_portfolio_risk_limits(self, portfolio_risk: Dict[str, Any]) -> None:
        """
        Check if portfolio risk exceeds limits and trigger alerts.
        
        Args:
            portfolio_risk: Portfolio risk metrics
        """
        # Check each risk metric against limits
        for metric, limit in self.risk_limits.items():
            if metric == 'max_portfolio_volatility' and portfolio_risk.get('portfolio_volatility', 0) > limit:
                await self._trigger_risk_alert(f"Portfolio volatility exceeded: {portfolio_risk['portfolio_volatility']:.2%}")
            
            elif metric == 'max_portfolio_beta' and portfolio_risk.get('portfolio_beta', 0) > limit:
                await self._trigger_risk_alert(f"Portfolio beta exceeded: {portfolio_risk['portfolio_beta']:.2f}")
            
            elif metric == 'max_portfolio_drawdown' and portfolio_risk.get('portfolio_max_drawdown', 0) > limit:
                await self._trigger_risk_alert(f"Portfolio max drawdown exceeded: {portfolio_risk['portfolio_max_drawdown']:.2%}")
            
            elif metric == 'max_portfolio_var' and portfolio_risk.get('portfolio_var_95', 0) > limit:
                await self._trigger_risk_alert(f"Portfolio VaR exceeded: {portfolio_risk['portfolio_var_95']:.2%}")

    async def _trigger_risk_alert(self, message: str) -> None:
        """
        Trigger a risk alert.
        
        Args:
            message: Alert message
        """
        logger.warning(f"Risk alert: {message}")
        self.stats['risk_alerts_triggered'] += 1
        
        # In a real implementation, this would send alerts via email, Slack, etc.

    async def _calculate_concentration_risk(self, positions: Dict[str, Any]) -> float:
        """
        Calculate concentration risk.
        
        Args:
            positions: Dictionary of positions
            
        Returns:
            Concentration risk score (0-1, higher is more concentrated)
        """
        if not positions:
            return 0.0
        
        # Calculate total portfolio value
        total_value = sum(pos.get('quantity', 0) * pos.get('price', 0) for pos in positions.values())
        
        if total_value == 0:
            return 0.0
        
        # Calculate Herfindahl-Hirschman Index (HHI)
        hhi = sum((pos.get('quantity', 0) * pos.get('price', 0) / total_value) ** 2 for pos in positions.values())
        
        return hhi

    async def _calculate_diversification_score(self, positions: Dict[str, Any]) -> float:
        """
        Calculate diversification score.
        
        Args:
            positions: Dictionary of positions
            
        Returns:
            Diversification score (0-1, higher is more diversified)
        """
        if not positions:
            return 0.0
        
        # Calculate number of unique symbols
        unique_symbols = len(set(pos.get('symbol') for pos in positions.values()))
        
        # Calculate number of unique exchanges
        unique_exchanges = len(set(pos.get('exchange') for pos in positions.values()))
        
        # Calculate diversification score
        diversification_score = min(unique_symbols / 10, 1.0) * 0.7 + min(unique_exchanges / 3, 1.0) * 0.3
        
        return diversification_score

    async def _calculate_target_allocations(self, positions: Dict[str, Any]) -> Dict[str, float]:
        """
        Calculate target allocations based on risk metrics.
        
        Args:
            positions: Dictionary of positions
            
        Returns:
            Dictionary of target allocations
        """
        # Calculate total portfolio value
        total_value = sum(pos.get('quantity', 0) * pos.get('price', 0) for pos in positions.values())
        
        if total_value == 0:
            return {}
        
        # Calculate target allocations based on risk metrics
        target_allocations = {}
        
        for symbol, position in positions.items():
            # Get risk metrics for the symbol
            key = f"{symbol}_{position.get('exchange', 'UNKNOWN')}"
            risk_metrics = self.risk_metrics.get(key)
            
            if risk_metrics:
                # Calculate allocation based on inverse of risk
                allocation = (1.0 / (risk_metrics.risk_score + 0.1)) / sum(1.0 / (self.risk_metrics.get(f"{s}_{position.get('exchange', 'UNKNOWN')}", RiskMetrics(symbol=s, exchange=position.get('exchange', 'UNKNOWN'))).risk_score + 0.1) for s, pos in positions.items())
                target_allocations[symbol] = allocation
            else:
                # Equal allocation if no risk metrics
                target_allocations[symbol] = 1.0 / len(positions)
        
        return target_allocations

    async def _risk_monitoring_loop(self) -> None:
        """
        Main risk monitoring loop.
        
        This method continuously monitors risk metrics and updates risk limits.
        """
        while self.running:
            try:
                # Update risk metrics (in a real implementation, this would get data from exchanges)
                # For now, just sleep
                await asyncio.sleep(60.0)  # Update every minute
                
            except Exception as e:
                logger.error(f"Error in risk monitoring loop: {str(e)}")
                await asyncio.sleep(60.0)  # Wait before retrying

    def get_stats(self) -> Dict[str, Any]:
        """
        Get risk manager statistics.
        
        Returns:
            Dictionary of statistics
        """
        return self.stats.copy()

    def get_risk_metrics(self, symbol: str, exchange: str) -> Optional[RiskMetrics]:
        """
        Get risk metrics for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            
        Returns:
            RiskMetrics object or None if not found
        """
        key = f"{symbol}_{exchange}"
        return self.risk_metrics.get(key)

    def get_position_sizes(self, symbol: str, exchange: str) -> Optional[PositionSize]:
        """
        Get position size for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            
        Returns:
            PositionSize object or None if not found
        """
        key = f"{symbol}_{exchange}"
        return self.position_sizes.get(key)