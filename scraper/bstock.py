"""B-Stock API scraper.

Flow:
  1. POST auth.bstock.com/api/login → FusionAuth JWT (no OAuth UI, no browser)
  2. GET bstock.com/all-auctions RSC with JWT as cookie → listing JSON embedded in RSC
     (primary path — blocked by Cloudflare if Railway IP is flagged)
  3. FALLBACK: If RSC returns 403 (Cloudflare), load known auction IDs from Supabase
     and refresh each via /buy/listings/details/{id} which bypasses CF.

The /buy/listings/details/{id} endpoint is NOT protected by Cloudflare interactive
challenges — confirmed in production logs. It serves as the CF-bypass fallback.
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

# ── Webshare sticky residential proxy ────────────────────────────────────────
_WEBSHARE_USER = os.getenv("WEBSHARE_PROXY_USER", "")
_WEBSHARE_PASS = os.getenv("WEBSHARE_PROXY_PASS", "")
_WEBSHARE_SESSION = "bstock1"  # fixed session = same residential IP per scrape run


def _webshare_proxy_url() -> str | None:
    """Return Webshare sticky residential proxy URL, or None if not configured."""
    if not _WEBSHARE_USER or not _WEBSHARE_PASS:
        return None
    return f"http://{_WEBSHARE_USER}-session-{_WEBSHARE_SESSION}:{_WEBSHARE_PASS}@p.webshare.io:80"


# ── Browserbase (Cloudflare Turnstile bypass for /all-auctions discovery) ─────
# /all-auctions is now behind a route-level Cloudflare Turnstile interstitial
# ("just a moment") that residential-proxy httpx retries cannot clear. We drive
# a Browserbase managed headless browser to clear the challenge, then run the
# RSC fetch *inside* the browser context (same origin, cf_clearance cookie + JA3
# ride along) and feed the body to the existing _extract_listings_from_rsc parser.
_BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY", "")
_BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID", "")
# Single attempt with these capability modes. advanced_stealth is Enterprise-only
# (session create 403s with "Advanced stealth mode is only available on the
# Enterprise plan"), so it is NOT in the default. On the Hobby plan, "proxies"
# + auto captcha-solving clears the Turnstile interstitial (verified live: 201
# session, Turnstile solved, listings returned). Tune via the env var, e.g. set
# BROWSERBASE_MODES="proxies,advanced_stealth,verified" only on Enterprise.
_BROWSERBASE_MODES = os.getenv("BROWSERBASE_MODES", "proxies")


def _browserbase_available() -> bool:
    return bool(_BROWSERBASE_API_KEY and _BROWSERBASE_PROJECT_ID)



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


def _fetch_rsc_page(token: str, offset: int = 0, condition_qs: str = "condition=%5B%22New%22%5D", proxy: str | None = None) -> str:
    """Fetch one RSC page and return decompressed body text. proxy: optional HTTP proxy URL."""
    url = LISTINGS_RSC_BASE.format(condition_qs=condition_qs, offset=offset)
    _get_kwargs: dict = {
        "headers": {
            "User-Agent": BROWSER_UA,
            "Cookie": f"token={token}; access_token={token}",
            "Authorization": f"Bearer {token}",
            "RSC": "1",
            "Accept-Encoding": "gzip",
        },
        "timeout": 30,
        "follow_redirects": True,
    }
    if proxy:
        _get_kwargs["proxy"] = proxy
    resp = httpx.get(url, **_get_kwargs)
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
    # Fallback: parse "Ext. Retail $XX,XXX" from title if retailPrice is missing
    if not msrp:
        import re as _re
        _m = _re.search(r'Ext\.\s*Retail\s*\$([0-9,]+)', obj.get("title") or "")
        if _m:
            msrp = float(_m.group(1).replace(",", ""))
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


def _scrape_via_rsc(token: str, conditions: list[str], proxy: str | None = None) -> list[Listing]:
    """Primary scrape path: /all-auctions RSC endpoint. Returns empty list on CF 403."""
    import urllib.parse as _ulp
    import json as _json

    condition_qs = "condition=" + _ulp.quote(_json.dumps(conditions))
    results: list[Listing] = []
    seen_ids: set[str] = set()
    offset = 0

    while True:
        log.info("Fetching RSC page offset=%d", offset)
        try:
            body = _fetch_rsc_page(token, offset=offset, condition_qs=condition_qs, proxy=proxy)
        except RuntimeError as exc:
            if "403" in str(exc):
                log.warning("RSC endpoint returned 403 (Cloudflare block) — will use DB fallback")
            else:
                log.error("RSC fetch failed at offset=%d: %s", offset, exc)
            break
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

    return results


def _build_rsc_url(conditions: list[str], offset: int) -> str:
    """Build the /all-auctions RSC URL for a given condition set + offset."""
    import urllib.parse as _ulp
    import json as _json
    condition_qs = "condition=" + _ulp.quote(_json.dumps(conditions))
    return LISTINGS_RSC_BASE.format(condition_qs=condition_qs, offset=offset)


def _create_browserbase_session(bb, modes: list[str]):
    """Create a Browserbase session with the requested capability modes.

    modes may contain: "proxies", "advanced_stealth", "verified".
    Raises browserbase.APIStatusError (e.g. 402/plan-tier) which the caller
    surfaces verbatim rather than masking.
    """
    # snake_case keys = the SDK's documented input; it transforms them to the
    # camelCase wire aliases. Passing camelCase directly is not the SDK contract.
    browser_settings: dict = {"solve_captchas": True}
    use_proxies = "proxies" in modes
    if "advanced_stealth" in modes:
        browser_settings["advanced_stealth"] = True
    if "verified" in modes:
        browser_settings["verified"] = True
    return bb.sessions.create(
        project_id=_BROWSERBASE_PROJECT_ID,
        proxies=use_proxies,
        browser_settings=browser_settings,
    )


def _scrape_via_browserbase(token: str, conditions: list[str]) -> list[Listing]:
    """Discovery via Browserbase managed browser that clears Cloudflare Turnstile.

    Flow (single session, Turnstile solved once):
      1. Create session (proxies + advanced stealth), connect over CDP.
      2. Inject JWT cookies (token / access_token) for .bstock.com.
      3. Navigate to /all-auctions, wait for the interstitial to clear.
      4. Run the RSC fetch *in-page* per offset (cf_clearance + correct JA3 ride
         along), feed each body to _extract_listings_from_rsc — full parser reuse.

    RSC-only: if the in-page RSC fetch ever returns unparseable bodies, a DOM-card
    parser would need to be added; not implemented (RSC has the rich fields).
    """
    if not _browserbase_available():
        log.warning("Browserbase not configured (BROWSERBASE_API_KEY/PROJECT_ID) — skipping")
        return []

    try:
        from browserbase import Browserbase
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        log.error("Browserbase/Playwright not installed: %s", exc)
        return []

    modes = [m.strip() for m in _BROWSERBASE_MODES.split(",") if m.strip()]
    bb = Browserbase(api_key=_BROWSERBASE_API_KEY)

    try:
        session = _create_browserbase_session(bb, modes)
    except Exception as exc:
        # Plan-tier / quota errors (402 Payment Required, advanced_stealth gating)
        # are real blockers — surface verbatim, do not silently fall through.
        log.error("Browserbase session create failed (modes=%s): %s", modes, exc)
        return []

    log.info("Browserbase session created: %s (modes=%s)", session.id, modes)
    results: list[Listing] = []
    seen_ids: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(session.connect_url)
        try:
            # Use the session's pre-bound context (stealth/proxy attach to it).
            ctx = browser.contexts[0]
            ctx.add_cookies([
                {"name": "token", "value": token, "domain": ".bstock.com", "path": "/"},
                {"name": "access_token", "value": token, "domain": ".bstock.com", "path": "/"},
            ])
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Surface Browserbase captcha-solving lifecycle in logs.
            page.on("console", lambda msg: (
                log.info("Browserbase: Turnstile solving started") if msg.text == "browserbase-solving-started"
                else log.info("Browserbase: Turnstile solving finished") if msg.text == "browserbase-solving-finished"
                else None
            ))

            # Navigate to the listings page to clear the interstitial once.
            nav_url = _build_rsc_url(conditions, 0).replace("&_rsc=rsc1", "")
            log.info("Browserbase: navigating to %s", nav_url)
            page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the challenge to clear: the interstitial title is
            # "Just a moment..."; the real page is not. Poll up to ~30s.
            for _ in range(30):
                try:
                    title = (page.title() or "").lower()
                except Exception:
                    page.wait_for_timeout(1000)
                    continue
                if "just a moment" not in title and "attention required" not in title:
                    break
                page.wait_for_timeout(1000)
            else:
                try:
                    current_title = page.title()
                except Exception:
                    current_title = "Unknown (navigating)"
                log.warning("Browserbase: interstitial did not clear in time (title=%r)", current_title)

            # Loop offsets via in-page RSC fetch (same origin → cf_clearance rides).
            offset = 0
            while True:
                rsc_url = _build_rsc_url(conditions, offset)
                body = page.evaluate(
                    """async ({url, tok}) => {
                        const r = await fetch(url, {
                            headers: {'RSC': '1', 'Authorization': 'Bearer ' + tok},
                            credentials: 'include',
                        });
                        return {status: r.status, text: await r.text()};
                    }""",
                    {"url": rsc_url, "tok": token},
                )
                status = body.get("status")
                text = body.get("text") or ""
                if status != 200:
                    log.warning("Browserbase in-page RSC offset=%d returned status=%s", offset, status)
                    break

                page_listings, total = _extract_listings_from_rsc(text)
                log.info("Browserbase RSC offset=%d: %d listings (total=%d)", offset, len(page_listings), total)
                if not page_listings:
                    break
                for obj in page_listings:
                    listing = _listing_from_rsc_obj(obj)
                    if listing and listing.auction_id not in seen_ids:
                        seen_ids.add(listing.auction_id)
                        results.append(listing)
                offset += len(page_listings)
                if offset >= total:
                    break
        finally:
            browser.close()

    log.info("Browserbase discovery: %d listings", len(results))
    return results


def _scrape_via_db_fallback(token: str) -> list[Listing]:
    """CF-bypass fallback: reload known auction IDs from Supabase, refresh each via
    /buy/listings/details/{id} which is NOT Cloudflare-protected.

    This keeps the service operational when Railway's IP is CF-blocked on /all-auctions.
    """
    import os as _os

    supabase_url = _os.environ.get("SUPABASE_URL", "").rstrip("/")
    svc_key = _os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not supabase_url or not svc_key:
        log.error("Supabase env vars missing — cannot run DB fallback")
        return []

    log.info("RSC blocked — loading active auction IDs from Supabase for DB fallback")

    from datetime import datetime, timezone, timedelta
    now_iso = datetime.now(timezone.utc).isoformat()
    # Only reload lots that haven't ended yet (time_remaining > now)
    # Also include recently ended lots (within 2 days) in case timing is off
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()

    try:
        r = httpx.get(
            f"{supabase_url}/rest/v1/bstock_listings",
            params={
                "time_remaining": f"gt.{cutoff_iso}",
                "select": "auction_id,storefront",
                "limit": "500",
            },
            headers={"apikey": svc_key, "Authorization": f"Bearer {svc_key}"},
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json()
    except Exception as exc:
        log.error("DB fallback: Supabase query failed: %s", exc)
        return []

    if not rows:
        log.warning("DB fallback: no active auction IDs found in Supabase")
        return []

    log.info("DB fallback: refreshing %d known auction IDs via detail endpoint", len(rows))

    # Import here to avoid circular import (fetch_listing imports from scraper.bstock)
    from scraper.fetch_listing import fetch_listing

    results: list[Listing] = []
    for row in rows:
        aid = row["auction_id"]
        try:
            detail = fetch_listing(aid)
            if not detail:
                log.debug("DB fallback: no data for %s", aid)
                continue
            # Convert detail dict back to a Listing object
            listing = Listing(
                auction_id=aid,
                url=detail.get("url") or f"https://bstock.com/buy/listings/details/{aid}",
                title=detail.get("title"),
                image_url=detail.get("image_url"),
                manifest_doc_url=detail.get("manifest_doc_url"),
                location=detail.get("location"),
                listing_type=detail.get("listing_type"),
                condition=detail.get("condition"),
                unit_count=detail.get("unit_count"),
                msrp=detail.get("msrp"),
                current_bid=detail.get("current_bid"),
                pct_of_msrp=detail.get("pct_of_msrp"),
                per_unit=detail.get("per_unit"),
                time_remaining=detail.get("time_remaining"),
                bid_count=detail.get("bid_count"),
                price_label=detail.get("price_label"),
                storefront=detail.get("storefront") or row.get("storefront"),
            )
            results.append(listing)
        except Exception as exc:
            log.warning("DB fallback: failed to refresh %s: %s", aid, exc)

    log.info("DB fallback: refreshed %d listings", len(results))
    return results


def scrape_listings(conditions: list[str] | None = None) -> list[Listing]:
    """Scrape all active B-Stock listings for the given condition(s).

    conditions: list of B-Stock condition strings, e.g. ["New"], ["New", "Used"].
    Defaults to SCRAPE_CONDITIONS env var, or ["New"] if not set.

    B-Stock condition values: "New", "Used", "Salvage"

    If the primary RSC endpoint is blocked by Cloudflare (403), falls back to
    refreshing known auction IDs from Supabase via /buy/listings/details/{id}.
    """
    if conditions is None:
        raw = os.getenv("SCRAPE_CONDITIONS", "New")
        conditions = [c.strip() for c in raw.split(",") if c.strip()]

    token = get_jwt_token()
    log.info("Scraping conditions: %s", conditions)

    # 1. Fast-path: direct RSC fetch (cheap, no browser cost). Succeeds only if
    #    the request IP isn't hitting the route-level Cloudflare Turnstile.
    results = _scrape_via_rsc(token, conditions)

    # 2. Primary discovery: Browserbase managed browser clears Turnstile.
    #    The /all-auctions route now serves an interactive Turnstile interstitial
    #    that proxy-rotated httpx cannot bypass — only a real browser solving the
    #    challenge can. This is the main discovery path when the fast-path 403s.
    if not results:
        log.info("Direct RSC returned 0 — attempting Browserbase Turnstile bypass")
        results = _scrape_via_browserbase(token, conditions)
        if results:
            log.info("Browserbase discovery succeeded: %d listings", len(results))

    # 3. Last resort: refresh known auction IDs from Supabase via the detail
    #    endpoint (not Turnstile-protected). Cannot discover NEW listings.
    if not results:
        log.warning("Discovery returned 0 — activating DB fallback (refresh-only)")
        results = _scrape_via_db_fallback(token)

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
