# Belaku — GIFT Nifty → Nifty Futures execution bot

Single-process algorithmic trading bot for the `Surikbs` Pine strategy
(`bot/st.pine`). It subscribes to **GIFT Nifty** ticks, builds candles, evaluates
the Pine conditions itself, and trades the **NFO Nifty future**. No TradingView
in the loop.

Requires Python 3.12 or newer.

**📖 Full documentation: <https://surikbs-xp.github.io/belaku/>**

## The one idea that explains the design

GIFT Nifty trades ~20 hours a day and is the standard pre-market proxy for
Nifty 50 — but it is an *index*, and cannot be traded. So Belaku reads signals
from GIFT Nifty and places orders on the NFO Nifty future. Everything else
follows from that split.

| Window | Behaviour |
|---|---|
| GIFT Nifty session (~20 h) | evaluate the strategy on every closed bar |
| NSE F&O session (09:15–15:30 IST) | LIVE orders may be placed; PAPER uses available prices |
| Outside NSE F&O hours | PAPER signals may simulate at the completed GIFT close; LIVE signals are **deferred and dropped** |

A signal computed on a 02:00 bar is not a signal at 09:15, so deferred signals
are never queued for the open.

## Quick start

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
test -f bot/.env || cp bot/.env.example bot/.env   # then edit bot/.env
.venv/bin/python bot/bot.py
```

Kite credentials are **required**, not optional — the GIFT Nifty feed runs on
`KiteTicker`, so without a token there is no data and no strategy.
`EXECUTION_MODE=PAPER` mocks *orders*, not market data.

Dashboard: <http://127.0.0.1:9000>

```bash
# Docker
docker compose up -d --build
```

See [Setup](https://surikbs-xp.github.io/belaku/setup) and
[Running](https://surikbs-xp.github.io/belaku/running) for the token flow, the
Docker volume layout, and health checks.

## Documentation

| Page | What is in it |
|---|---|
| [Introduction](https://surikbs-xp.github.io/belaku/introduction) | What the bot does, precisely, and its honest limitations |
| [Setup](https://surikbs-xp.github.io/belaku/setup) | Install, Kite credentials, getting an access token |
| [Running](https://surikbs-xp.github.io/belaku/running) | Local and Docker, rebuilds, logs, stopping |
| [Architecture](https://surikbs-xp.github.io/belaku/architecture) | Components, the two clocks, warm-up, signal lifecycle |
| [Strategy](https://surikbs-xp.github.io/belaku/strategy) | Triple SuperTrend + PSAR, and why they are computed locally |
| [Execution](https://surikbs-xp.github.io/belaku/execution) | Order path, safety gates, PAPER vs LIVE, margin, panic |
| [Configuration](https://surikbs-xp.github.io/belaku/configuration) | Every environment variable |
| [API](https://surikbs-xp.github.io/belaku/api) | The JSON endpoints |
| [Dashboard](https://surikbs-xp.github.io/belaku/dashboard) | Tabs, polling, asset caching, security |
| [Database](https://surikbs-xp.github.io/belaku/database) | What is stored, and why |
| [Schema reference](https://surikbs-xp.github.io/belaku/database/schema) | Every table, column and index |
| [Database operations](https://surikbs-xp.github.io/belaku/database/operations) | Inspecting, backup, pruning |
| [Performance](https://surikbs-xp.github.io/belaku/performance) | Measured resource use and what was fixed |
| [Testing](https://surikbs-xp.github.io/belaku/testing) | What the tests pin, and how to add one |

## Files

| File | Role |
|---|---|
| `bot/bot.py` | **The bot.** Entrypoint: tick feed, engine, mark loop, API, dashboard |
| `bot/gift_nifty.py` | GIFT Nifty instrument resolution, KiteTicker subscription, tick→candle aggregation |
| `bot/engine.py` | Evaluates the strategy on GIFT Nifty bars; routes orders to the Nifty future |
| `bot/strategy.py` | The Pine entry rules in Python (PSAR / triple SuperTrend) |
| `bot/order_manager.py` | Validation, margin, sizing, paper/live execution, pause/panic |
| `bot/kite_client.py` | KiteConnect wrapper; quotes, instrument lookup, mocked orders in PAPER |
| `bot/kite_auth.py` | Kite login callback, request-token exchange, and access-token persistence |
| `bot/database.py` | SQLite ledger: account, positions, orders, trades, equity curve, GIFT candles |
| `bot/config.py` | `.env` loading, `EXECUTION_MODE` toggle, all tunables |
| `bot/web/` | Chart-free vanilla-JS dashboard, served by `bot/bot.py` |
| `tests/` | Focused automated regression tests |
| `website/` | Docusaurus source for the documentation site |

## Tuning

| Setting | Effect |
|---|---|
| `BOT_NAME` | dashboard title and browser tab name (default `Belaku`) |
| `GIFT_CANDLE_INTERVAL` | bar size for the GIFT Nifty series (`5minute`, `15minute`…) |
| `ENGINE_INTERVAL_SECONDS` | how often the engine checks for a closed bar and marks PnL |
| `STRATEGY_SUPERTRENDS` | SuperTrend instances as `atr_length:multiplier` pairs (default `11:1,12:2,13:3`) |
| `STRATEGY_ST_MIN_AGREE` | how many SuperTrends must agree before a signal is allowed |
| `STRATEGY_PSAR_*` | Parabolic SAR start / increment / maximum |

The warm-up requirement is derived from the configured SuperTrend periods
(`longest ATR length + 1`), so it follows `STRATEGY_SUPERTRENDS` automatically.

If you change the timeframe in `st.pine`, change `GIFT_CANDLE_INTERVAL` to match
— the indicators are only meaningful on the timeframe they were tuned for.

## Tests

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q bot tests
```

## Safety

| Rule | Where |
|---|---|
| NSE session only, 09:15–15:30 IST, Mon–Fri | `kite_client.market_status`, enforced in `engine` and `order_manager` |
| LIVE front-month symbol and lot size verified against Kite's NFO instruments | `kite_client.KiteClient.front_month_contract` |
| Duplicate/replayed signals rejected | `order_manager._already_processed` |
| Stale signals rejected (default 300 s) | `order_manager._signal_age_seconds` |
| `MAX_OPEN_LOTS` and `LOT_MARGIN` enforced before every entry | `order_manager._execute_directional` |
| LIVE orders only placed inside the NSE F&O session; after-hours PAPER uses the GIFT close | `engine.StrategyEngine.execute` |
| Unresolved orders and broker/ledger mismatches block further LIVE actions | `order_manager.OrderManager` |
| Panic reports partial or failed square-offs instead of claiming success | `order_manager.OrderManager.square_off` |
| Panic pauses new entries | `order_manager.pause` / `OrderManager.paused` |

### Known limitations

* **No stop-loss, no profit target.** The Pine strategy defines entries only.
  Positions stay open until an opposite entry or a manual square-off. Size
  positions accordingly — this is a property of the strategy, not a missing
  feature.
* **The dashboard is unauthenticated.** Keep it on loopback or behind an
  authenticating proxy.
* **`bot/.env` is tracked in git.** Any credentials in it are in the repository
  history. Move it out of version control and rotate the keys before treating
  this as production. `.gitignore` already excludes `*.env*s`; the file was
  added before that rule.

## Building the docs

```bash
cd website
npm install
npm run start     # dev server at http://localhost:3000/belaku/
npm run build     # static output in website/build
```

Deployment to GitHub Pages is handled by
`.github/workflows/deploy-docs.yml` on pushes to `main` that touch `website/`.
