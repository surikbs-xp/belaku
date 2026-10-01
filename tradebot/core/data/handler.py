"""
Data handler for Belaku TradeBot.

This module handles data collection, processing, and storage for the trading bot,
including market data, historical data, and real-time data streams.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
from collections import deque

logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    """Market data structure."""
    symbol: str
    exchange: str
    timestamp: datetime
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    volume: float
    timeframe: str = "1minute"
    indicators: Dict[str, Any] = field(default_factory=dict)

    @property
    def price_change(self) -> float:
        """Get price change from open."""
        return self.close_price - self.open_price

    @property
    def price_change_percent(self) -> float:
        """Get price change percentage from open."""
        return (self.price_change / self.open_price * 100) if self.open_price > 0 else 0.0

    @property
    def high_low_range(self) -> float:
        """Get high-low range."""
        return self.high_price - self.low_price

    @property
    def average_price(self) -> float:
        """Get average price (HLC/3)."""
        return (self.high_price + self.low_price + self.close_price) / 3


@dataclass
class HistoricalData:
    """Historical data structure."""
    symbol: str
    exchange: str
    timeframe: str
    start_time: datetime
    end_time: datetime
    candles: List[MarketData] = field(default_factory=list)

    @property
    def total_candles(self) -> int:
        """Get total number of candles."""
        return len(self.candles)

    @property
    def latest_price(self) -> Optional[float]:
        """Get latest price."""
        if self.candles:
            return self.candles[-1].close_price
        return None

    @property
    def earliest_price(self) -> Optional[float]:
        """Get earliest price."""
        if self.candles:
            return self.candles[0].close_price
        return None


class DataHandler:
    """
    Data handler for collecting, processing, and storing market data.
    
    This class manages data collection from various sources, processes the data,
    and provides access to historical and real-time market data.
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the data handler.
        
        Args:
            config: Configuration dictionary containing data handler settings
        """
        self.config = config
        self.market_data_streams: Dict[str, deque] = {}
        self.historical_data: Dict[str, HistoricalData] = {}
        self.data_providers: Dict[str, Any] = {}
        self.data_subscribers: Dict[str, List[Callable]] = {}
        self.running = False
        self.data_cache: Dict[str, deque] = {}
        
        # Data processing
        self.indicator_calculators: Dict[str, Callable] = {}
        self.data_filters: List[Callable] = []
        
        # Performance tracking
        self.stats = {
            'data_points_collected': 0,
            'data_points_processed': 0,
            'data_points_dropped': 0,
            'processing_time_ms': 0.0,
        }
        
        logger.info("Data handler initialized")

    async def start(self) -> None:
        """
        Start the data handler.
        
        This method initializes all data providers and starts data collection.
        """
        logger.info("Starting data handler")
        self.running = True
        
        # Initialize data providers
        await self._initialize_data_providers()
        
        # Start data collection
        asyncio.create_task(self._data_collection_loop())
        
        logger.info("Data handler started successfully")

    async def stop(self) -> None:
        """
        Stop the data handler.
        
        This method gracefully shuts down all data providers.
        """
        logger.info("Stopping data handler")
        self.running = False
        
        # Stop data providers
        for provider in self.data_providers.values():
            if hasattr(provider, 'stop'):
                await provider.stop()
        
        logger.info("Data handler stopped")

    async def add_data_stream(self, symbol: str, exchange: str, max_size: int = 1000) -> None:
        """
        Add a data stream for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            max_size: Maximum size of the data stream
        """
        stream_key = f"{symbol}_{exchange}"
        
        if stream_key not in self.market_data_streams:
            self.market_data_streams[stream_key] = deque(maxlen=max_size)
            self.data_cache[stream_key] = deque(maxlen=max_size)
            self.data_subscribers[stream_key] = []
        
        logger.info(f"Added data stream for {symbol} on {exchange}")

    async def add_data_provider(self, name: str, provider: Any) -> None:
        """
        Add a data provider.
        
        Args:
            name: Provider name
            provider: Data provider instance
        """
        self.data_providers[name] = provider
        logger.info(f"Added data provider: {name}")

    async def subscribe_to_data(self, symbol: str, exchange: str, callback: Callable) -> None:
        """
        Subscribe to data updates for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            callback: Async callback function
        """
        stream_key = f"{symbol}_{exchange}"
        
        if stream_key not in self.data_subscribers:
            self.data_subscribers[stream_key] = []
        
        self.data_subscribers[stream_key].append(callback)
        logger.info(f"Subscribed to {symbol} on {exchange}")

    async def get_latest_data(self, symbol: str, exchange: str, limit: int = 100) -> List[MarketData]:
        """
        Get latest market data for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            limit: Maximum number of data points to return
            
        Returns:
            List of MarketData objects
        """
        stream_key = f"{symbol}_{exchange}"
        
        if stream_key not in self.market_data_streams:
            return []
        
        data = list(self.market_data_streams[stream_key])[-limit:]
        return data

    async def get_historical_data(self, symbol: str, exchange: str, start_time: datetime, end_time: datetime, timeframe: str = "1minute") -> List[MarketData]:
        """
        Get historical data for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            start_time: Start time
            end_time: End time
            timeframe: Timeframe
            
        Returns:
            List of MarketData objects
        """
        stream_key = f"{symbol}_{exchange}"
        
        if stream_key not in self.historical_data:
            return []
        
        historical = self.historical_data[stream_key]
        
        # Filter data by time range
        filtered_candles = []
        for candle in historical.candles:
            if start_time <= candle.timestamp <= end_time:
                filtered_candles.append(candle)
        
        return filtered_candles

    async def add_indicator_calculator(self, name: str, calculator: Callable) -> None:
        """
        Add an indicator calculator.
        
        Args:
            name: Calculator name
            calculator: Async function to calculate indicators
        """
        self.indicator_calculators[name] = calculator
        logger.info(f"Added indicator calculator: {name}")

    async def add_data_filter(self, filter_func: Callable) -> None:
        """
        Add a data filter.
        
        Args:
            filter_func: Async function to filter data
        """
        self.data_filters.append(filter_func)
        logger.info("Added data filter")

    async def process_market_data(self, market_data: MarketData) -> MarketData:
        """
        Process market data through all filters and indicator calculators.
        
        Args:
            market_data: Market data to process
            
        Returns:
            Processed market data
        """
        start_time = datetime.now()
        
        # Apply data filters
        for filter_func in self.data_filters:
            try:
                market_data = await filter_func(market_data)
            except Exception as e:
                logger.error(f"Error in data filter: {str(e)}")
        
        # Calculate indicators
        for name, calculator in self.indicator_calculators.items():
            try:
                indicators = await calculator(market_data)
                market_data.indicators.update(indicators)
            except Exception as e:
                logger.error(f"Error in indicator calculator {name}: {str(e)}")
        
        # Update statistics
        self.stats['data_points_processed'] += 1
        self.stats['processing_time_ms'] += (datetime.now() - start_time).total_seconds() * 1000
        
        return market_data

    async def _initialize_data_providers(self) -> None:
        """
        Initialize data providers.
        """
        # Initialize default data providers
        # This would typically include:
        # - Zerodha data provider
        # - Binance data provider
        # - Coinbase data provider
        # - Custom data providers
        
        logger.info("Initialized data providers")

    async def _data_collection_loop(self) -> None:
        """
        Main data collection loop.
        
        This method continuously collects data from all providers and processes it.
        """
        while self.running:
            try:
                # Collect data from all providers
                for provider in self.data_providers.values():
                    if hasattr(provider, 'collect_data'):
                        try:
                            data = await provider.collect_data()
                            if data:
                                await self._process_new_data(data)
                        except Exception as e:
                            logger.error(f"Error collecting data from provider: {str(e)}")
                
                # Sleep for next iteration
                await asyncio.sleep(self.config.get('collection_interval', 1.0))
                
            except Exception as e:
                logger.error(f"Error in data collection loop: {str(e)}")
                await asyncio.sleep(5.0)  # Wait before retrying

    async def _process_new_data(self, data: Any) -> None:
        """
        Process new data from providers.
        
        Args:
            data: New data to process
        """
        try:
            # Convert to MarketData if needed
            if isinstance(data, dict) and 'symbol' in data and 'exchange' in data:
                market_data = MarketData(
                    symbol=data['symbol'],
                    exchange=data['exchange'],
                    timestamp=datetime.fromisoformat(data['timestamp']) if isinstance(data['timestamp'], str) else data['timestamp'],
                    open_price=data.get('open_price', 0),
                    high_price=data.get('high_price', 0),
                    low_price=data.get('low_price', 0),
                    close_price=data.get('close_price', 0),
                    volume=data.get('volume', 0),
                    timeframe=data.get('timeframe', '1minute'),
                    indicators=data.get('indicators', {})
                )
            else:
                # Assume it's already a MarketData object
                market_data = data
            
            # Process data
            processed_data = await self.process_market_data(market_data)
            
            # Store data
            stream_key = f"{processed_data.symbol}_{processed_data.exchange}"
            if stream_key in self.market_data_streams:
                self.market_data_streams[stream_key].append(processed_data)
                self.data_cache[stream_key].append(processed_data)
                
                # Notify subscribers
                for callback in self.data_subscribers.get(stream_key, []):
                    try:
                        await callback(processed_data)
                    except Exception as e:
                        logger.error(f"Error in data subscriber: {str(e)}")
            
            # Update statistics
            self.stats['data_points_collected'] += 1
            
        except Exception as e:
            logger.error(f"Error processing new data: {str(e)}")
            self.stats['data_points_dropped'] += 1

    def get_stats(self) -> Dict[str, Any]:
        """
        Get data handler statistics.
        
        Returns:
            Dictionary of statistics
        """
        return self.stats.copy()

    def get_data_streams(self) -> List[str]:
        """
        Get list of active data streams.
        
        Returns:
            List of stream keys
        """
        return list(self.market_data_streams.keys())

    def get_cache(self, symbol: str, exchange: str, limit: int = 100) -> List[MarketData]:
        """
        Get cached data for a symbol.
        
        Args:
            symbol: Trading symbol
            exchange: Exchange name
            limit: Maximum number of data points to return
            
        Returns:
            List of MarketData objects
        """
        stream_key = f"{symbol}_{exchange}"
        
        if stream_key not in self.data_cache:
            return []
        
        data = list(self.data_cache[stream_key])[-limit:]
        return data