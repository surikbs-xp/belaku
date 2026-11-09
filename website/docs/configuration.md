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
| `ALLOWED_HOSTS` | `127.0.0.1,localhost,[::1],testserver` | Host header allow-list. Only widen this for a real proxy. |
| `ALLOWED_ORIGINS` | *(empty)* | Extra cross-origin callers, comma separated. Empty = same-origin only. |
| `DASHBOARD_PASSWORD` | *(empty)* | Password for the dashboard and control API. **Empty leaves it open.** |
| `DASHBOARD_SESSION_SECRET` | *(random per process)* | Pins the session signing key so logins survive a restart |
| `ENABLE_API_DOCS` | `0` | Set `1` to serve `/docs` and `/openapi.json` again |
| `LOG_FILE` | `bot/logs/bot.log` | Docker: `/data/bot.log` |
| `LOG_LEVEL` | `INFO` | Minimum level written. Also the floor the Logs tab can query. |
| `UVICORN_ACCESS_LOG` | `0` | Set `1` to log every request |

`LOG_FILE` is rotated at 5 MB with 5 backups, and the Logs tab only reads the
last 512 KB of it — so a long-running bot cannot make the dashboard slow.

:::danger[The dashboard is unauthenticated]
Anyone who can reach `/api/panic` or `/api/toggle-mode` can move real money.
Keep it on loopback, or put it behind an authenticating reverse proxy.

Three controls reduce (but do not remove) the exposure:

- **Cross-site POSTs are rejected.** `Origin` must match the Host allow-list;
  a page on another site gets `403`. Requests with no `Origin` (curl, the
  container healthcheck) still pass.
- **`Host` is allow-listed**, which blocks DNS rebinding — where a malicious
  hostname resolves to your bot and the browser then treats it as same-origin.
- **`/docs` is off**, so the control surface is not enumerated for a stranger.

None of these are authentication. If the port is ever reachable from outside
your machine, anyone who can send a request can panic the account.

`/api/logs` also tail-reads `LOG_FILE`, so keep it on loopback too.
:::


## Execution

| Variable | Default | Notes |
|---|---|---|
| `EXECUTION_MODE` | `PAPER` | `PAPER` or `LIVE` |
| `DEFAULT_LOTS` | `1` | Lots per entry |
| `NIFTY_LOT_SIZE` | `65` | Fallback when Kite instruments are unavailable |
| `MAX_OPEN_LOTS` | `4` | Hard cap on concurrent lots |
| `STOP_LOSS_POINTS` | `0` | Close a position this many points against entry. `0` disables. |
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
| `GIFT_CANDLE_INTERVAL` | `15minute` | Must match the timeframe the strategy was tuned on |
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


## Alerting

| Variable | Default | Notes |
|---|---|---|
| `ALERT_WEBHOOK_URL` | *(empty)* | JSON webhook for watchdog alerts. Empty = log and dashboard only. |
| `WATCHDOG_INTERVAL_SECONDS` | `30` | How often trading health is checked |

The watchdog alerts when the process is up but the trading is not: a stale feed,
a disconnected feed during market hours, a risk breach, a paused strategy, or an
engine error. Alerts fire only after three consecutive failing checks and are
rate-limited to one per kind per 15 minutes. See
[Production deployment](./production.md#monitoring).


## Tuning notes

**`GIFT_CANDLE_INTERVAL` must match the strategy's timeframe.** The indicators
are only meaningful on the timeframe they were tuned for. Changing it without
re-tuning the strategy changes the signals.

It also decides how long warm-up takes, because it is bars - not time - that
the indicators need: the longest SuperTrend wants 14 closed bars, which is
70 minutes at 5 minutes and 3.5 hours at 15. The bot sidesteps that on
startup by loading Kite history, so the delay only bites if history is
unavailable and the series has to be built from live ticks.

Bars are stored per interval, so switching this back and forth does not mix
two series together.

**`LOT_MARGIN` is an estimate.** LIVE prefers Kite's real margin API and reports
when it falls back to this number. Set it conservatively.

**`MAX_OPEN_LOTS` caps position size; `STOP_LOSS_POINTS` caps loss.** The strategy
has no stop-loss of its own — it exits on an opposite signal or a manual
square-off — so without this a position is unbounded between those two events.
The limit latches: once tripped the bot squares off, pauses, and stays paused
until you acknowledge the breach on the dashboard. See
[Production deployment](./production.md#risk-limits).
