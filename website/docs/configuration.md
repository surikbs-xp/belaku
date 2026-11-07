---
title: Configuration
---

# Configuration

Every setting is an environment variable, read from `bot/.env` with a safe
default. The bot boots in PAPER mode with no credentials at all — it just cannot
evaluate the strategy without a token.

## Credentials

| Variable | Default | Notes |
|---|---|---|
| `KITE_API_KEY` | — | From [https://developers.kite.trade](https://developers.kite.trade) |
| `KITE_API_SECRET` | — | |
| `KITE_ACCESS_TOKEN` | — | Expires daily; refreshed by the login flow |
| `KITE_REDIRECT_URL` | `http://127.0.0.1:9000/zerodha/callback` | Must match the Kite console exactly |

## Dashboard / API

| Variable | Default | Notes |
|---|---|---|
| `BOT_NAME` | `Belaku` | Browser tab title and header |
| `WEBHOOK_HOST` | `127.0.0.1` | Bind address. Keep on loopback. |
| `WEBHOOK_PORT` | `9000` | |
| `LOG_FILE` | `bot/logs/bot.log` | Docker: `/data/bot.log` |
| `LOG_LEVEL` | `INFO` | Minimum level written. Also the floor the Logs tab can query. |
| `UVICORN_ACCESS_LOG` | `0` | Set `1` to log every request |

`LOG_FILE` is rotated at 5 MB with 5 backups, and the Logs tab only reads the
last 512 KB of it — so a long-running bot cannot make the dashboard slow.

:::danger[The dashboard is unauthenticated]
Anyone who can reach `/api/panic` or `/api/toggle-mode` can move real money.
Keep it on loopback, or put it behind an authenticating reverse proxy.

This includes `/api/logs`, which tail-reads `LOG_FILE`. Credential-shaped
strings are masked before the response is built, but the log is still a
diagnostic stream you would not want on a public URL.
:::


## Execution

| Variable | Default | Notes |
|---|---|---|
| `EXECUTION_MODE` | `PAPER` | `PAPER` or `LIVE` |
| `DEFAULT_LOTS` | `1` | Lots per entry |
| `NIFTY_LOT_SIZE` | `65` | Fallback when Kite instruments are unavailable |
| `MAX_OPEN_LOTS` | `4` | Hard cap on concurrent lots |
| `VIRTUAL_BALANCE` | `200000` | PAPER starting capital |
| `PRODUCT` | `NRML` | `NRML` or `MIS` |
| `ORDER_TYPE` | `MARKET` | `MARKET` or `LIMIT` |
| `SLIPPAGE_TICKS` | `1.0` | |
| `TICK_SIZE` | `0.05` | Nifty futures tick |
| `LOT_MARGIN` | `150000` | **Estimate** used when Kite's margin API is unavailable |
| `TRADING_ALLOW_OFF_HOURS` | `0` | Testing escape hatch — bypasses the session gate |

## Strategy

| Variable | Default | Notes |
|---|---|---|
| `STRATEGY_SUPERTRENDS` | `11:1,12:2,13:3` | `atr_length:multiplier` pairs, comma separated |
| `STRATEGY_ST_MIN_AGREE` | `2` | How many SuperTrends must agree |
| `STRATEGY_PSAR_START` | `0.02` | |
| `STRATEGY_PSAR_INCREMENT` | `0.02` | |
| `STRATEGY_PSAR_MAXIMUM` | `0.2` | |

Changing `STRATEGY_SUPERTRENDS` moves the warm-up requirement automatically
(`longest ATR period + 1`). Malformed entries are dropped rather than raising, so
a typo cannot stop the bot from booting — if nothing usable remains, the default
triple is used.

## Market data

| Variable | Default | Notes |
|---|---|---|
| `GIFT_CANDLE_INTERVAL` | `5minute` | Must match the timeframe the strategy was tuned on |
| `ENGINE_INTERVAL_SECONDS` | `5.0` | Mark loop and closed-bar check cadence |
| `QUOTE_POLL_SECONDS` | `5` | |
| `INSTRUMENT_REFRESH_SECONDS` | `3600` | Kite instrument dump refresh |
| `HTTP_TIMEOUT_SECONDS` | `4.0` | |

## Storage

| Variable | Default | Notes |
|---|---|---|
| `DB_PATH` | `bot/paper_trading.db` | Docker: `/data/paper_trading.db` |
| `HOLIDAY_CACHE_PATH` | `bot/data/nse_holidays.json` | Docker: `/data/nse_holidays.json` |

:::warning[Docker paths must be on the volume]
The image is `read_only: true`. Any path inside the image cannot be written,
so `DB_PATH`, `LOG_FILE` and `HOLIDAY_CACHE_PATH` must all point at `/data`.
:::


## Tuning notes

**`GIFT_CANDLE_INTERVAL` must match the strategy's timeframe.** The indicators
are only meaningful on the timeframe they were tuned for. Changing it without
re-tuning the strategy changes the signals.

**`LOT_MARGIN` is an estimate.** LIVE prefers Kite's real margin API and reports
when it falls back to this number. Set it conservatively.

**`MAX_OPEN_LOTS` is the only position-size cap.** There is no stop-loss, so this
is your primary risk control.
