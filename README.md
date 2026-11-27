# Belaku — two-strategy NIFTY futures execution bot

Single-process algorithmic trading bot for the `Surikbs` Pine strategy
(`bot/st.pine`). It runs **two independent strategies** — **GIFT NIFTY 15m** and
**NIFTY 60m** — each on its own signal series and timeframe, and both execute on
the **NFO Nifty future**. No TradingView in the loop.

Requires Python 3.12 or newer.

**📖 Full documentation: <https://surikbs-xp.github.io/belaku/>**

## The two strategies

| | GIFT NIFTY 15m | NIFTY 60m |
|---|---|---|
| Signal source | `NSEIX:GIFT NIFTY` | `NSE:NIFTY 50` |
| Timeframe | 15 minutes | 60 minutes |
| Execution | NIFTY futures | NIFTY futures |
| Stop-loss | none — the signal is the only exit | none — the signal is the only exit |

Each computes its signal independently on its own series and timeframe. Every
stop is measured on the **futures execution price**, never on the signal index,
and it only ever moves in the profitable direction — it never widens. It is
checked on the live futures price every mark cycle, not on a bar close, and it
stays active while the book is paused.

Each book has its own **LIVE/PAPER** switch, **Pause/Resume** and **Square off**
on the dashboard. Switching one never affects the other, and the mode choice is
persisted across restarts.

| Window | Behaviour |
|---|---|
| NSE F&O session (09:15–15:30 IST) | LIVE orders may be placed; PAPER uses available prices |
| Outside NSE F&O hours | PAPER signals may simulate at the completed close; LIVE signals are **deferred and dropped** |

A signal computed on a pre-open bar is not a signal at 09:15, so deferred signals
are never queued for the open.

## Quick start

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
test -f bot/.env || cp bot/.env.example bot/.env   # then edit bot/.env
.venv/bin/python bot/bot.py
```

`requirements-dev.txt` is the runtime file plus pytest; use `requirements.txt`
alone for a deployment that does not run the tests. The container installs only
the runtime file.

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
| [Production](https://surikbs-xp.github.io/belaku/production) | Unattended runbook: daily token, backups, pre-flight checks |
| [Hosting](https://surikbs-xp.github.io/belaku/hosting) | Choosing an always-on VM, deploying, remote access, migrating off a laptop |
| [Lightsail containers](https://surikbs-xp.github.io/belaku/lightsail) | The GitHub Actions pipeline that builds, pushes and deploys to a Lightsail container service |
| [Architecture](https://surikbs-xp.github.io/belaku/architecture) | Components, the two clocks, warm-up, signal lifecycle |
| [Strategy](https://surikbs-xp.github.io/belaku/strategy) | SuperTrend 14×3 + PSAR, and why they are computed locally |
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
| `bot/strategy.py` | The Pine entry rules in Python (PSAR / SuperTrend 14×3) |
| `bot/order_manager.py` | Validation, margin, sizing, paper/live execution, pause/panic |
| `bot/fill.py` | Realistic paper fill model — walks the visible order book, partial fills, adverse-tick fallback |
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
| `ENGINE_INTERVAL_SECONDS` | how often the engine checks for a closed bar and marks PnL |
| `STRATEGY_PSAR_START` / `_INCREMENT` / `_MAXIMUM` | Parabolic SAR inputs (default `0.02` / `0.02` / `0.2`) |

The strategy uses exactly two indicators — SuperTrend (14-period ATR × 3, fixed in
code) and Parabolic SAR — so the warm-up requirement is a constant **15 closed
bars**.

Each strategy's `STRATEGY_<n>_INTERVAL` must match the timeframe it was tuned on
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
| `MAX_DAILY_LOSS` blocks new entries once the day's realised loss reaches it | `risk.daily_loss_breach`, checked in `engine.evaluate_run` |
| LIVE orders only placed inside the NSE F&O session; after-hours PAPER uses the GIFT close | `engine.StrategyEngine.execute` |
| Unresolved orders are reconciled against the Zerodha order book, not assumed | `order_manager.OrderManager.reconcile_orders` |
| Broker/ledger mismatches block further LIVE actions | `order_manager.OrderManager` |
| Panic reports partial or failed square-offs instead of claiming success | `order_manager.OrderManager.square_off` |
| Emergency STOP ALL latches across restarts and blocks every entry | `order_manager.OrderManager.stop_all` |
| Dashboard requires a password when `DASHBOARD_PASSWORD` is set | `dashboard_auth`, enforced in `bot.security_middleware` |

### Known limitations

* **The signal is the only exit.** There is no stop-loss and no trailing stop
  (see [Strategy](https://surikbs-xp.github.io/belaku/strategy)), so a single loss
  is not capped by a level and there is no profit target: a winning trend is
  closed by the signal, not by a fixed level.
* **The dashboard is open when `DASHBOARD_PASSWORD` is empty.** Set it for any
  deployment that is not on loopback; the Lightsail deploy refuses to build a
  spec without it.
* **`bot/.env` is not tracked in git.** `.gitignore` excludes `*.env*s` and only
  `bot/.env.example` is committed. If an older clone ever committed a real
  `.env`, rotate those keys — see `scripts/purge-secrets-from-history.sh`.

## Building the docs

```bash
cd website
npm install
npm run start     # dev server at http://localhost:3000/belaku/
npm run build     # static output in website/build
```

Deployment to GitHub Pages is handled by
`.github/workflows/deploy-docs.yml` on pushes to `main` that touch `website/`.
