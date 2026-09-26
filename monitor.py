#!/usr/bin/env python3
"""
Secret Lair Drop Monitor
Polls the Secret Lair store and Chaos Vault pages for new products,
then sends Discord webhook notifications when new items appear.
"""

import argparse
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "180"))  # 3 min default
# While the site is down (likely a pre-release maintenance window), poll faster
# so we catch the new drop quickly once it's back.
MAINTENANCE_POLL_INTERVAL_SECONDS = int(
    os.environ.get("MAINTENANCE_POLL_INTERVAL_SECONDS", "60")
)
# Historical Secret Lair drops have gone live on the hour, so while in
# maintenance mode we ramp up to a near-realtime poll for a few minutes on
# either side of :00 and back off to the slower maintenance interval otherwise.
MAINTENANCE_BURST_INTERVAL_SECONDS = int(
    os.environ.get("MAINTENANCE_BURST_INTERVAL_SECONDS", "15")
)
MAINTENANCE_BURST_JITTER_SECONDS = int(
    os.environ.get("MAINTENANCE_BURST_JITTER_SECONDS", "2")
)
# How many minutes before / after :00 to burst-poll.
MAINTENANCE_BURST_WINDOW_BEFORE = int(
    os.environ.get("MAINTENANCE_BURST_WINDOW_BEFORE", "2")
)
MAINTENANCE_BURST_WINDOW_AFTER = int(
    os.environ.get("MAINTENANCE_BURST_WINDOW_AFTER", "2")
)
# Shorter request timeout while the site is down so a hung request can't eat
# the whole poll interval.
MAINTENANCE_REQUEST_TIMEOUT_SECONDS = int(
    os.environ.get("MAINTENANCE_REQUEST_TIMEOUT_SECONDS", "10")
)
# How long the store must be continuously down before we treat it as a
# pre-release maintenance window and fire the "Releasing Soon" alert. Brief
# routine maintenance that recovers before this elapses sends nothing.
MAINTENANCE_ALERT_AFTER_MINUTES = float(
    os.environ.get("MAINTENANCE_ALERT_AFTER_MINUTES", "15")
)
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

# Pages to monitor
PAGES = {
    "store": "https://secretlair.wizards.com/us/",
    "chaos_vault": "https://secretlair.wizards.com/us/chaosvault",
}

# Optional: also monitor the "shop all" page which may list products not on the homepage
MONITOR_SHOP_ALL = os.environ.get("MONITOR_SHOP_ALL", "true").lower() == "true"
if MONITOR_SHOP_ALL:
    PAGES["shop_all"] = "https://secretlair.wizards.com/us/shopall"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("secret-lair-monitor")

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load known product IDs from the state file."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning("Failed to read state file, starting fresh: %s", e)
    return {"known_products": {}, "last_check": None}


def save_state(state: dict) -> None:
    """Persist state to disk."""
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

# Matches product links like /us/product/1249999 or /us/product/1249999/slug-name
PRODUCT_LINK_RE = re.compile(r"/us/product/(\d+)(?:/([^\"'\s]*))?")


def fetch_page(url: str, timeout: int = 30) -> str | None:
    """Fetch a page's HTML. Returns None on failure.

    Sends cache-busting headers so we don't get a stale 'we'll be right back'
    page from a CDN edge after Wizards brings the site back online.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    sep = "&" if "?" in url else "?"
    bust_url = f"{url}{sep}_={int(time.time())}"
    try:
        resp = requests.get(bust_url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


# Phrases that indicate Wizards has the site behind a maintenance/holding page
# (HTTP 200, but the page itself is the maintenance page). Matched against
# <title> and top-level <h1> only — substring matching against the whole body
# false-positived for ~20 days on a promo banner that contained one of these
# phrases, silently suppressing all product detection.
_MAINTENANCE_INDICATORS = (
    "we'll be right back",
    "we will be right back",
    "be right back",
    "under maintenance",
    "site maintenance",
    "scheduled maintenance",
    "temporarily unavailable",
    "service unavailable",
    "503",
)


def is_maintenance_page(html: str | None) -> bool:
    """Detect a 'site down' holding page that loaded as 200 OK.

    Only considers the <title> and top-level <h1> text — real maintenance pages
    set these explicitly, and ignoring body text avoids false-positives on
    promo banners that happen to contain phrases like "be right back".
    """
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    if soup.title and soup.title.string:
        candidates.append(soup.title.string)
    for h1 in soup.find_all("h1"):
        text = h1.get_text(strip=True)
        if text:
            candidates.append(text)
    haystack = " ".join(candidates).lower()
    if not haystack:
        return False
    return any(indicator in haystack for indicator in _MAINTENANCE_INDICATORS)


def parse_products(html: str) -> dict[str, dict]:
    """
    Extract products from HTML.
    Returns dict keyed by product ID with metadata.
    """
    products: dict[str, dict] = {}
    soup = BeautifulSoup(html, "html.parser")

    # Find all anchor tags linking to product pages
    for anchor in soup.find_all("a", href=PRODUCT_LINK_RE):
        href = anchor.get("href", "")
        match = PRODUCT_LINK_RE.search(href)
        if not match:
            continue

        product_id = match.group(1)
        slug = match.group(2) or ""

        # Try to get the product name from the anchor's title attr or text content
        name = anchor.get("title", "").strip()
        if not name:
            # Look for heading tags inside the anchor's parent card
            parent = anchor.find_parent(class_=re.compile(r"product|card|item", re.I))
            if parent:
                heading = parent.find(re.compile(r"h[1-6]"))
                if heading:
                    name = heading.get_text(strip=True)
            if not name:
                name = anchor.get_text(strip=True)
        if not name:
            # Fall back to the slug
            name = slug.replace("-", " ").title() if slug else f"Product {product_id}"

        # Try to find price
        price = ""
        parent_card = anchor.find_parent(class_=re.compile(r"product|card|item", re.I))
        if parent_card:
            price_el = parent_card.find(string=re.compile(r"\$\d+"))
            if price_el:
                price_match = re.search(r"\$[\d,.]+", str(price_el))
                if price_match:
                    price = price_match.group(0)

        url = f"https://secretlair.wizards.com/us/product/{product_id}"
        if slug:
            url += f"/{slug}"

        products[product_id] = {
            "id": product_id,
            "name": name,
            "price": price,
            "url": url,
        }

    return products


def check_chaos_vault_active(html: str) -> bool:
    """
    Determine if the Chaos Vault currently has products listed
    (vs. just the 'GET NOTIFIED' placeholder page).
    """
    soup = BeautifulSoup(html, "html.parser")
    # If there are product links, the vault is active
    product_links = soup.find_all("a", href=PRODUCT_LINK_RE)
    return len(product_links) > 0


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------

def send_discord_notification(products: list[dict], source: str) -> bool:
    """Send a Discord embed for new products."""
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping notification")
        return False

    # Color map for the embed sidebar
    colors = {
        "store": 0x7B2D8B,       # Purple for main store
        "chaos_vault": 0xE74C3C,  # Red for Chaos Vault
        "shop_all": 0x3498DB,     # Blue for shop all
    }

    source_labels = {
        "store": "Secret Lair Store",
        "chaos_vault": "Chaos Vault",
        "shop_all": "Shop All",
    }

    embeds = []
    for product in products:
        embed = {
            "title": product["name"],
            "url": product["url"],
            "color": colors.get(source, 0x95A5A6),
            "fields": [],
            "footer": {
                "text": f"Source: {source_labels.get(source, source)} • ID: {product['id']}",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if product.get("price"):
            embed["fields"].append({"name": "Price", "value": product["price"], "inline": True})
        embed["fields"].append({"name": "Link", "value": f"[View Product]({product['url']})", "inline": True})
        embeds.append(embed)

    # Discord allows max 10 embeds per message. Batch if needed.
    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        payload = {
            "username": "Secret Lair Monitor",
            "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                          "wizardsofthecoast-secret-lair/favicon-32.png",
            "content": f"**🃏 New Secret Lair Drop{'s' if len(batch) > 1 else ''} Detected!**"
            if i == 0
            else None,
            "embeds": batch,
        }
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 5)
                log.warning("Rate limited by Discord, waiting %.1fs", retry_after)
                time.sleep(retry_after)
                resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            resp.raise_for_status()
            log.info("Discord notification sent (%d embeds)", len(batch))
        except requests.RequestException as e:
            log.error("Failed to send Discord notification: %s", e)
            return False

    return True


def send_current_state_notification(
    products: list[dict],
    available_pages: list[str],
    unavailable_pages: list[str],
    chaos_vault_active: bool | None,
) -> bool:
    """Post a snapshot of currently visible products and page availability."""
    if not DISCORD_WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL is not set; cannot post current state")
        return False

    source_labels = {
        "store": "Secret Lair Store",
        "chaos_vault": "Chaos Vault",
        "shop_all": "Shop All",
    }
    if chaos_vault_active is None:
        vault_status = "Unknown (page unavailable)"
    else:
        vault_status = "Open" if chaos_vault_active else "Closed"

    status_lines = [f"**Chaos Vault:** {vault_status}"]
    if available_pages:
        status_lines.append(
            "**Updated pages:** "
            + ", ".join(source_labels.get(page, page) for page in available_pages)
        )
    if unavailable_pages:
        status_lines.append(
            "**Unavailable pages (snapshot is partial):** "
            + ", ".join(source_labels.get(page, page) for page in unavailable_pages)
        )
    if not products and available_pages:
        status_lines.append("No products were found on the available pages.")
    elif not available_pages:
        status_lines.append("No pages could be fetched; current products cannot be confirmed.")

    description = "\n".join(status_lines)
    if products:
        header = f"\n\n**Current products ({len(products)}):**\n"
        description += header
        for index, product in enumerate(products):
            source = source_labels.get(product.get("source"), product.get("source", "Unknown"))
            line = f"• [{product['name']}]({product['url']}) — {source}"
            if product.get("price"):
                line += f" — {product['price']}"
            line += "\n"
            remaining = len(products) - index - 1
            omitted_note = f"… and {remaining} more products omitted.\n" if remaining else ""
            if len(description) + len(line) + len(omitted_note) > 4096:
                description += f"… and {len(products) - index} more products omitted."
                break
            description += line

    embed = {
        "title": "Current Secret Lair State",
        "description": description,
        "color": 0x7B2D8B,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    payload = {
        "username": "Secret Lair Monitor",
        "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                      "wizardsofthecoast-secret-lair/favicon-32.png",
        "content": "**Current Secret Lair state**",
        "embeds": [embed],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code == 429:
            retry_after = resp.json().get("retry_after", 5)
            log.warning("Rate limited by Discord, waiting %.1fs", retry_after)
            time.sleep(retry_after)
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error("Failed to post current state to Discord: %s", e)
        return False

    log.info("Current state posted (%d products)", len(products))
    return True


def post_current_state(state: dict) -> bool:
    """Fetch current pages, persist discovered products, and post one snapshot."""
    known = state.get("known_products", {})
    products_by_id: dict[str, dict] = {}
    available_pages: list[str] = []
    unavailable_pages: list[str] = []
    chaos_vault_active: bool | None = None

    for source, url in PAGES.items():
        log.info("Refreshing %s: %s", source, url)
        html = fetch_page(url, timeout=30)
        if html is None or is_maintenance_page(html):
            unavailable_pages.append(source)
            continue

        available_pages.append(source)
        if source == "chaos_vault":
            chaos_vault_active = check_chaos_vault_active(html)

        for product_id, product in parse_products(html).items():
            product_with_source = {**product, "source": source}
            products_by_id[product_id] = product_with_source
            previous = known.get(product_id, {})
            known[product_id] = {
                **previous,
                **product,
                "first_seen": previous.get("first_seen", datetime.now(timezone.utc).isoformat()),
                "source": previous.get("source", source),
            }

    state["known_products"] = known
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    if chaos_vault_active is not None:
        state["chaos_vault_active"] = chaos_vault_active
    save_state(state)

    return send_current_state_notification(
        list(products_by_id.values()),
        available_pages,
        unavailable_pages,
        chaos_vault_active,
    )


def send_release_soon_notification() -> bool:
    """One-shot notification fired when the site goes into maintenance mode.

    Replaces the per-fetch error spam that used to fire during the multi-hour
    pre-release window.
    """
    if not DISCORD_WEBHOOK_URL:
        return False

    payload = {
        "username": "Secret Lair Monitor",
        "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                      "wizardsofthecoast-secret-lair/favicon-32.png",
        "content": "**🃏 Secret Lair Releasing Soon!**",
        "embeds": [
            {
                "title": "Secret Lair site is down",
                "url": "https://secretlair.wizards.com/us/",
                "color": 0xF1C40F,  # Yellow
                "description": (
                    "The Secret Lair site is currently unavailable, which usually "
                    "means a new drop is about to go live. Monitoring more "
                    "frequently — you'll get a notification as soon as the new "
                    "products appear."
                ),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("'Releasing Soon' notification sent")
        return True
    except requests.RequestException as e:
        log.error("Failed to send 'Releasing Soon' notification: %s", e)
        return False


def send_chaos_vault_opened_notification() -> bool:
    """Special notification when the Chaos Vault transitions from closed to open."""
    if not DISCORD_WEBHOOK_URL:
        return False

    payload = {
        "username": "Secret Lair Monitor",
        "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                      "wizardsofthecoast-secret-lair/favicon-32.png",
        "content": "# 🚨 CHAOS VAULT IS NOW OPEN! 🚨",
        "embeds": [
            {
                "title": "The Secret Lair Chaos Vault is LIVE",
                "url": "https://secretlair.wizards.com/us/chaosvault",
                "color": 0xE74C3C,
                "description": "The Chaos Vault has opened! Go check it out before it's gone!",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Chaos Vault OPENED notification sent")
        return True
    except requests.RequestException as e:
        log.error("Failed to send Chaos Vault notification: %s", e)
        return False


# Error throttling: don't spam Discord if the same error keeps firing
_last_error_sent: dict[str, float] = {}
ERROR_NOTIFY_COOLDOWN = 900  # Only re-notify for the same error context every 15 minutes


def send_discord_error_notification(error_msg: str, context: str = "") -> bool:
    """Send an error notification to Discord so issues are visible even from phone."""
    if not DISCORD_WEBHOOK_URL:
        return False

    # Throttle: skip if we sent the same context recently
    now = time.time()
    throttle_key = context or error_msg[:100]
    if throttle_key in _last_error_sent:
        if now - _last_error_sent[throttle_key] < ERROR_NOTIFY_COOLDOWN:
            log.debug("Error notification throttled (cooldown): %s", throttle_key)
            return False

    description = f"```\n{error_msg[:1800]}\n```"
    if context:
        description = f"{context}\n{description}"

    payload = {
        "username": "Secret Lair Monitor",
        "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                      "wizardsofthecoast-secret-lair/favicon-32.png",
        "embeds": [
            {
                "title": "⚠️ Monitor Error",
                "color": 0xFFA500,  # Orange
                "description": description,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        _last_error_sent[throttle_key] = now
        return True
    except requests.RequestException:
        log.error("Failed to send error notification to Discord")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def in_top_of_hour_burst_window() -> bool:
    """True when the wall clock is in the configured top-of-hour burst window.

    Minute-of-hour is the same across all whole-hour timezones, so we don't
    need to know which timezone Wizards releases in.
    """
    minute = datetime.now().minute
    return (
        minute >= 60 - MAINTENANCE_BURST_WINDOW_BEFORE
        or minute <= MAINTENANCE_BURST_WINDOW_AFTER
    )


def compute_sleep_interval(in_maintenance: bool) -> float:
    """Pick the next sleep duration based on site state and clock position."""
    if not in_maintenance:
        return float(POLL_INTERVAL_SECONDS)
    if in_top_of_hour_burst_window():
        jitter = random.uniform(
            -MAINTENANCE_BURST_JITTER_SECONDS, MAINTENANCE_BURST_JITTER_SECONDS
        )
        return max(1.0, MAINTENANCE_BURST_INTERVAL_SECONDS + jitter)
    return float(MAINTENANCE_POLL_INTERVAL_SECONDS)


def run_check(state: dict) -> tuple[dict, bool]:
    """Run one check cycle across all monitored pages.

    Returns (state, in_maintenance). When the site is in maintenance mode we
    skip product parsing and let the caller use a faster poll interval.
    """
    known = state.get("known_products", {})
    chaos_vault_was_active = state.get("chaos_vault_active", False)
    was_in_maintenance = state.get("maintenance_mode", False)

    # While we were already in maintenance mode, use a tight request timeout so
    # one hung connection can't blow past the burst-poll interval.
    fetch_timeout = (
        MAINTENANCE_REQUEST_TIMEOUT_SECONDS if was_in_maintenance else 30
    )

    now = datetime.now(timezone.utc)

    # Fetch every page up front. Product detection always runs on every page
    # regardless of the store page's state — silently gating product checks on
    # the homepage being healthy hid two missed drops for ~20 days when the
    # store page false-positived as "in maintenance".
    page_html: dict[str, str | None] = {}
    for source, url in PAGES.items():
        log.debug("Checking %s: %s", source, url)
        page_html[source] = fetch_page(url, timeout=fetch_timeout)

    # ---- Maintenance tracking (purely informational; never blocks below) ----
    store_html = page_html.get("store")
    store_down = store_html is None or is_maintenance_page(store_html)

    if store_down:
        down_since_raw = state.get("down_since")
        if down_since_raw is None:
            down_since = now
            state["down_since"] = now.isoformat()
            log.info("Secret Lair store appears down — starting outage timer")
        else:
            down_since = datetime.fromisoformat(down_since_raw)

        down_minutes = (now - down_since).total_seconds() / 60.0
        if not was_in_maintenance and down_minutes >= MAINTENANCE_ALERT_AFTER_MINUTES:
            log.info(
                "Store down %.1f min (>= %.0f) — entering maintenance mode",
                down_minutes,
                MAINTENANCE_ALERT_AFTER_MINUTES,
            )
            send_release_soon_notification()
            state["maintenance_mode"] = True
        else:
            log.info(
                "Store down %.1f min (alert at %.0f min)",
                down_minutes,
                MAINTENANCE_ALERT_AFTER_MINUTES,
            )
    else:
        state["down_since"] = None
        if was_in_maintenance:
            log.info("Secret Lair site is back online")
            state["maintenance_mode"] = False

    # ---- Product detection (always runs, every page, every cycle) ----
    for source, _url in PAGES.items():
        html = page_html.get(source)
        if html is None:
            continue

        if source == "chaos_vault":
            is_active = check_chaos_vault_active(html)
            if is_active and not chaos_vault_was_active:
                log.info("Chaos Vault has OPENED!")
                send_chaos_vault_opened_notification()
            elif not is_active and chaos_vault_was_active:
                log.info("Chaos Vault has closed.")
            state["chaos_vault_active"] = is_active

        products = parse_products(html)
        new_products = []
        for pid, product in products.items():
            if pid not in known:
                log.info("New product found: [%s] %s (%s)", pid, product["name"], source)
                new_products.append(product)
                known[pid] = {
                    **product,
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                }
        if new_products:
            send_discord_notification(new_products, source)

    state["known_products"] = known
    state["last_check"] = now.isoformat()
    return state, state.get("maintenance_mode", False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor Secret Lair product drops")
    parser.add_argument(
        "--post-current-state",
        action="store_true",
        help="Fetch the monitored pages, save state, post one current-state snapshot, and exit",
    )
    args = parser.parse_args()

    if args.post_current_state:
        log.info("Refreshing current Secret Lair state")
        try:
            return 0 if post_current_state(load_state()) else 1
        except Exception:
            log.exception("Failed to refresh and post current state")
            return 1

    if not DISCORD_WEBHOOK_URL:
        log.warning(
            "DISCORD_WEBHOOK_URL is not set. Notifications will be logged but not sent."
        )

    log.info("Secret Lair Monitor starting")
    log.info(
        "Monitoring %d page(s), polling every %ds",
        len(PAGES),
        POLL_INTERVAL_SECONDS,
    )
    log.info("State file: %s", STATE_FILE)

    state = load_state()
    log.info("Loaded state with %d known products", len(state.get("known_products", {})))

    # First run: populate state without notifying (unless --notify-on-start flag)
    first_run = len(state.get("known_products", {})) == 0
    if first_run:
        log.info("First run detected — populating initial product list (no notifications)")
        # Temporarily unset webhook to suppress first-run notifications
        saved_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        notify_on_start = os.environ.get("NOTIFY_ON_START", "false").lower() == "true"
        if not notify_on_start:
            globals()["DISCORD_WEBHOOK_URL"] = ""

        state, _ = run_check(state)
        save_state(state)

        if not notify_on_start:
            globals()["DISCORD_WEBHOOK_URL"] = saved_webhook

        log.info(
            "Initial scan complete: %d products catalogued",
            len(state.get("known_products", {})),
        )

    in_maintenance = state.get("maintenance_mode", False)
    while True:
        was_in_maintenance = in_maintenance
        try:
            state, in_maintenance = run_check(state)
            save_state(state)
        except Exception as e:
            log.exception("Unhandled error during check cycle")
            send_discord_error_notification(
                f"{type(e).__name__}: {e}",
                context="Unhandled error during check cycle",
            )
            in_maintenance = state.get("maintenance_mode", False)

        # If we just transitioned from maintenance → up, skip the sleep and
        # check again immediately so the new drop is announced as soon as it
        # appears rather than waiting another poll interval.
        if was_in_maintenance and not in_maintenance:
            log.info("Recovery detected — re-checking immediately")
            continue

        interval = compute_sleep_interval(in_maintenance)
        log.debug("Sleeping %.1fs until next check", interval)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
