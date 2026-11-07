---
title: Setup
---

# Setup

import Tabs from "@theme/Tabs";
import TabItem from "@theme/TabItem";

## Requirements

- Python **3.12+**
- Zerodha Kite Connect API credentials
- A valid Kite access token

## Install

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
test -f bot/.env || cp bot/.env.example bot/.env
```

Then edit `bot/.env`.

## Credentials

Kite credentials are **required, not optional**. The GIFT Nifty feed runs on
`KiteTicker`, so without a token there is no market data and no strategy.

1. Create an app at [https://developers.kite.trade](https://developers.kite.trade)
2. Set `KITE_API_KEY` and `KITE_API_SECRET` in `bot/.env`
3. Register the Redirect URL as `http://127.0.0.1:9000/zerodha/callback`

:::warning[`bot/.env` is tracked in git]
This file is currently committed, which means any credentials in it are in
the repository history. Move it out of version control and rotate the keys
before treating this as production. `.gitignore` already excludes `*.env*s`
— the file was added before that rule.
:::


:::note[PAPER mode still needs a token]
`EXECUTION_MODE=PAPER` mocks *orders*, not market data. The strategy cannot
evaluate anything without a live feed.
:::


## Getting the access token

Kite access tokens expire daily. There are three ways to get one:

<Tabs>
<TabItem value="auto" label="Automatic (default)">

At startup, a valid saved token starts the feed automatically. If the token is
missing or expired and credentials are configured, the bot opens Zerodha login
in your local browser.

Sign-in and 2FA are still required — the bot cannot do those for you.

</TabItem>
<TabItem value="dashboard" label="Dashboard">

Use the **Connect** button in the header. It opens the login URL and captures the
`request_token` on the callback.

</TabItem>
<TabItem value="manual" label="Manual">

For headless deployments, paste a `request_token` into the dashboard, or:

```bash
curl -X POST localhost:9000/api/kite/session \
  -H 'Content-Type: application/json' \
  -d '{"request_token":"..."}'
```

</TabItem>
</Tabs>

The token is persisted to `bot/.env` and survives a restart.

## Verify

```bash
curl -s localhost:9000/api/status | python -m json.tool
```

Look for:

- `kite.authenticated: true`
- `engine.feed.connected: true`
- `engine.warmup_have` climbing toward `warmup_needed` (14)

## Next

- [Running](/running) — local and Docker
- [Configuration](/configuration) — every environment variable
