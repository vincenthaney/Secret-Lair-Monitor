# Secret Lair Drop Monitor

Monitors the [Secret Lair](https://secretlair.wizards.com/us/) store and [Chaos Vault](https://secretlair.wizards.com/us/chaosvault) for new product drops. Sends notifications to a Discord channel via webhook when new items appear.

## What it monitors

- **Main store** (`/us/`) — Featured drops and homepage products
- **Chaos Vault** (`/us/chaosvault`) — Detects when the vault opens/closes and alerts immediately
- **Shop All** (`/us/shopall`) — Catches products that may not be on the homepage (optional, enabled by default)

## Features

- Detects new product additions by tracking product IDs
- Special "CHAOS VAULT IS OPEN" alert when the vault transitions from closed to active
- Detects pre-release site downtime and fires a single "Releasing Soon" alert (instead of spamming for every failed poll), then polls faster so the new drop is announced quickly when the site returns
- Rich Discord embeds with product name, price, and direct link
- Persists state across restarts (won't re-notify on reboot)
- First run silently catalogues existing products (no spam on initial deploy)
- Rate-limit aware for Discord API

## Setup

### 1. Create a Discord Webhook

In your Discord server: **Server Settings > Integrations > Webhooks > New Webhook**

Pick the channel you want alerts in, copy the webhook URL.

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and set your webhook URL:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/aBcDeFgHiJkLmNoPqRsT
```

### 3. Deploy

```bash
docker compose up -d
```

Check logs:
```bash
docker compose logs -f
```

## Post a Current-State Snapshot

To refresh the monitored pages and post their current products and Chaos Vault status to Discord, run the one-shot command. It updates `data/state.json` first and sends newly found products as part of the snapshot, without separate new-product alerts. If a page is unavailable, the message marks the snapshot as partial.

With Docker Compose, make sure `.env` contains `DISCORD_WEBHOOK_URL`, then run:

```bash
docker compose run --build --rm secretlair python monitor.py --post-current-state
```

`--build` builds the current repository code first. This uses the service's existing `/data` volume, so it shares state with the running monitor. The one-shot container exits after posting.

To run locally, install the packages from `requirements.txt`, set `DISCORD_WEBHOOK_URL`, and point `STATE_FILE` at the repo's data file. In PowerShell:

```powershell
$env:DISCORD_WEBHOOK_URL = "<your Discord webhook URL>"
$env:STATE_FILE = "./data/state.json"
python monitor.py --post-current-state
```

In Bash:

```bash
export DISCORD_WEBHOOK_URL="<your Discord webhook URL>"
STATE_FILE=./data/state.json python monitor.py --post-current-state
```

## Configuration

All config is via environment variables (set in `.env`):

| Variable | Default | Description |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | *(required)* | Discord webhook URL |
| `POLL_INTERVAL_SECONDS` | `180` | Seconds between checks when the site is up |
| `MAINTENANCE_POLL_INTERVAL_SECONDS` | `60` | Poll interval used while the site is down, *outside* the top-of-hour window |
| `MAINTENANCE_BURST_INTERVAL_SECONDS` | `15` | Fast poll interval while in maintenance mode and near :00 (drops historically go live on the hour) |
| `MAINTENANCE_BURST_JITTER_SECONDS` | `2` | ±jitter added to the burst interval to avoid looking like a cron job |
| `MAINTENANCE_BURST_WINDOW_BEFORE` | `2` | Minutes before :00 to start burst-polling |
| `MAINTENANCE_BURST_WINDOW_AFTER` | `2` | Minutes after :00 to keep burst-polling |
| `MAINTENANCE_REQUEST_TIMEOUT_SECONDS` | `10` | HTTP timeout while in maintenance mode (so a hung request can't eat a burst interval) |
| `MAINTENANCE_ALERT_AFTER_MINUTES` | `15` | Site must be continuously down this long before the "Releasing Soon" alert fires (filters routine maintenance windows) |
| `MONITOR_SHOP_ALL` | `true` | Also monitor `/us/shopall` |
| `NOTIFY_ON_START` | `false` | Notify for existing products on first run |
| `LOG_LEVEL` | `INFO` | Log verbosity |

## How It Works

1. On startup, fetches all monitored pages and parses product links from the HTML
2. Product IDs (the numeric ID in `/us/product/1249999`) are the unique key
3. First run stores all current products silently (no notification flood)
4. Subsequent checks compare against known state — new IDs trigger Discord alerts
5. The Chaos Vault page is checked for whether it has any product listings. When products appear, a special high-visibility alert fires.
6. State is written to `state.json` after every cycle

## Troubleshooting

**No notifications on first run** — This is by design. Set `NOTIFY_ON_START=true` if you want alerts for existing products.

**Products not detected** — The scraper looks for `/us/product/{id}` links in the HTML. If Wizards changes their URL structure or moves to fully client-rendered content, the regex patterns may need updating. Check logs at `LOG_LEVEL=DEBUG`.

**Rate limited** — The monitor handles Discord 429 responses automatically. If the site itself rate-limits you, increase `POLL_INTERVAL_SECONDS`.

**State reset** — Delete the `data/` directory to start fresh. The next run will re-catalogue everything.
