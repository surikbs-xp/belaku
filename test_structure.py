"""
Test script to verify Belaku TradeBot structure.

This script tests the basic structure and imports of the Belaku TradeBot
system without requiring external API connections.
"""

import sys
import asyncio
from unittest.mock import Mock, AsyncMock

# Test imports
def test_imports():
    """Test that all modules can be imported correctly."""
    print("Testing imports...")
    
    try:
        from core.types import Order, PositionSide, OrderStatus, Position
        print("✅ core.types imported successfully")
    except ImportError as e:
        print(f"❌ core.types import failed: {e}")
        return False
    
    try:
        from core.engine import TradingEngine
        print("✅ core.engine imported successfully")
    except ImportError as e:
        print(f"❌ core.engine import failed: {e}")
        return False
    
    try:
        from core.strategy.base import BaseStrategy, Signal
        print("✅ core.strategy.base imported successfully")
    except ImportError as e:
        print(f"❌ core.strategy.base import failed: {e}")
        return False
    
    try:
        from core.strategy.giftnifty import GiftNiftyStrategy
        print("✅ core.strategy.giftnifty imported successfully")
    except ImportError as e:
        print(f"❌ core.strategy.giftnifty import failed: {e}")
        return False

    try:
        from core.strategy.supertrend_divergence import SuperTrendWeightedDivergenceStrategy
        print("✅ core.strategy.supertrend_divergence imported successfully")
    except ImportError as e:
        print(f"❌ core.strategy.supertrend_divergence import failed: {e}")
        return False
    
    try:
        from core.risk.manager import RiskManager
        print("✅ core.risk.manager imported successfully")
    except ImportError as e:
        print(f"❌ core.risk.manager import failed: {e}")
        return False
    
    try:
        from core.data.handler import DataHandler
        print("✅ core.data.handler imported successfully")
    except ImportError as e:
        print(f"❌ core.data.handler import failed: {e}")
        return False
    
    try:
        from exchanges.base import BaseExchange
        print("✅ exchanges.base imported successfully")
    except ImportError as e:
        print(f"❌ exchanges.base import failed: {e}")
        return False
    
    try:
        from exchanges.zerodha import ZerodhaExchange
        print("✅ exchanges.zerodha imported successfully")
    except ImportError as e:
        print(f"❌ exchanges.zerodha import failed: {e}")
        return False
    
    return True


async def test_basic_functionality():
    """Test basic functionality without external dependencies."""
    print("\nTesting basic functionality...")
    
    try:
        from core.types import Order, OrderStatus
        from core.engine import TradingEngine
        
        # Create a mock config
        config = {
            'loop_interval': 1.0,
            'max_orders_per_second': 10,
            'max_positions': 100,
        }
        
        # Initialize trading engine
        engine = TradingEngine(config)
        print("✅ TradingEngine initialized successfully")
        
        # Test order creation
        order = Order(
            id="test_order_1",
            symbol="RELIANCE",
            side="buy",
            order_type="market",
            quantity=10.0
        )
        print("✅ Order created successfully")
        
        # Test order properties
        assert order.is_cancelable == True
        assert order.is_filled == False
        print("✅ Order properties work correctly")
        
        return True
        
    except Exception as e:
        print(f"❌ Basic functionality test failed: {e}")
        return False


async def test_strategy_functionality():
    """Test strategy functionality."""
    print("\nTesting strategy functionality...")
    
    try:
        from core.strategy.giftnifty import GiftNiftyStrategy
        
        # Create strategy with mock parameters
        parameters = {
            'min_signal_strength': 0.7,
            'min_confidence': 0.8,
            'max_position_size': 100000,
        }
        
        strategy = GiftNiftyStrategy(parameters)
        print("✅ GiftNiftyStrategy initialized successfully")
        
        # Test strategy properties
        assert strategy.name == 'GiftNifty'
        assert strategy.enabled == True
        print("✅ Strategy properties work correctly")

        from core.strategy.supertrend_divergence import SuperTrendWeightedDivergenceStrategy
        st_strategy = SuperTrendWeightedDivergenceStrategy({
            'atr_period': 10,
            'multiplier': 3.0,
            'rsi_period': 14,
            'rsi_weight': 0.6,
            'macd_weight': 0.4,
            'min_strength': 0.55,
        })

        import numpy as np
        rising_prices = np.arange(20.0, 40.0)
        falling_prices = np.arange(40.0, 20.0, -1.0)
        psar_bullish, bull_stop = st_strategy._parabolic_sar_direction(
            rising_prices,
            rising_prices - 1,
            rising_prices - 0.2,
        )
        psar_bearish, bear_stop = st_strategy._parabolic_sar_direction(
            falling_prices,
            falling_prices - 1,
            falling_prices - 0.8,
        )
        assert psar_bullish == 1 and bull_stop < rising_prices[-1]
        assert psar_bearish == -1 and bear_stop > falling_prices[-1]

        rising_candles = [
            {'open': price - 0.2, 'high': price + 1, 'low': price - 1, 'close': price}
            for price in rising_prices
        ]
        falling_candles = [
            {'open': price - 0.2, 'high': price + 1, 'low': price - 1, 'close': price}
            for price in falling_prices
        ]
        for trend_direction, prices, expected_signal in (
            (1, rising_candles, 'buy'),
            (-1, falling_candles, 'sell'),
            (-1, rising_candles, None),
        ):
            confirmation_strategy = SuperTrendWeightedDivergenceStrategy({
                'min_strength': 1.0,
                'cooldown_period': 0,
            })
            confirmation_strategy._supertrend_direction = lambda closes, highs, lows: (trend_direction, 0.0)
            confirmation_strategy._weighted_divergence = lambda closes: 0.0
            confirmation_signals = await confirmation_strategy.analyze(
                {'GIFT NIFTY': {'candles': prices}}
            )
            if expected_signal is None:
                assert confirmation_signals == []
            else:
                assert confirmation_signals[0].signal_type == expected_signal

        sample_series = [
            {'open': 100.0, 'high': 101.0, 'low': 99.5, 'close': 100.8},
            {'open': 100.8, 'high': 101.5, 'low': 100.2, 'close': 101.2},
            {'open': 101.2, 'high': 102.0, 'low': 100.9, 'close': 101.8},
            {'open': 101.8, 'high': 102.2, 'low': 101.0, 'close': 102.4},
            {'open': 102.4, 'high': 102.8, 'low': 101.5, 'close': 102.1},
            {'open': 102.1, 'high': 102.6, 'low': 101.7, 'close': 102.5},
            {'open': 102.5, 'high': 103.1, 'low': 101.9, 'close': 103.0},
            {'open': 103.0, 'high': 103.6, 'low': 102.2, 'close': 103.2},
            {'open': 103.2, 'high': 103.8, 'low': 102.9, 'close': 103.4},
            {'open': 103.4, 'high': 103.9, 'low': 102.8, 'close': 103.1},
            {'open': 103.1, 'high': 103.7, 'low': 102.5, 'close': 103.6},
            {'open': 103.6, 'high': 104.1, 'low': 103.0, 'close': 104.0},
            {'open': 104.0, 'high': 104.5, 'low': 103.2, 'close': 104.2},
            {'open': 104.2, 'high': 104.8, 'low': 103.5, 'close': 104.6},
        ]
        signals = await st_strategy.analyze({'NIFTY': {'candles': sample_series}})
        assert isinstance(signals, list)
        print("✅ SuperTrendWeightedDivergenceStrategy generated valid signal output")
        
        return True
        
    except Exception as e:
        print(f"❌ Strategy functionality test failed: {e}")
        return False


async def test_risk_manager():
    """Test risk manager functionality."""
    print("\nTesting risk manager functionality...")
    
    try:
        from core.risk.manager import RiskManager
        
        # Create mock config
        config = {
            'max_position_size': 100000,
            'stop_loss_percentage': 0.02,
            'take_profit_percentage': 0.05,
            'max_drawdown': 0.1,
            'max_trades_per_day': 100,
        }
        
        # Initialize risk manager
        risk_manager = RiskManager(config)
        print("✅ RiskManager initialized successfully")
        
        # Test risk manager properties
        assert risk_manager.running == False
        print("✅ RiskManager properties work correctly")
        
        return True
        
    except Exception as e:
        print(f"❌ Risk manager test failed: {e}")
        return False


async def main():
    """Run all tests."""
    print("=" * 60)
    print("Belaku TradeBot Structure Test")
    print("=" * 60)
    
    # Test imports
    if not test_imports():
        print("\n❌ Import tests failed!")
        sys.exit(1)
    
    # Test basic functionality
    if not await test_basic_functionality():
        print("\n❌ Basic functionality tests failed!")
        sys.exit(1)
    
    # Test strategy functionality
    if not await test_strategy_functionality():
        print("\n❌ Strategy functionality tests failed!")
        sys.exit(1)
    
    # Test risk manager
    if not await test_risk_manager():
        print("\n❌ Risk manager tests failed!")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("✅ All tests passed!")
    print("=" * 60)
    print("\nThe Belaku TradeBot structure is working correctly.")
    print("Note: External API connections (like Zerodha) are not tested")
    print("in this script to avoid network dependencies.")


if __name__ == "__main__":
    asyncio.run(main())