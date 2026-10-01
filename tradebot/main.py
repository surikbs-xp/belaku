"""
Main entry point for Belaku TradeBot.

This module serves as the main entry point for the trading bot,
initializing all components and starting the trading engine.
"""

import asyncio
import logging
import sys
from datetime import datetime
from typing import Dict, Any

from .core.engine import TradingEngine
from .exchanges.zerodha import ZerodhaExchange
from .core.strategy.giftnifty import GiftNiftyStrategy

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
        'giftnifty': {
            'min_signal_strength': 0.7,
            'min_confidence': 0.8,
            'max_position_size': 100000,
            'stop_loss_percentage': 0.02,
            'take_profit_percentage': 0.05,
            'cooldown_period': 300,
            'max_signals_per_hour': 10,
            'gift_event_weight': 1.0,
            'nifty_event_weight': 0.8,
            'special_event_weight': 0.9,
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
    
    return config


async def main() -> None:
    """
    Main function to run the trading bot.
    """
    logger.info("Starting Belaku TradeBot")
    
    # Load configuration
    config = load_config()
    
    # Initialize trading engine
    engine = TradingEngine(config['engine'])
    
    # Initialize Zerodha exchange
    zerodha_exchange = ZerodhaExchange(
        name='ZERODHA',
        api_key=config['zerodha']['api_key'],
        api_secret=config['zerodha']['api_secret'],
        access_token=config['zerodha']['access_token']
    )
    
    # Add exchange to engine
    engine.add_exchange(zerodha_exchange)
    
    # Initialize GiftNifty strategy
    giftnifty_strategy = GiftNiftyStrategy(config['giftnifty'])
    
    # Add strategy to engine
    engine.add_strategy(giftnifty_strategy)
    
    # Connect to Zerodha
    await zerodha_exchange.connect()
    
    # Start trading engine
    await engine.start()
    
    # Start GiftNifty event extraction
    asyncio.create_task(extract_giftnifty_events(zerodha_exchange))
    
    logger.info("Belaku TradeBot started successfully")
    
    try:
        # Keep the bot running
        while True:
            await asyncio.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        # Stop trading engine
        await engine.stop()
        
        # Disconnect from Zerodha
        await zerodha_exchange.disconnect()
        
        logger.info("Belaku TradeBot stopped")


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