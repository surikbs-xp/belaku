# Belaku TradeBot

A sophisticated trading bot designed for automated cryptocurrency and stock trading.

## Overview

Belaku TradeBot is a comprehensive trading automation platform that combines advanced algorithmic trading strategies with robust risk management and real-time market analysis.

## Features

- **Multi-exchange support**: Zerodha and other exchanges
- **Advanced strategies**: Technical analysis, machine learning, sentiment analysis
- **Risk management**: Position sizing, stop-loss, take-profit, portfolio optimization
- **Real-time monitoring**: Live P&L tracking, performance analytics
- **Backtesting**: Historical data analysis and strategy optimization
- **Paper trading**: Risk-free trading simulation
- **API integration**: REST and WebSocket support
- **Docker deployment**: Easy containerization and scaling

## Architecture

```
belaku/
├── core/
│   ├── engine.py          # Main trading engine
│   ├── strategy/          # Strategy implementations
│   │   ├── base.py        # Base strategy class
│   │   └── giftnifty.py   # GiftNifty-specific strategy
│   ├── risk/              # Risk management
│   │   └── manager.py     # Risk control logic
│   └── data/              # Data handling
│       └── handler.py     # Market data processing
├── exchanges/             # Exchange integrations
│   ├── base.py            # Base exchange class
│   └── zerodha.py         # Zerodha integration
├── core/types.py          # Shared data types
├── main.py                # Entry point
├── requirements.txt       # Dependencies
├── .env.example          # Environment configuration
└── test_structure.py      # Structure test script
```

## Quick Start

### Prerequisites

- Python 3.8+
- pip
- PostgreSQL (optional, for historical data)
- Redis (optional, for caching)

### Installation

```bash
# Clone the repository
cd /path/to/belaku
cd tradebot

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env

# Initialize database (if using PostgreSQL)
python scripts/init_db.py
```

### Running the Bot

```bash
# Start the trading engine
python main.py

# Run backtesting
python -m backtesting.run

# Start monitoring dashboard
python -m monitoring.dashboard
```

## Configuration

The bot uses a hierarchical configuration system:

- `config/` - Main configuration files
- `exchanges/` - Exchange-specific settings
- `strategies/` - Strategy parameters

Configuration can be overridden via environment variables or command-line arguments.

## Interpreting Strategy Logs

The SuperTrend strategy emits a `[STRATEGY]` status once per symbol per minute. Its `conditions` field reports the SuperTrend and Parabolic SAR (PSAR) directions and whether they agree. A BUY or SELL direction is eligible only when those two indicators agree and the market-data feed is fresh.

For example, `signal=BUY_BLOCKED_COOLDOWN` means the indicators agree on BUY, but the strategy recently emitted a signal and its cooldown has not expired. The default cooldown is 60 seconds. No new signal is returned for that evaluation. Other blocked states include `*_BLOCKED_STALE_FEED` and `*_BLOCKED_HOURLY_LIMIT`; `NO_SIGNAL` means the indicators do not agree.

`divergence` is a normalized RSI/MACD composite from -1 to 1. The `(strength_only)` label means it contributes to signal strength; it does not choose the BUY/SELL direction. Direction comes from the agreeing SuperTrend and PSAR values. `strength` in `[ST] FINAL signal` is a composite score, not a probability: it combines divergence magnitude and SuperTrend strength, with a lower multiplier when divergence points against the trend.

`[ST] FINAL signal` means the strategy created a signal object. It does not mean an order was submitted or filled. The logged `price` is the latest candle close, while the earlier `[STRATEGY]` line reports the live `ltp` separately. For example, `psar` is the current Parabolic SAR value, not the execution price.

## Live Order Management

Accepted strategy signals are submitted as live orders through the configured exchange. Zerodha orders use its regular order endpoint; the current signal path uses market orders. On startup, the engine imports Zerodha's current-day order book so working orders remain visible to duplicate-order checks after a restart. Broker updates maintain local order status and fills, and cancellations are only marked complete when the broker confirms them.

Stop-loss and take-profit levels are monitored by the running engine and trigger market close orders; they are not resting protective orders held at Zerodha. Keep the broker order book and positions as the source of truth, especially after a disconnect or restart.

When an opposite signal arrives for a futures contract, the engine checks that contract's broker orders and position. It requests cancellation of conflicting working orders, closes any filled position, and waits for broker updates to confirm the old exposure is flat before submitting the new-side market entry. A partially filled order is handled by canceling its remainder and closing the filled quantity first.

## Development

### Testing

```bash
# Run unit tests
pytest tests/

# Run integration tests
pytest tests/integration/

# Run performance tests
pytest tests/performance/
```

### Code Quality

```bash
# Linting
flake8 src/
mypy src/
black src/
isort src/

# Type checking
mypy src/
```

### Documentation

- API documentation: `mkdocs serve docs/`
- Architecture diagrams: `diagrams/architecture.md`

## Deployment

### Docker

```bash
# Build and run with Docker Compose
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Kubernetes

```bash
# Deploy to Kubernetes
kubectl apply -f k8s/
```

## Monitoring

The bot includes comprehensive monitoring:

- **Metrics**: Prometheus metrics collection
- **Logging**: Structured logging with ELK stack
- **Alerts**: Slack, Discord, email notifications
- **Dashboard**: Grafana visualization

## Security

- API key management
- Rate limiting
- IP whitelisting
- Two-factor authentication support

## License

This project is licensed under the MIT License.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Push to the branch
5. Create a pull request

## Support

For support, please open an issue in the repository.

## Acknowledgements

Special thanks to the open-source community for their contributions and the trading community for their insights.