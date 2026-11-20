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

Log lines are stamped in **IST**, not the container clock. The image runs in
UTC, so a UTC stamp would sit 5h30m behind every other time on the dashboard and
make a fresh entry look stale. Lines written before this was fixed keep their
original UTC stamps.

Anything matching `access_token=`, `api_secret=`, `request_token=`,
`password=`, or the SDK's `Authorization: token <key>:<token>` header is masked
to `***` **before** the search filter runs, so `?search=` cannot be used to
confirm a secret that the response itself hides. The Kite SDK's own loggers are
additionally pinned to `WARNING`, because at `DEBUG` it prints the
`Authorization` header.

## Portfolio

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/portfolio` | Balance, margin utilisation, realised/unrealised PnL |
| `GET` | `/api/positions?status=all\|open\|closed` | Nifty futures positions |
| `GET` | `/api/equity?limit=` | Cumulative equity curve |
| `GET` | `/api/trades?limit=&mode=` | Settled round trips with fees and net PnL |
| `GET` | `/api/analytics` | Trade statistics (KPIs, streaks, breakdowns) |
| `GET` | `/api/mirror` | Shadow paper book: sync state, live vs paper comparison |
| `GET` | `/api/venues` | Both books side by side — GIFT Nifty index vs Nifty future |
| `GET` | `/api/gift/positions` | GIFT Nifty index-book positions (paper only) |
| `GET` | `/api/gift/trades` | Settled round trips in the index book |
| `GET` | `/api/gift/orders` | Index-book order log |
| `GET` | `/api/gift/signals` | Index-book signal audit trail, including refusals |
| `GET` | `/api/gift/summary` | Index-book health and metrics |
| `GET` | `/api/gift/analytics` | Index-book trade statistics, shaped like `/api/analytics` |
| `GET` | `/api/gift/portfolio` | Index-book account metrics, shaped like `/api/portfolio` |

`/api/gift/analytics`, `/api/gift/portfolio` and `/api/gift/signals` are separate
endpoints rather than a `venue` parameter on the futures ones. The two books are
different databases, and a query parameter that silently switched ledgers would
make a wrong number look like a real one. All of them report `enabled: false`
when `GIFT_BOOK` is off, which is a normal state for the UI to render rather than
a 500.

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
`/api/risk/trailing` was removed along with the trailing stop, and the session
loss cap was removed with it. There is no per-trade stop and no session limit to
configure: the exit is the pair flipping, and `MAX_OPEN_LOTS` is the only
exposure limit. Both are read from the environment rather than edited at runtime.

## Guards

Enforced server-side, not just in the UI:

- Switching to `LIVE` requires `confirm=true` **and** complete Kite credentials.
- `/api/panic` requires `confirm=true`.
- A state-changing request (`POST`/`PUT`/`DELETE`) whose `Origin` is not the
  bot itself gets `403`.
- A `Host` outside `ALLOWED_HOSTS` gets `400`.
- `reason` on `/api/pause` and `/api/panic` is restricted to a single safe line.
- `/docs` and `/openapi.json` are `404` unless `ENABLE_API_DOCS=1`.

Violations return `4xx`.

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
    "warmup_needed": 15,
    "last_signal": "BULL",
    "current_values": {
      "psar": 24957.79,
      "supertrend": 25021.40,
      "supertrend_bullish": true,
      "supertrend_bearish": false,
      "st_14_3": 25021.40
    }
  },
  "strategy_config": {
    "supertrend": { "length": 14, "multiplier": 3.0 },
    "supertrend_label": "14×3",
    "confirmations_total": 2
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
