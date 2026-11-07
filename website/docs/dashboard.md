---
title: Dashboard
---

# Dashboard

`bot/web/` is a no-build single-page app: Tailwind from CDN and plain ES2020 in
`app.js`. No bundler, no `node_modules`, no build step — edit and reload.

## Tabs

### Dashboard

The trading view. Top to bottom:

- **Signal hero** — the current verdict (BULL / BEAR / NEUTRAL), accented by
  state, with the close, PSAR and SuperTrend vote counts.
- **Candle countdown** — time until the current 5-minute bar closes, anchored to
  the server's bucket start so a skewed client clock cannot shift it.
- **GIFT Nifty quote** — the signal source, with a client-side sparkline.
- **Execution panel** — the Nifty future, lots, product.
- **Live indicators** — SuperTrend vote bar (one segment per instance, labelled
  with its line value) and Parabolic SAR.
- **Account metrics** — portfolio value, real-time PnL, margin available with a
  utilisation bar, active mode.
- **Open positions** — with PnL and points.
- **Live rule checks** — exactly which of the two entry rules pass, for BUY and
  SELL separately.

### Orders & signals

Searchable, mode-filtered order book plus the engine signal trail — placed,
held, deferred, and **why**. CSV export.

### Performance & trades

Headline stats, a P&L calendar (green/red day cells with trade count and win
rate, holiday cells for NSE closures), and the trade ledger.

### Logs

The running process's log file, colour-coded by level (`ERROR` rows are tinted
red so a bad night is scannable rather than readable).

- **Level filter** is a *minimum* — `Warning +` also shows errors and criticals.
- **Tracebacks stay attached** to the `ERROR` above them, so filtering by level
  never leaves orphaned stack frames.
- **Search** filters the already-tailed lines by substring.
- **Follow** sticks to the newest line and unchecks itself when you scroll up,
  so a refresh does not yank the view away from what you are reading.
- **Wrap** re-flows long lines so a 600-character JSON indicator payload is
  readable instead of one endless scroll. On by default; uncheck it to keep
  columns aligned when scanning levels.
- **Copy** / **Download** take the currently filtered tail.

Credentials are masked server-side (`access_token=***`) before the lines are
sent, because this page is unauthenticated. See
[API → `/api/logs`](/api#status) for the details.

## Polling

| Cadence | Endpoints |
|---|---|
| 1.5 s | `/api/status`, `/api/portfolio` |
| 3 s | `/api/logs` — **only while the Logs tab is open** |
| 5 s | positions, orders, trades, signals |
| 20 s | Kite token health, LIVE readiness |
| 60 s | calendar rollup, holidays |

Polling pauses while the tab is hidden and resumes on focus. If the API goes
away the UI keeps the last values on screen, flags the failure, and recovers on
its own. The log tail has its own interval that starts when you open the Logs
tab and stops when you leave it, so an idle dashboard never touches the file.

## Design notes

**The vote bar is per-instance, not aggregate.** Three segments, each labelled
with its own SuperTrend line value and tinted by its own direction. An aggregate
"2 of 3" hides which period dissented.

**The countdown is server-anchored.** The client measures elapsed time from the
server's bucket start rather than trusting its own clock.

**Refusals are visible.** The signal trail shows blocked signals with reasons, so
a quiet bot is explainable rather than mysterious.

## Security

The frontend is unauthenticated and binds to `WEBHOOK_HOST` (default
`127.0.0.1`). Keep it on loopback, or put it behind an authenticating reverse
proxy before exposing it — anyone who can reach `/api/panic` or
`/api/toggle-mode` can move real money.

What the server does enforce, so a stray page or a rebinding hostname cannot
drive it:

| Control | Effect |
|---|---|
| `TrustedHostMiddleware` | A `Host` outside `ALLOWED_HOSTS` gets `400` (DNS rebinding) |
| Origin check on writes | A cross-site `POST`/`PUT`/`DELETE` gets `403` (CSRF) |
| `Content-Security-Policy` | `default-src 'self'`, `frame-ancestors 'none'`, `form-action 'none'` |
| `X-Frame-Options: DENY` | The confirm dialog cannot be clickjacked |
| `/docs` disabled | The control surface is not enumerated |

These are hardening, not authentication — see
[Configuration](/configuration) for the honest version of that caveat.

## Asset caching

`index.html` links assets with a version query:

```html
<link rel="stylesheet" href="/static/styles.css?v=trader-8" />
<script src="/static/app.js?v=trader-8" defer></script>
```

:::warning[Bump the version when you change CSS or JS]
The server sends no cache headers, so the browser will serve a stale copy
under an unchanged URL. If a change appears to do nothing, this is why —
bump `?v=` and reload.
:::

