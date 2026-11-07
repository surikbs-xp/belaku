---
title: Running
---

# Running

## Local

```bash
.venv/bin/python bot/bot.py
```

Dashboard: [http://127.0.0.1:9000](http://127.0.0.1:9000)

The process runs four threads:

| Thread | Job |
|---|---|
| Main | FastAPI/uvicorn — serves the dashboard and JSON API |
| Tick feed | `KiteTicker` websocket → GIFT Nifty candles |
| Strategy engine | Evaluates the strategy on each closed bar |
| Mark loop | Marks positions to market every `ENGINE_INTERVAL_SECONDS` |

## Docker

```bash
docker compose up -d --build
docker compose logs -f bot
```

The image is hardened: `read_only: true`, `cap_drop: ALL`,
`no-new-privileges`. That means **everything writable must be on a volume**:

| Path | Volume | Why |
|---|---|---|
| `/data/paper_trading.db` | `bot-data` | Trading state |
| `/data/bot.log` | `bot-data` | Logs |
| `/data/nse_holidays.json` | `bot-data` | Holiday cache |
| `/app/bot/.env` | bind mount | Credentials |

`compose.yaml` sets `DB_PATH`, `LOG_FILE` and `HOLIDAY_CACHE_PATH` to point at
`/data` for exactly this reason.

### Rebuilding after a code change

```bash
docker compose up -d --build
```

The container restarts and reloads its warm-up from `gift_candles`, so it is
immediately ready rather than waiting ~70 minutes.

### Health

```bash
docker compose ps          # expect: Up (healthy)
curl -s localhost:9000/api/health
```

## Stopping

```bash
docker compose stop        # keeps the volume
docker compose down        # keeps the volume
docker compose down -v     # DELETES the database
```

## Logs

```bash
# Local
tail -f bot/logs/bot.log

# Docker
docker compose logs -f bot
```

The engine logs one line per completed candle with the OHLC input, history
length, and every indicator value — enough to reproduce a verdict offline.

### In the dashboard

The **Logs** tab tails the same file in the browser, with level and text
filters, follow-on-scroll, and copy/download. It is the same stream as
`tail -f`; the browser just adds colour and filtering. See
[Dashboard → Logs](/dashboard#logs).

Credentials are masked server-side before the lines are sent, but the endpoint
has no authentication of its own — keep the dashboard on loopback or behind an
authenticating proxy, as the [Configuration](/configuration) warning says.

## Next

- [Configuration](/configuration)
- [Database operations](/database/operations)
- [Docs site](/docs-site)
