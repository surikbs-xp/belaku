---
title: API
---

# API

JSON over HTTP, served by FastAPI. Interactive docs at `/docs` while running.

Base URL: `http://127.0.0.1:9000`

## Status

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Liveness probe |
| `GET` | `/api/status` | Mode, uptime, Kite auth, market clock, feed + engine state, strategy config |
| `GET` | `/api/logs?limit=&level=&search=` | Tail of the bot's log file for the Logs tab |
| `GET` | `/api/live/readiness` | Every LIVE pre-flight check and what is blocking |
| `GET` | `/api/holidays` | NSE F&O closure list (`?refresh=true` to refetch) |

`/api/status` is the dashboard's main poll. It returns the engine's current and
previous verdicts, the feed state, and the resolved strategy configuration.

`/api/logs` reads only the last 512 KB of `LOG_FILE`, so a rotated 5 MB log
costs one seek rather than a full parse. `level` is a **minimum** severity —
`WARNING` also returns `ERROR` and `CRITICAL` — and a traceback stays attached
to the entry it explains instead of being filtered out as unparsed noise.
Anything matching `access_token=`, `api_secret=`, `request_token=`, `password=`
and friends is masked to `***` before it leaves the process, because the
dashboard is unauthenticated.

## Portfolio

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/portfolio` | Balance, margin utilisation, realised/unrealised PnL |
| `GET` | `/api/positions?status=all\|open\|closed` | Nifty futures positions |
| `GET` | `/api/equity?limit=` | Cumulative equity curve |
| `GET` | `/api/trades?limit=&mode=` | Settled round trips with fees and net PnL |
| `GET` | `/api/analytics` | Trade statistics (KPIs, streaks, breakdowns) |

## Ledger

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/orders?limit=&search=&mode=` | Order book |
| `GET` | `/api/signals?limit=` | Engine signal audit trail, including refusals |

## Journal

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/journal` | All trade notes |
| `PUT` | `/api/journal/{trade_id}` | Create or update a note |
| `DELETE` | `/api/journal/{trade_id}` | Delete a note |

:::note[Not surfaced in the UI]
These endpoints and the `trade_notes` table are functional, but the dashboard
no longer shows them. Kept because they are still served.
:::


## Kite

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/kite/status` | Token health |
| `POST` | `/api/kite/login-url` | Start the Zerodha login |
| `POST` | `/api/kite/session` | Exchange a `request_token` |
| `POST` | `/api/kite/feed/reconnect` | Restart market data with a valid token |

## Control

| Method | Path | Body | Purpose |
|---|---|---|---|
| `POST` | `/api/toggle-mode` | `{"mode":"PAPER\|LIVE","confirm":bool}` | Switch execution mode |
| `POST` | `/api/pause` | `{"paused":bool,"reason":str}` | Pause new entries |
| `POST` | `/api/panic` | `{"confirm":true,"reason":str}` | Square off and pause |

## Guards

Two are enforced server-side, not just in the UI:

- Switching to `LIVE` requires `confirm=true` **and** complete Kite credentials.
- `/api/panic` requires `confirm=true`.

Both return `4xx` otherwise.

## Example

```bash
curl -s localhost:9000/api/status | python -m json.tool
```

```json
{
  "mode": "PAPER",
  "bot_name": "Belaku",
  "kite": { "authenticated": true, "data_source": "KITE" },
  "engine": {
    "ready": true,
    "warmup_have": 1187,
    "warmup_needed": 14,
    "last_signal": "BULL",
    "current_values": {
      "psar": 24957.79,
      "supertrend_bull": 3,
      "supertrend_bear": 0,
      "supertrend_total": 3,
      "st_11_1": 25021.40,
      "st_12_2": 25004.26,
      "st_13_3": 24987.02
    }
  },
  "strategy_config": {
    "supertrend_label": "11×1 · 12×2 · 13×3",
    "min_agree": 2
  }
}
```

## Performance

The dashboard polls every 1.5 s, so the hot endpoints are cached:

| Endpoint | Typical latency |
|---|---|
| `/api/status` | 2–4 ms |
| `/api/portfolio` | 11–24 ms |
| `/api/positions`, `/api/orders`, `/api/trades`, `/api/signals` | 2–5 ms |

`/api/logs` is **not** on the 1.5 s poll — the Logs tab refreshes it every 3 s
and only while that tab is open, so an idle dashboard never reads the file.

See [Performance](/performance) for how that was achieved.
