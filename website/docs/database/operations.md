---
title: Operations
---

# Operations

Day-to-day handling of the database: inspecting it, backing it up, resetting it,
and fixing the things that go wrong.

## Inspecting

The file is plain SQLite, so any client works. With the `sqlite3` CLI:

```bash
# Local
sqlite3 bot/paper_trading.db

# Docker (the file is on the bot-data volume)
docker compose exec bot sqlite3 /data/paper_trading.db
```

Useful queries:

```sql
-- Open positions
SELECT id, symbol, position_key, quantity, entry_price, mode
FROM positions WHERE status = 'OPEN';

-- Realised PnL by day
SELECT substr(exit_time, 1, 10) AS day,
       COUNT(*) AS trades,
       ROUND(SUM(net_pnl), 2) AS net
FROM trades GROUP BY day ORDER BY day DESC;

-- Why the bot did not trade
SELECT ts, action, accepted, reason
FROM signals WHERE accepted = 0 ORDER BY id DESC LIMIT 20;

-- Bar quality
SELECT partial, COUNT(*) FROM gift_candles GROUP BY partial;

-- Table sizes
SELECT 'positions' AS t, COUNT(*) FROM positions
UNION ALL SELECT 'orders', COUNT(*) FROM orders
UNION ALL SELECT 'trades', COUNT(*) FROM trades
UNION ALL SELECT 'equity_curve', COUNT(*) FROM equity_curve
UNION ALL SELECT 'signals', COUNT(*) FROM signals
UNION ALL SELECT 'gift_candles', COUNT(*) FROM gift_candles;
```

## Backup

The database is one file, but WAL mode means recent writes may still be in
`-wal`. Copying only the `.db` can lose them.

**Safe, while running** — use SQLite's own backup, which is WAL-aware:

```bash
# Local
sqlite3 bot/paper_trading.db ".backup /tmp/belaku-$(date +%F).db"

# Docker
docker compose exec bot sqlite3 /data/paper_trading.db ".backup /data/backup.db"
docker compose cp bot:/data/backup.db ./belaku-$(date +%F).db
```

**Safe, while stopped** — copy all three files together:

```bash
docker compose stop bot
docker run --rm -v belaku_bot-data:/data -v "$PWD":/out alpine \
  sh -c 'cp /data/paper_trading.db* /out/'
docker compose start bot
```

:::danger[Never copy just the `.db` while the bot is running]
You will get a file that is missing the most recent transactions, or one
that fails `PRAGMA integrity_check`. Use `.backup` or stop the bot first.
:::


## Reset

`reset_account()` wipes trading state and restores the virtual balance. It
clears `positions`, `orders`, `trades`, `equity_curve` and `signals` — but
**keeps** `gift_candles`, so the bot does not have to re-warm.

From Python:

```python
import database as db
db.reset_account(initial_balance=200_000)
```

To wipe everything including candles, delete the file:

```bash
# Local
rm bot/paper_trading.db bot/paper_trading.db-wal bot/paper_trading.db-shm

# Docker
docker compose down
docker volume rm belaku_bot-data
docker compose up -d
```

## Pruning the equity curve

`equity_curve` is the only table that grows without bound — one row every 5 s,
about 17k/day. Nothing reads more than the recent window plus an aggregate, so
old samples are safe to drop.

```sql
-- Keep the last 30 days
DELETE FROM equity_curve
WHERE ts < datetime('now', '-30 days');

-- Reclaim the space (WAL needs this to actually shrink the file)
VACUUM;
```

Run it while the bot is stopped, or accept a brief write lock. `VACUUM` rewrites
the whole file and needs free disk equal to the database size.

## Troubleshooting

### `attempt to write a readonly database`

The container runs `read_only: true` and drops `CAP_DAC_OVERRIDE`, so it cannot
write a file it does not own. This happens when a file was copied in with
`docker compose cp`, which preserves the host uid.

```bash
docker run --rm -v belaku_bot-data:/data alpine chown 0:0 /data/*.db
# remove stale WAL sidecars so SQLite rebuilds them
docker run --rm -v belaku_bot-data:/data alpine \
  sh -c 'rm -f /data/*.db-shm /data/*.db-wal'
docker compose restart bot
```

### `database is locked`

Every write uses `BEGIN IMMEDIATE` with a 5 s busy timeout, so this means a
transaction is being held open too long — usually a network call inside a
`with session(...)` block. Writes must never span I/O.

Check for a stuck process:

```bash
docker compose exec bot sh -c 'ls -l /proc/*/fd 2>/dev/null | grep paper_trading'
```

### The dashboard shows stale numbers

The API reads live, so stale data means the writer stopped. Check the mark loop
heartbeat:

```bash
curl -s localhost:9000/api/status | python -m json.tool | grep -A5 heartbeat
```

`age_seconds` above `3 × interval_seconds` means the mark thread died.

### `no such table: trade_notes`

The file predates the journal table. `init_db()` creates it on the next
connection — restart the bot, or call `db.init_db()` directly.

### Verifying integrity

```bash
sqlite3 bot/paper_trading.db "PRAGMA integrity_check;"
# expect: ok
```

## What is safe to delete

| Path | Safe to delete? | Effect |
|---|---|---|
| `paper_trading.db` | Yes | Loses all history; re-warms from Kite |
| `paper_trading.db-wal` | **No** | Loses uncommitted transactions |
| `paper_trading.db-shm` | Only when stopped | SQLite rebuilds it |
| `bot/data/nse_holidays.json` | Yes | Refetched from NSE on next start |
| `bot/logs/` | Yes | Log history only |
