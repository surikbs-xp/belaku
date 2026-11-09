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
STOP_LOSS_POINTS=100        # choose deliberately; 0 disables
ALERT_WEBHOOK_URL=          # optional, but strongly recommended
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
| `RISK_BREACH` | A stop-loss or the daily loss cap fired |
| `PAUSED` | Strategy paused and nobody resumed it |
| `ENGINE_ERROR` | The engine recorded an error |

Set `ALERT_WEBHOOK_URL` to a Slack, Discord or ntfy webhook so these reach your
phone. Alerts are rate-limited to one per kind per 15 minutes, and only fire
after three consecutive failing checks, so a single slow poll does not page you.

Watchdog state is on `/api/status` under `watchdog`.

## Risk limits

The strategy has **no stop-loss of its own**. It exits on an opposite signal or a
manual square-off, so between those two events a position is unbounded.

| Setting | Effect |
| --- | --- |
| `STOP_LOSS_POINTS` | Close a position once it is this many index points against entry |
| `MAX_OPEN_LOTS` | Reject any order that would exceed this many lots |

The stop-loss latches: once tripped, the bot squares off the offending position,
pauses, and stays paused until you acknowledge the breach on the dashboard. This
is deliberate — a limit that clears itself when price recovers turns one bad day
into a run of them.

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
- [ ] `STOP_LOSS_POINTS` chosen deliberately
- [ ] `ALERT_WEBHOOK_URL` set and tested
- [ ] `scripts/backup-db.sh` scheduled and copied off-box
- [ ] systemd unit installed and enabled
- [ ] Log rotation confirmed (`/data/bot.log`, 5 MB × 5)
- [ ] Daily login routine established
- [ ] Ran in PAPER mode long enough to trust the signals
