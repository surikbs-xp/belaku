# Belaku TradeBot

Belaku is a Python trading bot that connects to Zerodha Kite Connect, builds live GIFT NIFTY candles, evaluates a SuperTrend/PSAR strategy, and routes market orders to the active NIFTY futures contract.

This application can place real orders. Start with a test account and review the configured quantity, risk limits, and broker position before running it unattended. A market order can fill at a different price from the logged reference price.

## Current Scope

- Zerodha Kite Connect REST and WebSocket integration.
- GIFT NIFTY historical and live candle data, with NIFTY front-month futures used for order routing.
- SuperTrend and Parabolic SAR (PSAR) direction agreement for BUY/SELL direction.
- MACD histogram divergence contributes to signal strength; it does not choose direction.
- Broker position/order reconciliation, reversal handling, risk checks, and local stop-loss/take-profit monitoring.

The repository does not currently include the database, Redis, Docker/Kubernetes deployment, dashboard, backtesting, or alert integrations described by older project notes.

## Project Layout

```text
main.py                         Application entry point and default configuration
get_token.py                    Local Kite OAuth callback and token exchange
core/engine.py                  Signal, order, position, and trading-loop orchestration
core/data/handler.py            Candle storage and strategy snapshots
core/risk/manager.py             Order risk checks and position sizing controls
core/strategy/base.py            Signal and strategy interfaces
core/strategy/supertrend_divergence.py  SuperTrend/PSAR strategy
core/strategy/giftnifty.py       Gift NIFTY event strategy implementation
core/types.py                   Shared order, position, and status types
exchanges/base.py                Exchange interface
exchanges/zerodha.py             Kite REST/WebSocket adapter and futures routing
test_*.py                        Adapter, order, strategy-timing, and structure tests
```

## Setup

Python 3.11 is used by the project virtual environment in this workspace. The dependencies are listed in `requirements.txt`.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

Set `ZERODHA_API_KEY` and `ZERODHA_API_SECRET` in `.env`. Leave `ZERODHA_ACCESS_TOKEN` empty to use the local OAuth flow, or provide a valid token to skip it. The callback URL configured in the Kite developer console must be:

```text
http://127.0.0.1:9000/zerodha/callback
```

Start the bot from the repository root with the virtual-environment interpreter:

```bash
./venv/bin/python main.py
```

The login flow listens on `127.0.0.1:9000`, opens the Kite login page, and exchanges the callback request token for an access token. The token is held in the running process; this code does not write it back to `.env`.

## Configuration

The application currently reads these environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `ZERODHA_API_KEY` | Kite application API key | Required |
| `ZERODHA_API_SECRET` | Kite application API secret | Required |
| `ZERODHA_ACCESS_TOKEN` | Existing session token; blank starts OAuth | Blank |
| `BOT_LOG_LEVEL` | Python logging level, such as `INFO` or `DEBUG` | `INFO` |

Strategy and engine parameters are defined in `main.py` rather than loaded from the other variables in `.env.example`. The application timeframe defaults to 15 minutes. Strategy defaults include ATR period 10, SuperTrend multiplier 3, configured pivot length 2, divergence sensitivity 0.3, a 60-second cooldown, and at most 10 generated signals per hour. The position-notional default is 100,000. Risk defaults include a 2% stop loss and 5% take profit. RSI is not used. `pivot_length` and `divergence_sensitivity` are recorded in configuration and signal metadata but are not currently used by the indicator calculations.

## Signal Timing and Conditions

For a configured candle timeframe, the strategy waits for the first fresh tick of a new candle. It computes indicators using only candles that were complete before that trigger candle began. The signal's `signal_candle_timestamp` identifies the candle used for analysis; `trigger_candle_timestamp` identifies the next candle in which the market order is submitted.

BUY or SELL direction comes from SuperTrend and PSAR agreeing. A disagreement produces no signal. Feed freshness, cooldown, and hourly signal limits can also block a valid direction. MACD histogram divergence and SuperTrend strength affect the reported strength score, which is a score, not a probability or guaranteed return.

Each emitted signal has an ID based on strategy, symbol, timeframe, direction, and trigger-candle timestamp. The engine ignores duplicate deliveries of the same ID during the current process.

## Orders, Reversals, and Retries

Orders generated from strategy signals are market orders. The logged entry reference is used for sizing and risk checks; it is not a guaranteed fill price.

At startup, the engine reconciles the broker's order book and net positions. A same-direction signal does not add to an existing position. An opposite signal cancels conflicting working orders, submits a market order to close the current position, waits for broker state to confirm that exposure is flat, and then submits the new-side entry. Partial positions are closed for the filled quantity before a replacement entry is submitted.

An explicit broker rejection gets at most one retry. A delayed rejected status retries the unfilled quantity using a new broker order. A second rejection stops that retry group. Risk validation failures and ambiguous generic transport errors are not retried. Retry counters and processed signal IDs are in memory and reset when the process restarts; broker positions and working orders are reconciled on startup.

Stop-loss and take-profit checks run in the engine and submit market close orders. They are not resting protective orders at Zerodha. The broker's net positions and order book are the source of truth after restart or disconnect.

## Logs

Logs are written to the terminal and `tradebot.log`. The default level is `INFO`; set `BOT_LOG_LEVEL=DEBUG` for more detail. Important records include:

- `[STRATEGY] SIGNAL`: strategy name, BUY/SELL action, closed-candle indicator price, next-candle entry reference, SuperTrend/PSAR agreement, feed/cooldown/hourly gates, divergence, and strength.
- `[SIGNAL] Processing`: strategy and condition summary passed to the engine, signal and trigger candle timestamps, quantity, and entry reference.
- `[POSITION] NO_CHANGE` / `[ORDER] NO_CHANGE`: an existing same-side position or order prevented a duplicate entry.
- `[REVERSAL]`: a close request, confirmation wait, and subsequent replacement entry.
- `[ORDER] RETRY`: the one permitted retry after an explicit broker rejection.

`indicator_close` and `entry_reference` are intentionally separate: indicators use the previous completed candle, while the market order is submitted after the next candle starts.

## Tests

Run the broker, order-management, and candle-timing tests with:

```bash
./venv/bin/python -m pytest -q test_order_management.py test_kiteconnect_adapter.py test_strategy_candle_timing.py
```

Run the standalone import and structure smoke test with:

```bash
./venv/bin/python test_structure.py
```

Tests use mocks and do not place broker orders. Live API behavior requires a separately configured Kite account and is not covered by the offline test suite.

## Security

Keep API keys, API secrets, and access tokens private. Use `.env` for local credentials and do not commit it. `.env.example` contains placeholders only. Rotate any credential that has been exposed.