"""
Main entry point for Belaku TradeBot.

This module serves as the main entry point for the trading bot,
initializing all components and starting the trading engine.
"""

import asyncio
import logging
import sys
import os
from typing import Dict, Any
from dotenv import load_dotenv

from get_token import acquire_access_token
from core.engine import TradingEngine
from exchanges.zerodha import ZerodhaExchange
from core.strategy.supertrend_divergence import SuperTrendWeightedDivergenceStrategy

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('tradebot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

logger = logging.getLogger(__name__)


def load_config() -> Dict[str, Any]:
    """
    Load configuration from environment variables and config files.
    
    Returns:
        Configuration dictionary
    """
    logger.info("Loading bot configuration from .env and defaults")
    load_dotenv()
    config = {
        'engine': {
            'loop_interval': 1.0,
            'max_orders_per_second': 10,
            'max_positions': 100,
        },
        'data': {
            'collection_interval': 1.0,
            'cache_size': 10000,
            'historical_days': 30,
        },
        'risk': {
            'max_position_size': 100000,
            'stop_loss_percentage': 0.02,
            'take_profit_percentage': 0.05,
            'max_drawdown': 0.1,
            'max_trades_per_day': 100,
        },
        'zerodha': {
            'api_key': 'YOUR_API_KEY_HERE',
            'api_secret': 'YOUR_API_SECRET_HERE',
            'access_token': 'YOUR_ACCESS_TOKEN_HERE',
        },
        'supertrend': {
            'atr_period': 10,
            'multiplier': 3.0,
            'rsi_period': 14,
            'rsi_weight': 0.6,
            'macd_weight': 0.4,
            'min_strength': 0.55,
            'cooldown_period': 60,
            'max_signals_per_hour': 10,
            'max_position_size': 100000,
        }
    }
    
    # Load from environment variables if available
    import os
    if os.getenv('ZERODHA_API_KEY'):
        config['zerodha']['api_key'] = os.getenv('ZERODHA_API_KEY')
    
    if os.getenv('ZERODHA_API_SECRET'):
        config['zerodha']['api_secret'] = os.getenv('ZERODHA_API_SECRET')
    
    if os.getenv('ZERODHA_ACCESS_TOKEN'):
        config['zerodha']['access_token'] = os.getenv('ZERODHA_ACCESS_TOKEN')

    logger.info(
        "Config ready: api_key=%s, api_secret=%s, access_token=%s",
        'SET' if config['zerodha']['api_key'] and config['zerodha']['api_key'] != 'YOUR_API_KEY_HERE' else 'MISSING',
        'SET' if config['zerodha']['api_secret'] and config['zerodha']['api_secret'] != 'YOUR_API_SECRET_HERE' else 'MISSING',
        'SET' if config['zerodha']['access_token'] and config['zerodha']['access_token'] != 'YOUR_ACCESS_TOKEN_HERE' else 'MISSING'
    )
    
    return config


async def main() -> None:
    """
    Main function to run the trading bot.
    """
    logger.info("[BOOT] Starting Belaku TradeBot")
    
    load_dotenv()
    api_key = os.getenv('ZERODHA_API_KEY', '').strip()
    api_secret = os.getenv('ZERODHA_API_SECRET', '').strip()
    access_token = os.getenv('ZERODHA_ACCESS_TOKEN', '').strip()

    logger.info("[AUTH] API key present=%s | secret present=%s | access token present=%s",
                bool(api_key), bool(api_secret), bool(access_token))

    if not api_key or not api_secret:
        raise RuntimeError("Set ZERODHA_API_KEY and ZERODHA_API_SECRET in .env")

    if not access_token or 'your_' in access_token.lower() or 'mock' in access_token.lower():
        logger.info("[AUTH] No valid access token found; starting Kite login flow")
        access_token = await asyncio.to_thread(acquire_access_token, api_key, api_secret)
        os.environ['ZERODHA_ACCESS_TOKEN'] = access_token
        logger.info("[AUTH] Access token acquired and saved to environment")
    else:
        logger.info("[AUTH] Existing access token detected; skipping login flow")

    # Load configuration
    config = load_config()
    logger.info("[BOOT] Configuration loaded with engine=%s, strategy=%s", list(config.keys()), 'supertrend')
    
    # Initialize trading engine
    logger.info("[BOOT] Creating TradingEngine")
    engine = TradingEngine(config['engine'])
    
    # Initialize Zerodha exchange
    logger.info("[BOOT] Initializing Zerodha exchange connection")
    zerodha_exchange = ZerodhaExchange(
        name='ZERODHA',
        api_key=config['zerodha']['api_key'],
        api_secret=config['zerodha']['api_secret'],
        access_token=config['zerodha']['access_token']
    )
    
    # Add exchange to engine
    engine.add_exchange(zerodha_exchange)
    logger.info("[BOOT] Zerodha exchange registered with engine")
    
    # Initialize the pure SuperTrend + divergence strategy
    logger.info("[BOOT] Initializing SuperTrendWeightedDivergenceStrategy")
    supertrend_strategy = SuperTrendWeightedDivergenceStrategy(config['supertrend'])

    # Add strategy to engine
    engine.add_strategy(supertrend_strategy)
    logger.info("[BOOT] Strategy registered: %s", supertrend_strategy.name)

    # Start trading engine
    logger.info("[BOOT] Starting engine and data loop")
    await engine.start()

    logger.info("[BOOT] Belaku TradeBot started successfully")
    
    try:
        # Keep the bot running
        while True:
            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("[SHUTDOWN] Received interrupt signal")
    finally:
        # Stop trading engine (also disconnects the exchanges)
        logger.info("[SHUTDOWN] Stopping engine and disconnecting exchanges")
        await engine.stop()

        logger.info("[SHUTDOWN] Belaku TradeBot stopped")


async def extract_giftnifty_events(exchange: ZerodhaExchange) -> None:
    """
    Extract GiftNifty events from Zerodha.
    
    Args:
        exchange: Zerodha exchange instance
    """
    logger.info("Starting GiftNifty event extraction")
    
    while True:
        try:
            # Extract GiftNifty events
            events = await exchange.extract_giftnifty_events()
            
            if events:
                logger.info(f"Extracted {len(events)} GiftNifty events")
                
                # Process events (in a real implementation, you would pass these to the strategy)
                for event in events:
                    logger.info(f"Event: {event['type']} - {event['symbol']}")
            
            # Wait before next extraction
            await asyncio.sleep(60.0)  # Extract every minute
            
        except Exception as e:
            logger.error(f"Error extracting GiftNifty events: {str(e)}")
            await asyncio.sleep(60.0)  # Wait before retrying


if __name__ == '__main__':
    asyncio.run(main())