---
title: Performance
---

# Performance

Measured, not estimated. Numbers come from the cgroup CPU counter inside the
container, which is exact — `docker stats` samples and reports misleading spikes.

## Current numbers

| Metric | Value |
|---|---|
| **Average CPU** | **1.00%** of one core |
| Memory | ~110 MiB |
| `/api/status` | 2–4 ms |
| `/api/portfolio` | 11–24 ms |
| Ledger endpoints | 2–5 ms |

Measured over 90 s with the dashboard polling.

## What was wrong

The bot started at **8.91% CPU**. Profiling found five real problems, none of
which were "the strategy is slow".

### 1. `profile()` had no cache

`kite.profile()` is a network round-trip. The dashboard polls it every 1.5 s just
to render "Kite authenticated".

**54.7 ms per poll.** Now cached 15 s.

### 2. `engine.status()` evaluated the strategy twice per poll

Once for the forming bar, once for the closed bar — 30 ms, every 1.5 s. But the
inputs only change when a bar closes.

Verdicts are now memoised on `(bar timestamp, candle count)`. A poll is a dict
lookup.

### 3. Failed quotes were not cached

With an expired token, every `get_ltp` paid a doomed round-trip **plus three
retries with sleeps**. This was the largest hidden cost: `price_map()` took
52 ms.

Failures are now cached too (117 ms → 0 ms), while order paths pass `max_age=0`
for a fresh price.

### 4. `get_performance_metrics` loaded 5000 equity rows

To derive two numbers, on every 5 s mark tick. Drawdown is now computed in SQL
with a window function — verified to match the Python implementation to 1e-9 on
5000 real rows.

`_mark_equity`: 15.7 ms → 8.6 ms.

### 5. History refresh rebuilt ~1200 Candle objects every 30 s

Even when Kite returned identical data. Now fingerprinted on
`(newest closed bucket, row count)` and skipped when unchanged.

Also added exponential backoff (30 s → 300 s) so a dead token costs one attempt
per few minutes instead of one per 30 s.

## Frontend

Six requests every 1.5 s became two fast plus four on a 5 s cadence:

| Cadence | Endpoints |
|---|---|
| 1.5 s | status, portfolio |
| 5 s | positions, orders, trades, signals |

## Result

| | Before | After |
|---|---|---|
| Average CPU | 8.91% | **1.00%** |
| Memory | 124.6 MiB | 110.5 MiB |
| `/api/status` | 95–125 ms | **2–4 ms** |
| `/api/portfolio` | 70–120 ms | **11–24 ms** |

## Measuring it yourself

`docker stats` samples over a short window and reports spikes that are not real.
Use the cgroup counter:

```bash
read_cpu() {
  docker exec belaku-bot-1 sh -c 'cat /sys/fs/cgroup/cpu.stat' \
    | awk '/usage_usec/{print $2}'
}
a=$(read_cpu); sleep 60; b=$(read_cpu)
echo "avg CPU: $(echo "scale=2; ($b-$a)/1000000/60*100" | bc)%"
```

## Known remaining costs

- **`equity_curve` grows without bound** — 17k rows/day. See
  [pruning](/database/operations#pruning-the-equity-curve).
- **The mark loop writes every 5 s** even when nothing changed. Cheap now
  (8.6 ms), but it is the largest remaining periodic cost.
- **`VACUUM` is never run automatically**, so the file does not shrink after
  deletes.
