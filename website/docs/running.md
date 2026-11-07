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
`no-new-privileges`, and it runs as an unprivileged user (`belaku`, uid 10001)
rather than root. That means **everything writable must be on a volume**:

| Path | Volume | Why |
|---|---|---|
| `/data/paper_trading.db` | `bot-data` | Trading state |
| `/data/bot.log` | `bot-data` | Logs |
| `/data/nse_holidays.json` | `bot-data` | Holiday cache |
| `/app/bot/.env` | bind mount | Credentials |

`compose.yaml` sets `DB_PATH`, `LOG_FILE` and `HOLIDAY_CACHE_PATH` to point at
`/data` for exactly this reason.

:::danger[Upgrading from a root-running image — read this first]
The container now runs as uid 10001. Docker sets a volume's ownership only when
it **creates** the volume, so a `belaku_bot-data` volume made by an earlier image
is still root-owned, and the unprivileged process cannot write `/data/bot.log`:

```
PermissionError: [Errno 13] Permission denied: '/data/bot.log'
```

The bot will crash-loop until this is fixed. Pick one:

```bash
# A. Recreate the volume — SIMPLEST, BUT DELETES THE DATABASE AND LOGS
docker compose down -v
docker compose up -d --build

# B. Keep the data: chown the volume once from the host, then it is owned
#    correctly for every future start
docker run --rm -v belaku_bot-data:/data alpine chown -R 10001:10001 /data
docker compose up -d --build
```

Option B is the one to use on a bot with real trade history.

Note that the container **cannot** repair this itself. `cap_drop: ALL` removes
`CAP_SETUID`/`CAP_SETGID` (so a root process cannot switch user) and
`CAP_DAC_OVERRIDE` (so it cannot write into a directory owned by another uid).
Being the right user from the start is the only workable arrangement, which is
why there is a `USER` directive instead of a chown-then-drop-privileges
entrypoint.
:::


:::note[The bind-mounted `.env` must be readable by uid 10001]
The container no longer runs as root, so `./bot/.env` has to be readable by the
container user. In practice that means either owning the file as uid 10001
(then `chmod 600` is right) or leaving it group/world-readable (`chmod 644`),
which is the usual situation on a desktop where the file is owned by your own
user account.

The token write-back (`kite_auth` persists a fresh token into `.env`) also needs
write access. If `.env` is not writable the bot still runs — it just keeps the
token in memory for that process only, which the callback page reports.
:::

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
