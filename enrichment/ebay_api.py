"""eBay API client for sold comps and active listing prices.

Uses:
  - OAuth client_credentials flow for app-level access token
  - Browse API v1 (item_summary/search) for active listings
  - Finding API v1 (findCompletedItems) for sold/completed listings

Token is cached in-process and refreshed 5 minutes before expiry.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import time
import urllib.parse
from typing import Any

import httpx

log = logging.getLogger(__name__)

_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}

EBAY_OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
EBAY_FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"
EBAY_SCOPE = "https://api.ebay.com/oauth/api_scope"


def _credentials() -> tuple[str, str]:
    client_id = os.getenv("EBAY_CLIENT_ID", "")
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "")
    return client_id, client_secret


def available() -> bool:
    client_id, client_secret = _credentials()
    return bool(client_id and client_secret)


def get_token() -> str:
    """Return a valid OAuth app token, fetching/refreshing as needed."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 300:
        return _token_cache["token"]

    client_id, client_secret = _credentials()
    if not (client_id and client_secret):
        raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")

    creds_b64 = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    r = httpx.post(
        EBAY_OAUTH_URL,
        headers={
            "Authorization": f"Basic {creds_b64}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=f"grant_type=client_credentials&scope={EBAY_SCOPE}",
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + int(data.get("expires_in", 7200))
    return _token_cache["token"]


def _browse_search(query: str, limit: int = 10, filter_str: str = "") -> list[dict]:
    """eBay Browse API active listing search."""
    try:
        token = get_token()
        # Note: ASPECT_REFINEMENTS returns NO itemSummaries — omit fieldgroups
        # for the default summary response.
        params: dict[str, Any] = {
            "q": query,
            "limit": limit,
        }
        if filter_str:
            params["filter"] = filter_str
        r = httpx.get(
            EBAY_BROWSE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                "Content-Type": "application/json",
            },
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("itemSummaries", [])
    except Exception as exc:
        log.debug("Browse API error: %s", exc)
        return []


def _finding_completed(query: str, limit: int = 10) -> list[dict]:
    """eBay Finding API — completed (sold) items.

    httpx encodes parentheses in param keys as %28/%29 which eBay rejects.
    Build the URL manually so itemFilter(0).name stays literal.
    """
    try:
        client_id, _ = _credentials()
        # Safe params — no special chars in keys
        base = urllib.parse.urlencode({
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.0.0",
            "SECURITY-APPNAME": client_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "keywords": query,
            "sortOrder": "EndTimeSoonest",
            "paginationInput.entriesPerPage": str(limit),
        })
        # itemFilter params must stay un-encoded so eBay can parse them
        filters = "itemFilter(0).name=SoldItemsOnly&itemFilter(0).value=true"
        url = f"{EBAY_FINDING_URL}?{base}&{filters}"
        r = httpx.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        items = (
            data.get("findCompletedItemsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )
        return items
    except Exception as exc:
        log.debug("Finding API error: %s", exc)
        return []


def _price_from_finding_item(item: dict) -> float | None:
    try:
        return float(
            item["sellingStatus"][0]["currentPrice"][0]["__value__"]
        )
    except (KeyError, IndexError, ValueError, TypeError):
        return None


def _active_price_estimate(query: str, unit_retail: float) -> dict[str, Any]:
    """
    Fallback when Finding API is unavailable.

    Uses Browse API active listings and discounts to estimate sold price:
      - New/Like-New condition listings → median × 0.85 (assume sold slightly below ask)
      - Applies retail sanity filter same as Finding API path
    Returns same keys as sold_comps for drop-in compatibility.
    """
    active = _browse_search(
        query, limit=10,
        filter_str="conditionIds:{1000|1500|2000|2500|3000}",
    )
    if not active:
        return {"ebay_sold_price": None, "ebay_sold_count": 0}

    active_prices: list[float] = []
    for a in active:
        try:
            p = float(a["price"]["value"])
            if p > 0.50 and (unit_retail == 0 or p <= unit_retail * 1.10):
                active_prices.append(p)
        except (KeyError, ValueError, TypeError):
            pass

    if not active_prices:
        return {"ebay_sold_price": None, "ebay_sold_count": 0}

    active_prices.sort()
    n = len(active_prices)
    median_active = active_prices[n // 2] if n % 2 else (active_prices[n // 2 - 1] + active_prices[n // 2]) / 2
    # Active listings tend to be ~15-20% above actual sold prices
    est_sold = round(median_active * 0.82, 2)

    return {
        "ebay_sold_price": est_sold,
        "ebay_sold_count": n,  # count of active comps used for estimate
        "ebay_active_low": min(active_prices),
        "ebay_price_source": "browse_active_est",  # flag: not real sold data
    }


def sold_comps(
    item: dict[str, Any],
    limit: int = 10,
) -> dict[str, Any]:
    """
    Fetch eBay sold comps for a manifest item.

    Tries Finding API (completed/sold listings) first.
    Falls back to Browse API active listing estimate if Finding is unavailable
    (rate-limited, error 10001, or otherwise failing).

    Returns:
      ebay_sold_price  — median sold price (or Browse-based estimate)
      ebay_sold_count  — number of comps
      ebay_low / ebay_high  — price range (Finding API only)
      ebay_active_low  — lowest current active listing
      ebay_price_source — "finding_sold" | "browse_active_est"
    """
    upc = (item.get("upc") or "").strip()
    brand = (item.get("brand") or "").strip()
    desc = (item.get("description") or "").strip()
    item_num = (item.get("item_num") or "").strip()

    # Build query — prefer UPC, then model number, then brand+description
    if upc and len(upc) >= 8:
        query = upc
    elif item_num and brand:
        query = f"{brand} {item_num}"
    elif brand and desc:
        # Trim description to key words (first 5 words)
        desc_short = " ".join(desc.split()[:5])
        query = f"{brand} {desc_short}"
    else:
        return {"ebay_sold_price": None, "ebay_sold_count": 0}

    unit_retail = float(item.get("unit_retail") or 0)

    # ── Try Finding API (real sold data) ────────────────────────────────────
    sold_items = _finding_completed(query, limit=limit)

    prices: list[float] = []
    for si in sold_items:
        p = _price_from_finding_item(si)
        if p and p > 0.50:
            # Sanity: discard prices > 110% of retail (likely bundles or wrong item)
            if unit_retail == 0 or p <= unit_retail * 1.10:
                prices.append(p)

    if prices:
        prices.sort()
        n = len(prices)
        median_price = prices[n // 2] if n % 2 else (prices[n // 2 - 1] + prices[n // 2]) / 2

        result: dict[str, Any] = {
            "ebay_sold_price": round(median_price, 2),
            "ebay_sold_count": n,
            "ebay_low": prices[0],
            "ebay_high": prices[-1],
            "ebay_price_source": "finding_sold",
        }

        # Grab lowest active listing as "what's it worth right now" signal
        active = _browse_search(query, limit=5, filter_str="conditionIds:{1000|1500|2000|2500|3000}")
        if active:
            active_prices = []
            for a in active:
                try:
                    p = float(a["price"]["value"])
                    active_prices.append(p)
                except (KeyError, ValueError, TypeError):
                    pass
            if active_prices:
                result["ebay_active_low"] = min(active_prices)

        return result

    # ── Finding API returned nothing — fall back to Browse estimate ──────────
    log.debug("Finding API empty for '%s', falling back to Browse active estimate", query)
    return _active_price_estimate(query, unit_retail)


def bulk_sold_comps(
    items: list[dict[str, Any]],
    max_items: int = 25,
    workers: int = 6,
) -> list[dict[str, Any]]:
    """
    Run sold_comps() on top N items (by unit_retail) in parallel.
    Returns items with eBay fields merged in.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sorted_items = sorted(items, key=lambda x: float(x.get("unit_retail") or 0), reverse=True)
    to_enrich = sorted_items[:max_items]
    remainder = sorted_items[max_items:]

    def _enrich(item: dict) -> dict:
        try:
            comps = sold_comps(item)
            return {**item, **comps}
        except Exception as exc:
            log.warning("eBay comp failed for '%s': %s", item.get("description", "?")[:40], exc)
            return item

    enriched = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_enrich, item): i for i, item in enumerate(to_enrich)}
        results_by_idx: dict[int, dict] = {}
        for f in as_completed(futures):
            idx = futures[f]
            try:
                results_by_idx[idx] = f.result()
            except Exception:
                results_by_idx[idx] = to_enrich[idx]
        enriched = [results_by_idx.get(i, to_enrich[i]) for i in range(len(to_enrich))]

    return enriched + remainder
