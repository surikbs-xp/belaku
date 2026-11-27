---
title: Dashboard
---

# Dashboard

`bot/web/` is a no-build single-page app: Tailwind from CDN and plain ES2020 in
`app.js`. No bundler, no `node_modules`, no build step — edit and reload.

## Tabs

### Dashboard

The trading view. Top to bottom:

- **Market strip** — the signal source's price with a sparkline, the execution
  future with its lots, product, **expiry** and the contract it **rolls to**, and
  the future's own ticks with the basis against the signal source. Global rather
  than per-book: both books trade the same future and read the same two series,
  so repeating them per band would be four copies of two numbers.
- **One band per strategy** — side by side from 760px. Each band is a complete
  view of that book, because two books are two independent strategies and a
  shared panel would have to be read twice without ever saying which figure
  belonged to which:

  | Section | What it shows |
  |---|---|
  | **Head** | Name, signal-series tag (`GIFT` / `NIFTY`, shown only when the name does not already say it), interval, exit rule, and a running/paused/error pill, with the book's **LIVE/PAPER** toggle, **Pause/Resume**, **Square off** and manual **Buy**/**Sell** controls |
  | **Signal** | The verdict as a filled chip — green for a long book, red for a short one — with the band's left rail and border carrying the same direction so two bands compare by colour before either is read |
  | **Indicators & rule checks** | One row per indicator. Each states its level, its own direction, the distance price must travel to flip it, and whether it agrees with the verdict. A gap under a quarter of the bar's own range turns amber. The close the verdict used sits above the rows, **named with the series it came from** (`GIFT` / `NIFTY`) and followed by the execution future's own price and the basis between them. The levels are the *signal series*, which is an index and cannot be traded; the book holds the future, and the two differ by the basis — a few hundred points on a normal day. Without that label the index levels read as the position's own levels and never match the price Zerodha quotes for the contract |
  | **Account** | That book's own equity, net P&L, trade count, win rate and profit factor |
  | **Position** | What that book currently holds, or `Flat` |
  | **Exit** | Which signal event closes the book — `NO_SIGNAL` for the 15-minute book, `flip` for the 60-minute one. There is no stop-loss and no trailing stop, so there is no level to show or edit |

  Indicators and rule checks share one table because they are the same two
  numbers read two ways. A single `2 of 2 confirmed` count hides *which*
  indicator voted which way, so a split bar reads as a weaker version of an
  agreeing one; naming each rule and colouring it by its own direction makes the
  disagreement the visible thing. A split bar's rows say `split` rather than
  leaving a dash that reads as missing data.

  Every figure comes from that book's own ledger and its own verdict. The close
  in particular is the one *that run* computed — measuring a book's
  distance-to-flip against another book's price would be quietly wrong.
- **Alerting** — channel, target, whether it is armed, alerts sent, last alert
  kind and watchdog health, with a **Send test alert** button. A channel that is
  silently misconfigured looks exactly like a quiet market, so it is testable on
  demand.

There is no aggregate portfolio panel. Each band carries its own account, so a
page-level total repeated equity and P&L without answering anything a band did
not; the Performance tab carries the per-book totals.

There is no session loss cap panel, because there is no session loss cap — see
[Configuration](./configuration.md).

### Orders & signals

Searchable, mode-filtered order book plus the engine signal trail — placed,
held, deferred, and **why**. CSV export.

Both strategies execute on the NIFTY future, so there is one ledger and no venue
selector. A **book** filter scopes the tab to one strategy, and the signal trail
follows the same filter, so a book that traded always shows the decisions behind
those fills.

**Unresolved orders** get their own amber strip above the order book. They block
new entries — an order whose outcome is unknown could still fill, so stacking a
second one on top would double the exposure — and burying them in the execution
log left an operator staring at "Unresolved order" with no way to find the order
that caused it. The strip follows the mode filter, so viewing LIVE shows the LIVE
ones.

**Reconcile** resolves them against the Zerodha order book immediately, without a
restart, and cross-checks the trade book for fills with no local order row. It
targets the mode being viewed, not the process mode: an operator looking at LIVE
orders who presses Reconcile means the LIVE ones. An order the broker does not
know about is left unresolved and reported rather than assumed dead — it may be a
submission whose acknowledgement was lost.

### Performance & trades

Headline stats, a P&L calendar (green/red day cells with trade count and win
rate, holiday cells for NSE closures), and the trade ledger.

The headline tiles reuse the portfolio payload the 1.5 s poll already fetched,
cached in the page, so opening the tab never blanks the tiles while waiting for
the next poll.

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

Timestamps are IST, matching the rest of the dashboard. The container itself
runs in UTC, so the formatter converts rather than trusting the system clock.

## Polling

| Cadence | Endpoints |
|---|---|
| 1.5 s | `/api/status`, `/api/portfolio` |
| 3 s | `/api/logs` — **only while the Logs tab is open** |
| 5 s | positions, orders, trades, signals — both books |
| 20 s | Kite token health, LIVE readiness |
| 60 s | calendar rollup, holidays |

Polling pauses while the tab is hidden and resumes on focus. If the API goes
away the UI keeps the last values on screen, flags the failure, and recovers on
its own. The log tail has its own interval that starts when you open the Logs
tab and stops when you leave it, so an idle dashboard never touches the file.

## Design notes

**The indicator bars are one segment each.** There is a single SuperTrend
instance, so its bar is green when bullish and red when bearish rather than a
count across periods; PSAR is likewise one binary price-position check. The rule
confirmation still reads "2 of 2", because that counts the two *rules*, not
SuperTrend instances.

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

