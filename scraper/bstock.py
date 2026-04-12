"""B-Stock API scraper — no browser, no Cloudflare issues.

Flow:
  1. POST auth.bstock.com/api/login → FusionAuth JWT (no OAuth UI, no browser)
  2. GET bstock.com/all-auctions RSC with JWT as cookie → listing JSON embedded in RSC
  3. Parse listings from RSC JSON array, paginate via offset param
  4. Return structured Listing objects with all deal data

Why this works: FusionAuth's /api/login endpoint doesn't go through the OAuth UI
(which shows a white screen) and doesn't hit Cloudflare on bstock.com. The RSC
endpoint passes Cloudflare silently because we have a valid JWT in the cookie.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

import httpx

log = logging.getLogger(__name__)

# ── Auth config ──────────────────────────────────────────────────────────────
FUSIONAUTH_URL = "https://auth.bstock.com/api/login"
FUSIONAUTH_CLIENT_ID = "1b094c5f-c8a6-416c-8c62-4dc77ca88ce9"
BSTOCK_EMAIL = os.getenv("BSTOCK_EMAIL", "marketingteam@nickient.com")
BSTOCK_PASSWORD = os.getenv("BSTOCK_PASSWORD", "")

# ── Listings endpoint ─────────────────────────────────────────────────────────
# Base RSC URL — condition filter injected dynamically by scrape_listings()
# B-Stock condition values: "New", "Used", "Salvage"
LISTINGS_RSC_BASE = "https://bstock.com/all-auctions?{condition_qs}&offset={offset}&_rsc=rsc1"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ── Token cache ───────────────────────────────────────────────────────────────
_cached_token: str = ""
_token_fetched_at: float = 0.0
TOKEN_TTL_SECONDS = 3500  # FusionAuth JWTs are typically 1hr; refresh at ~58min


@dataclass
class Listing:
    auction_id: str          # listing 'id' field from RSC
    url: str
    title: str | None
    image_url: str | None
    manifest_doc_url: str | None
    location: str | None
    listing_type: str | None       # "Auction" | "Make An Offer"
    condition: str | None          # inventoryType: Overstock | Customer Returns | New
    unit_count: int | None
    msrp: float | None             # retailPrice
    current_bid: float | None      # retailPrice * percentMsrp
    pct_of_msrp: float | None      # percentMsrp * 100
    per_unit: float | None         # current_bid / unit_count
    time_remaining: str | None     # ISO end time string
    bid_count: int | None          # numberOfBids
    price_label: str | None        # deal field: "Great Price" | "Good Price" | "Fair Price"
    storefront: str | None         # storefrontName

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def get_jwt_token() -> str:
    """Return a cached FusionAuth JWT, refreshing when near expiry."""
    global _cached_token, _token_fetched_at
    if _cached_token and (time.time() - _token_fetched_at) < TOKEN_TTL_SECONDS:
        return _cached_token

    if not BSTOCK_PASSWORD:
        raise RuntimeError(
            "BSTOCK_PASSWORD env var not set. "
            "Add it to Doppler ent-agency-automation/prd."
        )

    log.info("Fetching FusionAuth JWT for %s", BSTOCK_EMAIL)
    resp = httpx.post(
        FUSIONAUTH_URL,
        json={
            "loginId": BSTOCK_EMAIL,
            "password": BSTOCK_PASSWORD,
            "applicationId": FUSIONAUTH_CLIENT_ID,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"FusionAuth login failed: {resp.status_code} {resp.text[:200]}"
        )
    data = resp.json()
    _cached_token = data["token"]
    _token_fetched_at = time.time()
    log.info("Got FusionAuth JWT (len=%d)", len(_cached_token))
    return _cached_token


def _fetch_rsc_page(token: str, offset: int = 0, condition_qs: str = "condition=%5B%22New%22%5D") -> str:
    """Fetch one RSC page and return decompressed body text."""
    url = LISTINGS_RSC_BASE.format(condition_qs=condition_qs, offset=offset)
    resp = httpx.get(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Cookie": f"token={token}; access_token={token}",
            "Authorization": f"Bearer {token}",
            "RSC": "1",
            "Accept-Encoding": "gzip",
        },
        timeout=30,
        follow_redirects=True,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"RSC fetch failed: {resp.status_code} url={url}"
        )
    # httpx auto-decompresses gzip
    return resp.text


def _extract_listings_from_rsc(body: str) -> tuple[list[dict], int]:
    """Parse listings JSON array from Next.js RSC body.

    The RSC stream embeds a JSON array of listing objects ending with
    ],"total":{N},"limit":...  We find that anchor and walk back to
    extract the full array.

    Returns (listings_list, total_count).
    """
    # Find total count
    m = re.search(r'"total":(\d+),"limit":', body)
    total = int(m.group(1)) if m else 0

    if total == 0:
        return [], 0

    # Find the ], right before ,"total":
    idx_total = m.start() if m else -1
    if idx_total < 0:
        return [], 0

    # Walk back from idx_total to find the ] that closes the listings array
    arr_end = body.rfind("]", 0, idx_total)
    if arr_end < 0:
        return [], 0

    # Walk back further to find matching [
    depth = 0
    arr_start = -1
    for i in range(arr_end, -1, -1):
        if body[i] == "]":
            depth += 1
        elif body[i] == "[":
            depth -= 1
            if depth == 0:
                arr_start = i
                break

    if arr_start < 0:
        return [], 0

    try:
        listings = json.loads(body[arr_start : arr_end + 1])
        return listings, total
    except json.JSONDecodeError as exc:
        log.warning("JSON decode failed for RSC array: %s", exc)
        return [], 0


def _listing_from_rsc_obj(obj: dict) -> Listing | None:
    """Convert a raw RSC listing dict to a Listing dataclass."""
    listing_id = obj.get("id") or obj.get("listingId")
    if not listing_id:
        return None

    url = f"https://bstock.com/buy/listings/details/{listing_id}"

    msrp = obj.get("retailPrice")
    pct = obj.get("percentMsrp")
    current_bid: float | None = None
    pct_of_msrp: float | None = None
    if msrp and pct:
        current_bid = round(msrp * pct, 2)
        pct_of_msrp = round(pct * 100, 4)

    units = obj.get("units")
    per_unit: float | None = None
    if current_bid and units and units > 0:
        per_unit = round(current_bid / units, 2)

    # Condition: prefer inventoryType ("Overstock", "Customer Returns", etc.)
    condition = obj.get("inventoryType")
    if not condition:
        pkg = obj.get("packagingCondition") or []
        condition = pkg[0] if pkg else None

    # Pricing strategy → listing_type
    strategy = obj.get("pricingStrategy", "")
    if strategy == "AUCTION":
        listing_type = "Auction"
    elif strategy in ("MAKE_AN_OFFER", "OFFER"):
        listing_type = "Make An Offer"
    else:
        listing_type = strategy or None

    # Deal label is in the 'deal' field
    price_label = obj.get("deal") or None

    # Manifest: try docserv URL from 'sku' field
    sku = obj.get("sku") or obj.get("sellerLotId", [None])[0] if obj.get("sellerLotId") else None
    manifest_url: str | None = None
    # Modern manifests are at docserv; try the listing documents if available
    # (documents not in RSC, but sku can be used later to look up)
    site_abb = obj.get("siteAbb", "")

    # winning bid takes precedence over computed bid
    winning = obj.get("winningBidAmount")
    if winning:
        current_bid = winning
        if msrp and winning:
            pct_of_msrp = round((winning / msrp) * 100, 4)
        if winning and units and units > 0:
            per_unit = round(winning / units, 2)

    return Listing(
        auction_id=listing_id,
        url=url,
        title=obj.get("title"),
        image_url=obj.get("primaryImageUrl"),
        manifest_doc_url=manifest_url,
        location=obj.get("region"),
        listing_type=listing_type,
        condition=condition,
        unit_count=units,
        msrp=msrp,
        current_bid=current_bid,
        pct_of_msrp=pct_of_msrp,
        per_unit=per_unit,
        time_remaining=obj.get("endTime"),
        bid_count=obj.get("numberOfBids"),
        price_label=price_label,
        storefront=obj.get("storefrontName"),
    )


def scrape_listings(conditions: list[str] | None = None) -> list[Listing]:
    """Scrape all active B-Stock listings for the given condition(s).

    conditions: list of B-Stock condition strings, e.g. ["New"], ["New", "Used"].
    Defaults to SCRAPE_CONDITIONS env var, or ["New"] if not set.

    B-Stock condition values: "New", "Used", "Salvage"
    """
    import os, urllib.parse
    if conditions is None:
        raw = os.getenv("SCRAPE_CONDITIONS", "New")
        conditions = [c.strip() for c in raw.split(",") if c.strip()]

    # Build condition query string: condition=%5B%22New%22%2C%22Used%22%5D
    import json as _json
    condition_qs = "condition=" + urllib.parse.quote(_json.dumps(conditions))

    token = get_jwt_token()
    results: list[Listing] = []
    seen_ids: set[str] = set()
    offset = 0

    log.info("Scraping conditions: %s", conditions)
    while True:
        log.info("Fetching RSC page offset=%d", offset)
        try:
            body = _fetch_rsc_page(token, offset=offset, condition_qs=condition_qs)
        except Exception as exc:
            log.error("RSC fetch failed at offset=%d: %s", offset, exc)
            break

        page_listings, total = _extract_listings_from_rsc(body)
        log.info("  Got %d listings (total=%d)", len(page_listings), total)

        if not page_listings:
            break

        for obj in page_listings:
            listing = _listing_from_rsc_obj(obj)
            if listing and listing.auction_id not in seen_ids:
                seen_ids.add(listing.auction_id)
                results.append(listing)

        offset += len(page_listings)
        if offset >= total or not page_listings:
            break

    log.info("Scraped %d total listings", len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    listings = scrape_listings()
    print(f"\n{len(listings)} listings scraped\n")
    for l in listings[:10]:
        bid_str = f"${l.current_bid:,.0f}" if l.current_bid else "no bid"
        pct_str = f"{l.pct_of_msrp:.1f}%" if l.pct_of_msrp else "?"
        msrp_str = f"${l.msrp:,.0f}" if l.msrp else "?"
        store = (l.storefront or "-")[:20]
        label = (l.price_label or "-")[:12]
        title = (l.title or "")[:60]
        print(f"[{label:12}] {store:20} {bid_str:>10} / {msrp_str} MSRP ({pct_str}) — {title}")
