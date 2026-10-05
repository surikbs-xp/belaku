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