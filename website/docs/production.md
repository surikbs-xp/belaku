---
sidebar_position: 3
---

# Production deployment

This is the runbook for running Belaku unattended. It assumes you have read
[Running the bot](./running.md) and have a working `docker compose up`.

:::danger Read this before going live

Two things must be true before this bot trades real money:

1. **The Kite API secret has been rotated.** An earlier `bot/.env` was committed
   to a public repository. See [Credential rotation](#credential-rotation).
2. **`DASHBOARD_PASSWORD` is set.** Without it the control API is open to
   anything that can reach the port, including `/api/panic` and the endpoint
   that arms LIVE mode.

:::

## What "production" means here

The bot is a single process with four threads: the GIFT Nifty tick feed, the
strategy engine, the mark-to-market loop, and the watchdog. It needs no inbound
traffic — it pulls market data itself — so it can run behind a firewall with no
public IP.

| Requirement | Why |
| --- | --- |
| Always-on host | The feed must be connected before 09:15 IST |
| ~1 GB RAM, 1 vCPU | Python plus pandas; the workload is tiny |
| Persistent disk | SQLite holds the whole trading record |
| Outbound HTTPS | Kite REST and the websocket |
| A daily login | Kite tokens expire around 06:00 IST — see below |

## The daily token problem

**This is the real obstacle to running 365 days a year, not hosting.**

Kite has no headless login. The flow is a browser redirect: you authenticate,
Zerodha redirects to your registered Redirect URL with a `request_token`, and the
bot exchanges it for an `access_token` valid until roughly 06:00 IST the next
day. There is no API to automate the interactive step, and automating it with a
stored TOTP secret is a credential-handling decision you should make
deliberately, not by accident.

What the bot does about it:

- It keeps running on cached candles when the token is stale, rather than
  crashing. A stale token is the normal morning condition.
- The dashboard shows the token state and a **Connect** button.
- The watchdog alerts if the feed is down during market hours.

What you must do: **log in each morning before 09:15 IST.** Register
`http://127.0.0.1:9000/zerodha/callback` as the Redirect URL in the Kite console
and the bot captures the token automatically — no copy-paste.

If you cannot log in daily, this bot cannot run unattended. Plan for it.

## Host setup

:::tip[Choosing and preparing the machine]

This page assumes the host already exists. For picking one, hardening a fresh
VM, reaching the dashboard without exposing the port, and migrating the database
off a laptop, see [Hosting](./hosting.md).

:::

### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
```

### 2. Clone and configure

```bash
sudo mkdir -p /opt/belaku && sudo chown "$USER" /opt/belaku
git clone https://github.com/surikbs-xp/belaku /opt/belaku
cd /opt/belaku
cp bot/.env.example bot/.env
```

Edit `bot/.env` and set at minimum:

```bash
KITE_API_KEY=...
KITE_API_SECRET=...
DASHBOARD_PASSWORD=<a long random string>
ALERT_ENABLED=1             # 0 = silent; switch on only while testing
ALERT_CHANNEL=telegram      # telegram | slack | discord | ntfy | generic
ALERT_TELEGRAM_BOT_TOKEN=   # if telegram
ALERT_TELEGRAM_CHAT_ID=
```

Generate a password with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

### 3. Start it

```bash
docker compose up -d --build
docker compose logs -f bot
```

### 4. Install the systemd unit

```bash
sudo cp deploy/belaku.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now belaku
systemctl status belaku
```

The unit starts the stack on boot and restarts it if the daemon loses it. The
container's own `restart: unless-stopped` covers the container process; the unit
covers the stack.

## Credential rotation

The API secret was committed to a public repository and must be treated as
compromised. Rotating it is the only real fix.

1. Open the [Kite developer console](https://developers.kite.trade/) and
   **regenerate the API secret**.
2. Update `KITE_API_SECRET` in `bot/.env`.
3. Re-authenticate to mint a fresh access token.
4. Optionally, purge the value from git history so it cannot be re-discovered:

   ```bash
   scripts/purge-env-from-history.sh --dry-run
   scripts/purge-env-from-history.sh --apply
   git push --force --all
   ```

   This does **not** un-publish the value. Anyone who cloned the repository
   already has it. Rotate first, purge second.

## Backups

The database holds positions, orders, trades, the equity curve and journal
notes. It lives in a Docker volume, so losing the volume loses all of it.

```bash
scripts/backup-db.sh
```

The script uses SQLite's `.backup` (not `cp`, which can capture a torn write and
miss the WAL sidecar), verifies `PRAGMA integrity_check` on the result, and
prunes snapshots older than `KEEP_DAYS`.

Schedule it after the close:

```cron
30 16 * * 1-5 cd /opt/belaku && ./scripts/backup-db.sh >> /opt/belaku/backups/backup.log 2>&1
```

:::warning

This writes to the same machine. Copy the backups off-box, or they die with the
host.

:::

## Monitoring

The container healthcheck answers "is the process alive?". That is not the same
question as "is it trading?". The watchdog covers the difference:

| Alert | Meaning |
| --- | --- |
| `STALE_FEED` | Feed connected but no tick for 3 minutes during market hours |
| `FEED_DOWN` | Feed not connected during market hours |
| `STOP_OUT` | A position was closed by the signal flipping (the strategy keeps running) |
| `PAUSED` | Strategy paused and nobody resumed it |
| `ENGINE_ERROR` | The engine recorded an error |
| `ORDER_REJECTED` | The bot decided to trade and the order failed |
| `EXIT_UNCONFIRMED` | A signal exit could not be confirmed — a position may still be open |
| `TOKEN_EXPIRED` | Kite session is not authenticated; the bot cannot place orders |
| `BOT_STARTED` / `BOT_STOPPED` | Lifecycle, so a silent restart is visible |

Set `ALERT_ENABLED=1`, point `ALERT_CHANNEL` at `telegram`, `slack`, `discord`,
`ntfy` or `generic`, and supply that channel's credentials. A fill and a signal
flip are both events, so the bot messages a lot: enable it to verify the channel,
then set `ALERT_ENABLED=0` again. **The channel must match the destination**:
each service rejects a payload it does not recognise (Slack wants `text`,
Discord `content`, ntfy a plain body), so a mismatched pair fails silently at the
service while the bot believes it sent the alert.

Verify it on demand with **Send test alert** on the dashboard, or:

```bash
curl -X POST http://127.0.0.1:9000/api/alerts/test
```

Alerts are rate-limited to one per kind per 15 minutes, and only fire after three
consecutive failing checks, so a single slow poll does not page you. Watchdog
state is on `/api/status` under `watchdog`; channel config is on
`/api/alerts/status` (the token is never returned).

## Risk limits

The signal is the only exit: a position is held for as long as SuperTrend and
PSAR agree and closed on the first bar they stop. There is **no stop-loss and no
trailing stop**, so a single loss is not capped by a level. There is no profit
target.

| Setting | Effect |
| --- | --- |
| `MAX_OPEN_LOTS` | Reject any order that would exceed this many lots |
| `MAX_DAILY_LOSS` | Block new entries once the day's realised loss reaches this many rupees. `0` disables it. Never flattens an open position. |

There is no session loss cap. It was removed because blocking entries on the day's
realised loss is a second strategy the signal never looked at — it changes when
the bot trades for a reason the strategy cannot see. `MAX_OPEN_LOTS` is the only
exposure limit.

The exit is verified before it is reported — if the square-off did not fill, the
position stays open and the exit is retried on the next bar rather than being
forgotten. A position the bot believes it closed but did not is the worst state
this path can produce.

Re-entry needs a **fresh signal**, not the state that was already in force. A bar
that merely still agrees with the previous one cannot open a trade, so an exit is
never undone by the same signal that caused it. The conditions have to form again
from scratch: for a BUY, the combined SuperTrend-and-PSAR condition must become
true again after the exit.

:::warning[What the exit costs]
Measured on 853 real 15-minute GIFT Nifty bars: 42 trades, 38% win rate, average
win 106 points and average loss 61 points. Gross **+₹6,792**; after ₹40,539 of
round-trip charges, **−₹33,747**. The old 100/30/5 trailing rule on the same bars
produced −₹41,829 net, so the signal exit is the better of the two — but neither
is profitable at one lot. The charge stack, not the exit rule, is the binding
constraint. See [Performance](./performance.md).
:::

A signal that closes while the NSE is shut is *latched* as an owed entry and
honoured on the first bar that can fill it. The latch is persisted, so a deferred
entry survives a restart without anyone intervening.

For Nifty, one point is ₹1 per unit, so 65 units × 100 points = ₹6,500.

## Upgrading

```bash
cd /opt/belaku
git pull
docker compose up -d --build
```

If you are upgrading from an image that ran as root, the `/data` volume is
root-owned and the non-root container will crash-loop with
`PermissionError: '/data/bot.log'`. Repair it once:

```bash
docker compose down
docker run --rm -v belaku_bot-data:/data alpine chown -R 10001:10001 /data
docker compose up -d
```

## Security posture

| Control | Status |
| --- | --- |
| Dashboard authentication | `DASHBOARD_PASSWORD`, signed HttpOnly SameSite=Strict cookie |
| Login rate limiting | 5 attempts per 5 minutes per client |
| CSRF | Origin check plus SameSite=Strict |
| DNS rebinding | `ALLOWED_HOSTS` allow-list |
| Container | uid 10001, `cap_drop: ALL`, `read_only`, `no-new-privileges` |
| Port binding | `127.0.0.1:9000` — loopback only |
| API docs | Disabled unless `ENABLE_API_DOCS=1` |
| Log redaction | Credentials masked in `/api/logs` |

:::warning No TLS

The dashboard is served over plain HTTP. The session cookie is therefore not
marked `Secure`, and the password crosses the network in clear text. This is
acceptable on loopback or a private network. **Put TLS in front before exposing
it**, and set `secure=True` on the cookie once you do.

:::

## Pre-flight checklist

- [ ] Kite API secret rotated
- [ ] `DASHBOARD_PASSWORD` set to a long random value
- [ ] `ALERT_ENABLED=1`, `ALERT_CHANNEL` set, credentials supplied, and a test
      alert received — then `ALERT_ENABLED=0` again for normal running
- [ ] `scripts/backup-db.sh` scheduled and copied off-box
- [ ] systemd unit installed and enabled
- [ ] Log rotation confirmed (`/data/bot.log`, 5 MB × 5)
- [ ] Daily login routine established
- [ ] Ran in PAPER mode long enough to trust the signals
