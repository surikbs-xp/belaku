---
title: Hosting
sidebar_position: 4
---

# Hosting

import Tabs from "@theme/Tabs";
import TabItem from "@theme/TabItem";

The bot runs fine on a laptop, but a laptop is the wrong *host*. This page covers
choosing an always-on machine, deploying to it, reaching its dashboard, and
moving your existing trade history across.

## Why the laptop has to go

GIFT Nifty trades roughly 20 hours a day and the strategy needs **14 closed
15-minute bars** before it will act. That is about 3.5 hours of feed time, so the
warm-up has to be complete well before the NSE open at 09:15 IST — and a signal
that arrives at 09:20 is worth nothing if the machine was asleep at 03:00.

| Laptop behaviour | What it costs the bot |
| --- | --- |
| Sleep on lid close / idle | Feed disconnects; 15-minute bars are missing forever (they are not backfilled for the missed window) |
| Wi-Fi roaming, hotel networks | `KiteTicker` drops; warm-up restarts from wherever it left off |
| Battery runs out | Same, plus SQLite may keep a `-wal` sidecar that a later `cp` would miss |
| FileVault pre-boot lock | Nothing starts until you unlock, so a reboot overnight means no morning session |
| macOS restarting for an update | Token refresh window is missed |

The requirement is dull: a machine that stays powered, stays on a wired or
reliable network, and reboots into the stack with no human present. That is a
small Linux VM, not a MacBook Air.

:::tip[What you actually need]

| Resource | Size |
| --- | --- |
| CPU | 1 vCPU (the workload is one Python process with four threads) |
| RAM | 1 GB (pandas dominates; 512 MB is tight) |
| Disk | 10 GB (SQLite plus logs; the DB grows slowly) |
| Network | **outbound** HTTPS only — no inbound port required |
| OS | Any Linux with Docker: Ubuntu 24.04 LTS is the well-trodden path |

:::

## Choosing a host

| Option | Monthly | Verdict |
| --- | --- | --- |
| Managed VM — Hetzner CX22, DigitalOcean, Vultr | ~$4–6 | **Recommended for most.** Predictable, a real IP, no sleep states |
| AWS Lightsail (Mumbai) | ~$7–12 | Fine, and the cheapest way into the same AWS region as Kite's ticker. Verify IPv4 billing; use ≥1 GB. The *container service* variant has no persistent disk — see [Lightsail containers](./lightsail.md) |
| Oracle Cloud Always Free (ARM) | $0 | Fine, but capacity is often unavailable and idle-reclaim policies are a nuisance |
| Raspberry Pi at home | ~$0 + power | Works; needs a UPS and a static-ish LAN, and a home power cut is now a trading outage |
| A second desktop | $0 | Works if you stop it sleeping. Same discipline as the laptop, minus the lid |
| Fly.io / Railway / Render | ~$5 | **Avoid.** Ephemeral filesystems fight SQLite, the 127.0.0.1-only dashboard needs a proxy with a `Host`-allow-list entry, and the Kite redirect URL gets awkward |

The bot is a single instance with one SQLite writer. There is nothing to scale
and no reason to pay for more than the smallest VM.

### Region: where Kite actually lives

`ws.kite.trade` resolves to a load balancer in **AWS `ap-south-1` (Mumbai)**
(`kite-prod-ticker-connect-….ap-south-1.elb.amazonaws.com`), and `api.kite.trade`
is fronted by Cloudflare. A host in Mumbai or Singapore therefore sits a few
milliseconds from the feed, where a US or EU host is 150–250 ms away.

This barely affects trading: signals are evaluated once per **closed 15-minute
bar**, so single-digit milliseconds cannot show up in the results. It does matter
for *reliability* — a long-haul path to Cloudflare and the ticker is one more
thing that can wobble during a session. Prefer a region in India or Singapore if
the price is comparable; otherwise take the cheaper, closer-to-you option.

Two things to check before committing to a host:

- **Public IPv4 billing.** AWS has billed public IPv4 separately since 2024
  (~$3.60/mo). On a $5 plan that is a large relative increase. Confirm what the
  current bundle includes before you sign up.
- **512 MB is too small.** The running container idles around **100 MB**, but
  startup pulls a pandas history frame and peaks well above that. 1 GB is the
  floor in practice.

A 1 GB Mumbai instance is convenient but priced above the equivalent European or
US VM — and none of a managed provider's extras (static IP, DNS, snapshots,
console) are used here. The dashboard must stay unreachable, the database is an
unmanaged SQLite file in a Docker volume, and no host solves the daily Kite
login. Pay for uptime and location, nothing else.

## 1. Provision and harden the VM

Create an Ubuntu 24.04 LTS instance with your SSH public key. On Lightsail this
is scripted — see [Provisioning on Lightsail](#provisioning-on-lightsail) below.
Then:

```bash
# Only SSH is reachable; the bot needs no inbound ports at all.
sudo ufw allow OpenSSH
sudo ufw --force enable
sudo apt-get update && sudo apt-get install -y unattended-upgrades

# Set the clock to IST: logs, the token-refresh hour and the market session all
# assume it, and a UTC host makes every log line and the "token expired" note
# look wrong by 5:30.
sudo timedatectl set-timezone Asia/Kolkata
timedatectl                      # expect: Time zone: Asia/Kolkata (IST, +0530)
```

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
newgrp docker                    # or log out and back in
docker run --rm hello-world      # sanity check
```

## 3. Clone and configure

```bash
sudo mkdir -p /opt/belaku && sudo chown "$USER" /opt/belaku
git clone https://github.com/surikbs-xp/belaku /opt/belaku
cd /opt/belaku
cp bot/.env.example bot/.env
```

Edit `bot/.env`:

```bash
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_REDIRECT_URL=http://127.0.0.1:9000/zerodha/callback
DASHBOARD_PASSWORD=<a long random string>
ALERT_ENABLED=0                  # 0 = silent; 1 only while testing the channel
ALERT_CHANNEL=telegram           # where a message would go once enabled
ALERT_TELEGRAM_BOT_TOKEN=
ALERT_TELEGRAM_CHAT_ID=
```

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # dashboard password
```

The container runs as uid 10001 and **writes a refreshed Kite token back into
`.env`**, so the file must be both readable and writable by that uid:

```bash
sudo chown 10001:10001 bot/.env
sudo chmod 600 bot/.env
```

:::warning[Where settings actually come from]

`compose.yaml` pins `WEBHOOK_HOST`, `WEBHOOK_PORT`, `DB_PATH`, `LOG_FILE` and
`HOLIDAY_CACHE_PATH` in its `environment:` block, and **those win over `.env`**.
Everything else — credentials, dashboard password, risk limits, strategy, alert
webhook — is read from `bot/.env` at startup, so editing it and restarting is
enough. To change a pinned value, edit `compose.yaml` and restart.

:::

## 4. Start it

```bash
docker compose up -d --build
docker compose ps                # expect: Up (healthy)
docker compose logs -f bot
```

Because `restart: unless-stopped` is set, the container comes back after a
reboot. Installing the systemd unit as well covers the case where the Docker
daemon loses the whole stack:

```bash
sudo cp deploy/belaku.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now belaku
systemctl status belaku
```

Do not stop here if you plan to trade real money — work through
[Production deployment](./production.md) first, in particular the daily-token
routine and the risk limits.

## Reaching the dashboard without exposing it

The stack binds `127.0.0.1:9000` **inside the VM**, and the API has no
authentication beyond the origin and `Host` allow-lists. Do not open the port.
Three workable approaches, in increasing order of convenience:

<Tabs>
<TabItem value="tunnel" label="SSH tunnel (recommended)">

```bash
ssh -N -L 9000:127.0.0.1:9000 belaku@your-host
```

Then browse [http://127.0.0.1:9000](http://127.0.0.1:9000) on your laptop. The
bot sees a loopback request with the `Host` header it already allows, so nothing
in `bot/.env` needs changing — and `http://127.0.0.1:9000/zerodha/callback`
stays a valid Kite Redirect URL, which is what makes the daily login work.

</TabItem>
<TabItem value="tailscale" label="Tailscale / WireGuard">

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale status                # note your MagicDNS name, e.g. belaku.tail-xxxx.ts.net
```

Private networking reaches the dashboard from anywhere with no public port. Two
caveats specific to this bot:

- Add the name to `ALLOWED_HOSTS` in `bot/.env`, or the `Host` allow-list rejects
  the request (that allow-list is what blocks DNS rebinding, so it is doing its
  job): `ALLOWED_HOSTS=127.0.0.1,localhost,[::1],belaku.tail-xxxx.ts.net`.
- The Kite console needs a Redirect URL it will actually send the browser to.
  **Register a second Redirect URL**,
  `http://belaku.tail-xxxx.ts.net:9000/zerodha/callback`, rather than replacing
  the loopback one. Whether a broker's redirect follows a tailnet name depends on
  your setup, and the loopback URL under the SSH tunnel is the guaranteed path.

</TabItem>
<TabItem value="tls" label="Reverse proxy + TLS">

Put Caddy or nginx in front on a real domain and the dashboard becomes a normal
HTTPS site. This is the most work and the most convenient, and it is the only
option where you should also set `secure=True` on the session cookie.

```caddyfile
belaku.example.com {
    reverse_proxy 127.0.0.1:9000
}
```

- The proxy must pass the original `Host` through (Caddy does by default) and the
  hostname must be in `ALLOWED_HOSTS`.
- Add `https://belaku.example.com/zerodha/callback` as a Redirect URL in the Kite
  console; the bot already exempts `/zerodha/callback` from session auth so the
  broker's redirect works before you have logged in.
- Keep the `ports:` binding on `127.0.0.1` in `compose.yaml`. The proxy runs on
  the host and reaches the container through it.

</TabItem>
</Tabs>

## The daily token, unattended

There is no way around this: **Kite has no headless login.** The flow is a
browser redirect, sign-in and 2FA, and the `access_token` it produces dies around
06:00 IST the next day. See [Production deployment](./production.md#the-daily-token-problem).

On a headless VM the routine is:

1. Open the SSH tunnel (or Tailscale / the proxy).
2. Click **Connect** on the dashboard, sign in, and let Zerodha redirect to
   `/zerodha/callback`. The bot exchanges the `request_token` and writes the new
   token into `.env`. No copy-paste, and no browser on the server.
3. No tunnel handy? Mint the token locally and POST it — the endpoint is the same
   one the dashboard uses:

   ```bash
   curl -X POST http://127.0.0.1:9000/api/kite/session \
     -H 'Content-Type: application/json' \
     -d '{"request_token":"..."}'
   ```

   (Over SSH: `ssh -L 9000:127.0.0.1:9000 belaku@your-host` first.)

If that morning step is impossible for you, the bot cannot run truly unattended —
it will keep running on cached candles with a stale token, and the watchdog will
alert `FEED_DOWN` during market hours, but it will not trade. Automating the
login with a stored TOTP secret is possible and is a credential-handling decision
to make on purpose, ideally against a Kite account that is not your primary one.

## Backups off the box

`scripts/backup-db.sh` reads the `belaku_bot-data` volume and writes a verified
snapshot, but by default it writes into `./backups` — the same VM. A snapshot that
dies with the host is not a backup.

```bash
cd /opt/belaku
./scripts/backup-db.sh                                  # writes ./backups
BACKUP_DIR=/mnt/backups ./scripts/backup-db.sh          # or anywhere else
```

Schedule it after the close, and copy the directory off-box:

```cron
30 16 * * 1-5 cd /opt/belaku && ./scripts/backup-db.sh >> /opt/belaku/backups/backup.log 2>&1
45 16 * * 1-5 rclone sync /opt/belaku/backups remote:belaku-backups
```

The script needs `docker` and a host `python3` (any 3.x; Ubuntu ships one) — it
uses a throwaway container for the snapshot and the host Python to run
`PRAGMA integrity_check`. See
[Database operations](./database/operations.md) for restore and pruning.

## Moving your existing history

The trading record lives in the `belaku_bot-data` volume. Snapshot it on the Mac,
copy the file, and restore it on the VM. **Stop the old instance first** — SQLite
tolerates one writer, and `.backup` is only consistent against a stopped or
concurrently-running bot, not against two bots fighting for the same file.

```bash
# 1. On the Mac: snapshot, then stop the stack.
cd /Users/surikbs/Documents/GitHub/surikbs-xp/belaku
./scripts/backup-db.sh
docker compose down

# 2. Copy the newest snapshot to the VM.
scp "$(ls -t backups/belaku-*.db | head -1)" belaku@your-host:/tmp/
```

```bash
# 3. On the VM: create the volume, then restore into it.
cd /opt/belaku
docker compose up -d --build          # creates the volume with uid 10001 ownership
docker compose down

docker run --rm \
  -v belaku_bot-data:/data \
  -v /tmp:/restore:ro \
  alpine sh -c '
    rm -f /data/paper_trading.db /data/paper_trading.db-wal /data/paper_trading.db-shm
    cp /restore/belaku-*.db /data/paper_trading.db
    chown 10001:10001 /data/paper_trading.db'

docker compose up -d
curl -s localhost:9000/api/portfolio | python3 -m json.tool | head -30
```

The stale `-wal` / `-shm` removal matters: a sidecar left from an old database
belongs to a different file and SQLite will refuse to open the pair.

:::tip[Cleanest option: no token on the Mac at all]

Delete `KITE_ACCESS_TOKEN` from the Mac's `bot/.env` (or stop using that checkout)
so the laptop cannot mint orders. One host, one credential — otherwise a
forgotten local `docker compose up` can trade the same account as the VM.

:::

## Operating it

```bash
cd /opt/belaku

docker compose ps                 # is it up, and healthy?
docker compose logs -f bot        # follow
docker compose restart bot        # after editing bot/.env
git pull && docker compose up -d --build    # upgrade
```

| Alert | What it means |
| --- | --- |
| `STALE_FEED` | Connected but no tick for 3 minutes during market hours |
| `FEED_DOWN` | Not connected during market hours — usually a stale token |
| `STOP_OUT` | A position was closed by the signal flipping (the strategy keeps running) |
| `PAUSED` | Strategy paused and nobody resumed it |
| `ENGINE_ERROR` | The engine recorded an error |

`ALERT_ENABLED` is the master switch, and it is off by default: a filled-in
channel and token send nothing until it is set to `1`. Turn it on to confirm
delivery, then off again — every fill and every flip is an alert, and a phone is
a poor place to read a session. `ALERT_CHANNEL` selects the destination and the
payload is built to match it — `telegram`, `slack`, `discord`, `ntfy` or
`generic`. This matters: each service
rejects a payload it does not recognise, so a mismatched channel fails silently
at the service. Send a test from the dashboard (**Send test alert**) or
`POST /api/alerts/test` to confirm delivery before you rely on it.

Watchdog state is on `/api/status` under `watchdog`; channel config is on
`/api/alerts/status`.

## Provisioning on Lightsail

`deploy/lightsail/provision-instance.py` creates the instance, imports your SSH
key, attaches a static IP and closes the firewall down to SSH. It is idempotent,
so re-running after a partial failure resumes instead of duplicating.

```bash
# Your public key, e.g. ~/.ssh/belaku.pub
python3 deploy/lightsail/provision-instance.py \
  --public-key ~/.ssh/belaku.pub \
  --region ap-south-1 \
  --zone ap-south-1a
```

It prints the static IP and the `ssh ubuntu@…` line. Then continue from
[Install Docker](#2-install-docker).

The static IP matters more than it looks: without one the public address changes
on every stop/start, which breaks both the Kite redirect URL and the dashboard
bookmark each time the box is cycled for cost.

:::warning[The SCP may not allow the region you want]

Lightsail instance actions are governed by the organization's service control
policy, which can deny them per region. If `create-instances` fails with
`explicit deny in a service control policy`, the region is blocked regardless of
what the IAM identity policy allows — the SCP is evaluated first and wins. Ask
the organization admin to allow `lightsail:*Instance*` (and `lightsail:*KeyPair*`,
`lightsail:*StaticIp*`) in that region, or pick a region the policy already
permits.

:::

The script needs these actions on the identity that runs it:

```json title="belaku-provision-permissions.json"
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "lightsail:GetInstances",
        "lightsail:CreateInstances",
        "lightsail:GetKeyPairs",
        "lightsail:ImportKeyPair",
        "lightsail:GetStaticIps",
        "lightsail:AllocateStaticIp",
        "lightsail:AttachStaticIp",
        "lightsail:PutInstancePublicPorts"
      ],
      "Resource": "*"
    }
  ]
}
```

Add `lightsail:ReleaseStaticIp` and `lightsail:DeleteInstance` too, or you will
not be able to undo a mistake: an **unattached static IP is billed**, and a
policy that can allocate but not release leaves you paying for it.

## Checklist

- [ ] VM is always-on, with an SSH key and no password login
- [ ] `ufw` allows SSH only; port 9000 is **not** exposed publicly
- [ ] Timezone is `Asia/Kolkata`
- [ ] `bot/.env` owned by uid 10001, mode 600, with a long `DASHBOARD_PASSWORD`
- [ ] `docker compose ps` shows `Up (healthy)` after a reboot
- [ ] `deploy/belaku.service` installed and enabled
- [ ] Reachability solved (SSH tunnel, Tailscale, or TLS proxy) and a matching
      Kite Redirect URL registered
- [ ] Daily login routine established and tested once
- [ ] `scripts/backup-db.sh` scheduled **and copying off-box**
- [ ] Old instance stopped and its `KITE_ACCESS_TOKEN` removed

## Next

- [Production deployment](./production.md) — the full unattended runbook
- [Running](./running.md) — Docker layout, volumes, health
- [Lightsail containers](./lightsail.md) — the CI pipeline, and why it has no persistent disk
- [Configuration](./configuration.md) — every environment variable
