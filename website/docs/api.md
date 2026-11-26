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
Both strategies execute on the NIFTY future, so there is one ledger and no
venue parameter. A `venue` switch that silently changed ledgers would make a
wrong number look like a real one.

## Ledger

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/orders?limit=&search=&mode=` | Order book |
| `GET` | `/api/orders/pending?mode=` | Orders the broker has not finished with. These block new entries, so they are surfaced separately from the execution log. |
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
| `POST` | `/api/toggle-mode` | `{"mode":"PAPER\|LIVE","confirm":bool}` | Switch the process-wide execution mode |
| `POST` | `/api/pause` | `{"paused":bool,"reason":str}` | Pause new entries for every book |
| `POST` | `/api/panic` | `{"confirm":true,"reason":str}` | Square off every book and pause |
| `POST` | `/api/stop-all` | `{"confirm":true,"reason":str}` | Emergency halt: block new entries in every book, close nothing. Latches across restarts. |
| `POST` | `/api/stop-all/clear` | — | Lift the STOP ALL latch |
| `POST` | `/api/orders/reconcile?mode=` | — | Resolve unresolved orders against the Zerodha order book now, and cross-check the trade book. `mode` defaults to the process mode; the dashboard passes the mode it is showing. |
| `POST` | `/api/books/mode` | `{"strategy_id":str,"mode":"PAPER\|LIVE","confirm":bool}` | Switch one book's mode |
| `POST` | `/api/books/pause` | `{"strategy_id":str,"paused":bool,"reason":str}` | Pause or resume one book |
| `POST` | `/api/books/square-off` | `{"strategy_id":str,"confirm":true,"reason":str}` | Square off one book, then pause it |
| `POST` | `/api/books/stop` | `{"strategy_id":str,"stop":float,"confirm":true}` or `{"strategy_id":str,"distance":float,"confirm":true}` | Move one book's stop, by price or by points from the live mark. Tightening only; a looser level is refused. |
| `POST` | `/api/books/manual-order` | `{"strategy_id":str,"action":"BUY\|SELL","qty_lots":int,"confirm":true}` | Place a manual entry for one book |

The per-book endpoints are what the two strategy panels use. Each is scoped to
one `strategy_id`, so pausing or squaring off one book never touches the other's
position in the same contract. `/api/books/mode` refuses `LIVE → PAPER` while
that book still holds a live position, and requires `confirm=true` to arm `LIVE`.

`/api/books/manual-order` is routed through the same path as an automated
signal, so a manual entry inherits the NSE session gate, the pause switch, the
duplicate-entry guard, the lot and margin limits, the rollover rules and the
book's own initial stop. The operator chooses the direction; the risk rules are
not optional. It requires `confirm=true` because in `LIVE` it places a real
order.

Stops are configured per strategy with `STRATEGY_<n>_STOP_LOSS` and the trail
keys, read from the environment rather than edited at runtime.

## Guards

Enforced server-side, not just in the UI:

- Switching to `LIVE` (globally or per book) requires `confirm=true` **and**
  complete Kite credentials, and passes the pre-flight report.
- `/api/panic`, `/api/books/square-off` and `/api/books/manual-order` require
  `confirm=true`.
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
