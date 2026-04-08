"""B-Stock Playwright scraper with session persistence.

Logs in once, saves storage_state.json, reuses cookies on subsequent runs.
Scrapes the all-auctions listings page and extracts listing cards without
relying on hashed CSS class names (which change on every B-Stock deploy).

Extraction strategy: select the repeating listing card container by its
test ID or stable attribute, then parse visible text by position:
  [image, manifest_link, location, listing_type, title, condition, units,
   msrp, bid, pct_of_msrp, per_unit, time_remaining, bid_count, price_label]

If B-Stock changes the DOM meaningfully, fall back to regex over the full
rendered text of each card.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Page, sync_playwright

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

log = logging.getLogger(__name__)

STORAGE_STATE_PATH = Path(os.getenv("BSTOCK_STORAGE_STATE", "/tmp/bstock_storage.json"))
LOGIN_URL = "https://bstock.com/acct/signin"
LISTINGS_URL = 'https://bstock.com/all-auctions?condition=%5B%22New%22%5D'
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class Listing:
    auction_id: str
    url: str
    title: str | None
    image_url: str | None
    manifest_doc_url: str | None
    location: str | None
    listing_type: str | None        # Auction | Make An Offer
    condition: str | None            # Overstock | Customer Returns
    unit_count: int | None
    msrp: float | None
    current_bid: float | None
    pct_of_msrp: float | None
    per_unit: float | None
    time_remaining: str | None
    bid_count: int | None
    price_label: str | None
    storefront: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _money_to_float(s: str | None) -> float | None:
    if not s:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", s.replace("$", ""))
    return float(m.group(0).replace(",", "")) if m else None


def _extract_auction_id(url: str) -> str:
    m = re.search(r"/details/([a-f0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/auction/view/id/(\d+)", url)
    if m:
        return f"legacy-{m.group(1)}"
    return url


def _bootstrap_storage_state_from_env() -> None:
    """If BSTOCK_STORAGE_STATE_B64 env var is set, decode it to disk on startup."""
    b64 = os.getenv("BSTOCK_STORAGE_STATE_B64")
    if not b64 or STORAGE_STATE_PATH.exists():
        return
    import base64
    try:
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STORAGE_STATE_PATH.write_bytes(base64.b64decode(b64))
        log.info("Decoded storage state from env var to %s", STORAGE_STATE_PATH)
    except Exception as exc:
        log.error("Failed to decode BSTOCK_STORAGE_STATE_B64: %s", exc)


def _wait_for_cloudflare(page: Page, timeout_ms: int = 45000) -> None:
    """Wait out the Cloudflare 'Verifying...' challenge page if present."""
    try:
        # CF challenge page title
        page.wait_for_function(
            """() => !document.title.toLowerCase().includes('just a moment')
                   && !document.body.innerText.toLowerCase().includes('verifying')""",
            timeout=timeout_ms,
        )
    except Exception as exc:
        log.warning("Cloudflare wait timed out: %s", exc)


def login_if_needed(page: Page) -> None:
    page.goto(LISTINGS_URL, wait_until="domcontentloaded")
    _wait_for_cloudflare(page)

    # Already logged in if we can see auction cards / no email form
    logged_in_signal = page.locator("a[href*='/buy/listings/details/']").count() > 0
    if logged_in_signal:
        log.info("Already logged in (listings visible)")
        return

    if os.getenv("BSTOCK_REQUIRE_COOKIES", "true").lower() == "true":
        raise RuntimeError(
            "Session expired or no cookies loaded. Run scripts/bootstrap_session.py "
            "locally and update BSTOCK_STORAGE_STATE_B64 on Railway."
        )

    email = os.environ["BSTOCK_EMAIL"]
    password = os.environ["BSTOCK_PASSWORD"]
    log.info("Logging in to B-Stock as %s", email)

    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    _wait_for_cloudflare(page)

    # Step 1: email page. Type slowly to look human.
    email_input = page.locator("input[type='email'], input[name='email'], input[placeholder*='mail' i]").first
    email_input.wait_for(state="visible", timeout=30000)
    email_input.click()
    page.wait_for_timeout(300)
    email_input.type(email, delay=80)
    page.wait_for_timeout(500)

    # Click "Let's go" button
    page.locator(
        "button:has-text(\"Let's go\"), button:has-text('Lets go'), button:has-text('Continue'), button[type='submit']"
    ).first.click()

    # Step 2: password page
    password_input = page.locator("input[type='password'], input[name='password']").first
    password_input.wait_for(state="visible", timeout=30000)
    _wait_for_cloudflare(page)
    password_input.click()
    page.wait_for_timeout(300)
    password_input.type(password, delay=80)
    page.wait_for_timeout(500)

    page.locator(
        "button:has-text('Sign in'), button:has-text('Log in'), button:has-text(\"Let's go\"), button[type='submit']"
    ).first.click()

    page.wait_for_load_state("networkidle", timeout=45000)
    page.goto(LISTINGS_URL, wait_until="domcontentloaded")
    _wait_for_cloudflare(page)


def scroll_to_load_all(page: Page, max_scrolls: int = 30) -> None:
    """Infinite-scroll handler. Scrolls until no new content appears."""
    prev_height = 0
    for i in range(max_scrolls):
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(800)
        height = page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height
    log.info("Scrolled %d times, final height %d", i + 1, prev_height)


def parse_card(card_html: str, card_text: str, card_links: list[str], card_images: list[str]) -> Listing | None:
    """Parse a single listing card's extracted data into a Listing."""
    detail_url = next((u for u in card_links if "/buy/listings/details/" in u or "/auction/view/id/" in u), None)
    if not detail_url:
        return None
    manifest_url = next(
        (u for u in card_links if "docserv.bstock.com" in u or "manifest-prod.bstock.com" in u),
        None,
    )
    image_url = next((u for u in card_images if "bfile-prod" in u or "catalog/product" in u), None)

    # Normalize text into lines
    lines = [ln.strip() for ln in card_text.splitlines() if ln.strip()]
    joined = " | ".join(lines)

    # Pull fields by regex
    location = next((ln for ln in lines if re.match(r"^[A-Za-z .]+, [A-Z]{2}$", ln)), None)
    listing_type = "Auction" if "Auction" in joined and "Make An Offer" not in joined else ("Make An Offer" if "Make An Offer" in joined else None)
    condition = next((c for c in ("Overstock", "Customer Returns", "New", "Refurbished", "Salvage") if c in joined), None)

    units_m = re.search(r"([\d,]+)\s+units?", joined)
    unit_count = int(units_m.group(1).replace(",", "")) if units_m else None

    msrp_m = re.search(r"MSRP:\s*\$([\d,]+(?:\.\d+)?)", joined)
    msrp = float(msrp_m.group(1).replace(",", "")) if msrp_m else None

    pct_m = re.search(r"([\d.]+)%\s*of\s*MSRP", joined)
    pct_of_msrp = float(pct_m.group(1)) if pct_m else None

    per_unit_m = re.search(r"\$([\d,]+(?:\.\d+)?)\s*per\s*unit", joined)
    per_unit = float(per_unit_m.group(1).replace(",", "")) if per_unit_m else None

    time_m = re.search(r"(\d+d\s*\d+h|\d+h\s*\d+m|\d+m\s*\d+s)", joined)
    time_remaining = time_m.group(1) if time_m else None

    price_label = next((lbl for lbl in ("Great Price", "Good Price", "Fair Price") if lbl in joined), None)

    # Title: longest line that isn't one of the above
    title_candidates = [ln for ln in lines if len(ln) > 20 and "MSRP" not in ln and "$" not in ln]
    title = max(title_candidates, key=len) if title_candidates else None

    # Current bid: $X (not per unit, not MSRP)
    bid_m = re.findall(r"\$([\d,]+(?:\.\d+)?)", joined)
    current_bid = None
    if bid_m and msrp:
        for b in bid_m:
            val = float(b.replace(",", ""))
            if val != msrp and (per_unit is None or val != per_unit):
                current_bid = val
                break

    # Bid count: bare integer between time and price label
    bid_count = None
    if time_remaining and price_label:
        tail = joined.split(time_remaining, 1)[-1].split(price_label, 1)[0]
        bc = re.search(r"\b(\d{1,3})\b", tail)
        bid_count = int(bc.group(1)) if bc else 0

    return Listing(
        auction_id=_extract_auction_id(detail_url),
        url=detail_url,
        title=title,
        image_url=image_url,
        manifest_doc_url=manifest_url,
        location=location,
        listing_type=listing_type,
        condition=condition,
        unit_count=unit_count,
        msrp=msrp,
        current_bid=current_bid,
        pct_of_msrp=pct_of_msrp,
        per_unit=per_unit,
        time_remaining=time_remaining,
        bid_count=bid_count,
        price_label=price_label,
        storefront=None,
    )


def scrape_listings(url: str = LISTINGS_URL, headless: bool = True) -> list[Listing]:
    _bootstrap_storage_state_from_env()
    results: list[Listing] = []
    with sync_playwright() as pw:
        browser: Browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
            ],
        )
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": USER_AGENT,
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        if STORAGE_STATE_PATH.exists():
            context_kwargs["storage_state"] = str(STORAGE_STATE_PATH)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()
        if HAS_STEALTH:
            try:
                stealth_sync(page)
            except Exception as exc:
                log.warning("stealth_sync failed: %s", exc)

        login_if_needed(page)
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        scroll_to_load_all(page)

        # Save session for next run
        STORAGE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STORAGE_STATE_PATH))

        # Extract cards. Try common containers first.
        card_selector_candidates = [
            "[data-testid*='listing-card']",
            "[data-testid*='card']",
            "a[href*='/buy/listings/details/']",
        ]
        cards = []
        for sel in card_selector_candidates:
            cards = page.locator(sel).all()
            if len(cards) > 5:
                log.info("Using selector %s (%d cards)", sel, len(cards))
                break

        for card in cards:
            try:
                # Walk up to the containing card
                container = card.evaluate_handle(
                    "el => el.closest('[class*=\"card\"], [data-testid*=\"card\"], li, article') || el"
                ).as_element()
                if not container:
                    continue
                text = container.inner_text() or ""
                links = container.evaluate("el => Array.from(el.querySelectorAll('a')).map(a => a.href)") or []
                images = container.evaluate("el => Array.from(el.querySelectorAll('img')).map(i => i.src)") or []
                listing = parse_card("", text, links, images)
                if listing and listing.auction_id not in {r.auction_id for r in results}:
                    results.append(listing)
            except Exception as exc:
                log.warning("Card parse failed: %s", exc)

        browser.close()
    log.info("Scraped %d listings", len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    listings = scrape_listings(headless=False)
    print(json.dumps([l.to_dict() for l in listings[:5]], indent=2))
