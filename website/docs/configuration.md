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
| `MAX_DAILY_LOSS` | `0` | Daily realised-loss cap in rupees; blocks new entries once reached. `0` disables it. Never flattens a position. |
| `RECONCILE_INTERVAL_SECONDS` | `60` | How often the mark loop reconciles unresolved orders against the Zerodha order book. `0` disables the periodic pass. |
| `VIRTUAL_BALANCE` | `200000` | PAPER starting capital |
| `PRODUCT` | `NRML` | `NRML` or `MIS` |
| `ORDER_TYPE` | `MARKET` | `MARKET` or `LIMIT`. Not `SL`/`SL-M`: an entry order type of `SL-M` would be sent with no trigger price, and this bot places no stop orders at all. |
| `MARKET_PROTECTION` | `-1` | Zerodha's market-protection band in ticks. `-1` lets Kite choose; empty omits the parameter. |
| `SLIPPAGE_TICKS` | `1.0` | Adverse-tick fallback for a paper fill with no order-book depth |
| `TICK_SIZE` | `0.05` | Nifty futures tick |
| `LOT_MARGIN` | `150000` | **Estimate** used when Kite's margin API is unavailable |
| `TRADING_ALLOW_OFF_HOURS` | `0` | Testing escape hatch — bypasses the session gate |

## Strategy

The strategy uses exactly two indicators: **SuperTrend (14-period ATR × 3)** and
**Parabolic SAR (0.02 / 0.02 / 0.2)**. The SuperTrend is fixed in code rather than
configured — a second instance would reintroduce the vote the strategy dropped —
so only the PSAR inputs are settings.

| Variable | Default | Notes |
|---|---|---|
| `STRATEGY_PSAR_START` | `0.02` | |
| `STRATEGY_PSAR_INCREMENT` | `0.02` | |
| `STRATEGY_PSAR_MAXIMUM` | `0.2` | |

The warm-up requirement follows the fixed SuperTrend: 14 ATR bars plus one to
compare against, so **15 closed candles**. `STRATEGY_SUPERTRENDS` and
`STRATEGY_ST_MIN_AGREE` are no longer read; leaving them in an old `.env` is
harmless and cannot add an instance.

## Market data

| Variable | Default | Notes |
|---|---|---|
| `GIFT_CANDLE_INTERVAL` | `60minute` | Signal timeframe. See the tuning note below. |
| `ENGINE_INTERVAL_SECONDS` | `5.0` | Mark loop cadence, and the engine's *fallback* poll. A bar close wakes the engine immediately, so this is not the signal latency. |
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


## Strategy

The strategy uses exactly two indicators: **SuperTrend (14-period ATR × 3)** and
**Parabolic SAR (0.02 / 0.02 / 0.2)**. The SuperTrend is fixed in code rather than
configured — a second instance would reintroduce the vote the strategy dropped —
so only the PSAR inputs are settings.

| Variable | Default | Notes |
|---|---|---|
| `STRATEGY_PSAR_START` | `0.02` | |
| `STRATEGY_PSAR_INCREMENT` | `0.02` | |
| `STRATEGY_PSAR_MAXIMUM` | `0.2` | |

The warm-up requirement follows the fixed SuperTrend: 14 ATR bars plus one to
compare against, so **15 closed candles**. `STRATEGY_SUPERTRENDS` and
`STRATEGY_ST_MIN_AGREE` are no longer read; leaving them in an old `.env` is
harmless and cannot add an instance.

## Market data

| Variable | Default | Notes |
|---|---|---|
| `GIFT_CANDLE_INTERVAL` | `60minute` | Signal timeframe. See the tuning note below. |
| `ENGINE_INTERVAL_SECONDS` | `5.0` | Mark loop cadence, and the engine's *fallback* poll. A bar close wakes the engine immediately, so this is not the signal latency. |
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


## Strategy

| Variable | Default | Notes |
|---|---|---|
| `GIFT_CANDLE_INTERVAL` | `60minute` | Bar interval for the signal feed. Kite's valid names are `minute`, `3minute`, `5minute`, `10minute`, `15minute`, `30minute`, `60minute`, `day` — `1minute` is rejected and silently returns no bars. |
| `STRATEGY_EXIT` | `flip` | `flip` holds through a `NO_SIGNAL` bar and closes when the pair reverses; `flat` closes on the first disagreement. See [Strategy](./strategy.md#exit-rules). |
| `STRATEGY_PSAR_START` | `0.02` | PSAR step. |
| `STRATEGY_PSAR_INCREMENT` | `0.02` | PSAR increment. |
| `STRATEGY_PSAR_MAXIMUM` | `0.2` | PSAR ceiling. |

SuperTrend is fixed at 14×3 and is not configurable — a second instance would
reintroduce the vote this strategy deliberately does not have.

## Strategies

Two strategies run: **GIFT NIFTY 15m** and **NIFTY 60m**, each configured with
`STRATEGY_<n>_*` — see [Running multiple strategies](./multi-strategy.md), which
also explains why two strategies in one contract can cancel each other.

| Variable | Default | Notes |
|---|---|---|
| `STRATEGY_ID` | `Surikbs` | Which ledger book this process trades. Must match the rows in the database. |
| `STRATEGY_<n>_SOURCE` | `gift` for 1; required above | `gift` or `nifty`. |
| `STRATEGY_<n>_ID` | `Surikbs` for 1; required above | Ledger book; must be unique. |
| `STRATEGY_<n>_INTERVAL` | `60minute` | Bar interval for that strategy. |
| `STRATEGY_<n>_EXIT` | `flip` | `flip` or `flat`. |
| `STRATEGY_<n>_LOTS` | `1` | Position size for that run. |
| `STRATEGY_<n>_ENABLED` | `1` | `0` configures it without starting it. |
| `STRATEGY_<n>_LIVE` | `0` | Initial LIVE/PAPER choice; the dashboard toggle persists it. |
| `STRATEGY_<n>_FILTER_ADX` | *(off)* | Only enter when ADX(14) exceeds this. |
| `STRATEGY_<n>_FILTER_MA` | *(off)* | Only enter on the trend's side of this SMA. |

There is no stop-loss to configure. Neither book carries an initial stop or a
trailing stop: the signal is the only exit, so `STRATEGY_<n>_STOP_LOSS` and the
`TRAIL_*` keys no longer exist. See
[why there is no trail](./multi-strategy.md#why-there-is-no-trail).

**Neither filter is recommended.** Measured on three years of NIFTY 60-minute
bars, ADX makes the net monotonically worse — ADX>15 drops it from ₹160,318 to
₹92,221, and ADX>18 or higher loses money outright. It raises the win rate at
high thresholds only by removing the large winners too. MA50 is the one filter
that helped (₹247,679, 51.7% win) and it is still a fit rather than an edge:
negative second half, a confidence interval that includes zero, and 48% of gross
profit in a single trade. See [Backtesting](./backtesting.md) for the numbers.

Both filters gate **entries only**. An open position is still closed by the exit
rule, because a filter that blocked an exit would hold a losing trade through the
very conditions it was meant to avoid. A filtered bar is reported as `NO_SIGNAL`
with the reason in `filter_reason`, so the dashboard can say which gate refused
rather than just showing no signal. `STRATEGY_<n>_FILTER_MA` also raises the
warm-up requirement to `n + 1` bars, so the average is never tested on a window
it does not have.

## Alerting

| Variable | Default | Notes |
|---|---|---|
| `ALERT_ENABLED` | `0` | **Master switch.** `0` means nothing is sent, whatever the channel below says. Set `1` only while testing the channel. |
| `ALERT_CHANNEL` | `generic` | `telegram`, `slack`, `discord`, `ntfy` or `generic`. The payload is built per channel. |

| `ALERT_WEBHOOK_URL` | *(empty)* | Webhook for `slack`/`discord`/`ntfy`/`generic`. Empty = log and dashboard only. |
| `ALERT_TELEGRAM_BOT_TOKEN` | *(empty)* | Bot token from @BotFather, for `ALERT_CHANNEL=telegram`. |
| `ALERT_TELEGRAM_CHAT_ID` | *(empty)* | Chat id to send to: the numeric id of the chat that messaged the bot, never the bot's own. Resolve it with `scripts/telegram-setup.py --test`. |
| `ALERT_ON_LIFECYCLE` | `1` | Send a message on start and stop. |
| `WATCHDOG_INTERVAL_SECONDS` | `30` | How often trading health is checked |

A Telegram target must be a *chat* id, not the bot's identity. Telegram records a
chat only after it has received a message from it, so send the bot any message
from your own account first. Two values look configured and deliver nothing:

| Value | Telegram's answer |
|---|---|
| the bot's username, e.g. `belaku_bot` | `400 Bad Request: chat not found` |
| the bot's own numeric id | `403 Forbidden: the bot can't send messages to the bot` |

`scripts/telegram-setup.py` reads the token from `bot/.env`, finds the chat that
messaged the bot, writes the id back, and with `--test` sends a message so the
delivery is confirmed rather than assumed:

```bash
scripts/telegram-setup.py --list     # what Telegram has seen
scripts/telegram-setup.py --test     # write the id and send a test alert
```

Nothing leaves the bot while `ALERT_ENABLED=0`, which is the default even when
credentials are filled in. The bot is deliberately noisy — every fill and every
signal flip is an event — so a phone is a poor place to read a session. Enable it
to prove the channel end to end, then switch it back off.

The watchdog alerts when the process is up but the trading is not: a stale feed,
a disconnected feed during market hours, a risk breach, a paused strategy, or an
engine error. Alerts fire only after three consecutive failing checks and are
rate-limited to one per kind per 15 minutes. See
[Production deployment](./production.md#monitoring).


## Tuning notes

**`GIFT_CANDLE_INTERVAL` must match the strategy's timeframe.** The indicators
are only meaningful on the timeframe they were tuned for. The shipped value is
**60 minutes**: the strategy holds a position for a median of 26 bars, so a
finer timeframe multiplies the charge count without improving accuracy. Check
any change against [Backtesting](./backtesting.md) first — at 15 and 5 minutes
the same rule lost more money per trade, not less.

At 60 minutes the GIFT buckets close on the half hour (`08:30` closes at `09:30`),
so the first bar that closes inside the NSE session is the `08:30` one. That is
the bar the overnight reconcile runs on.

It also decides how long warm-up takes, because it is bars - not time - that
the indicators need: the longest SuperTrend wants 14 closed bars, which is
70 minutes at 5 minutes and 3.5 hours at 15. The bot sidesteps that on
startup by loading Kite history, so the delay only bites if history is
unavailable and the series has to be built from live ticks.

Bars are stored per interval, so switching this back and forth does not mix
two series together.

**`LOT_MARGIN` is an estimate.** LIVE prefers Kite's real margin API and reports
when it falls back to this number. Set it conservatively.

**There is no session loss cap.** It was removed: blocking new entries once the
day's realised loss reached a limit sounds prudent, but it is a second, hidden
strategy — it changes when the bot trades based on a number the strategy never
looked at. `MAX_OPEN_LOTS` and `MAX_DAILY_LOSS` are the exposure limits.

**`MAX_OPEN_LOTS` caps position size.** There is no stop-loss: the signal is the
only exit, so a single loss is not capped by a level. `MAX_DAILY_LOSS` blocks new
entries once the day's settled loss reaches it, and never flattens an open
position.

It is a **portfolio** budget, not a per-book size. Both books trade the same
contract, so the limit is shared: with two books enabled and `MAX_OPEN_LOTS=1`,
the first book to enter consumes the whole budget and the second is refused on
every signal with `2 lots would exceed MAX_OPEN_LOTS=1`. That looks exactly like
a strategy that has stopped working — the book evaluates, reports its signal, and
never places an order. The budget must be at least the sum of the enabled books'
sizes, and `config.validate()` warns at startup when it is not.

`STOP_LOSS_POINTS`, `TRAILING_STOP_ENABLED`, `TRAILING_INITIAL_POINTS`,
`TRAILING_STEP_POINTS`, `TRAILING_TIGHTEN_AT_POINTS`, `TRAILING_TIGHT_POINTS`,
`LARGE_CANDLE_ATR_MULTIPLIER` and `LARGE_CANDLE_MAX_POINTS` are **no longer
read**. There is no stop-loss at all: the signal is the only exit.

The signal exit is verified before it is reported: if the square-off did not
fill, the position stays open and the exit is retried on the next bar rather than
being forgotten. A position the bot believes it closed but did not is the worst
state this path can produce, so the confirmation is a `closed_positions` count
from the broker, not an assumption.

Re-entry after an exit needs a **fresh signal**. The bar that merely still agrees
with the previous one cannot open a trade, so the signal that was in force when
the position closed is never re-used — the conditions have to form again from
scratch. Nothing needs acknowledging for trading to continue.
