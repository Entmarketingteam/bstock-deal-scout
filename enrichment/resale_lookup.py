"""Resale price lookup for manifest items.

Uses Tavily search to find real sold/asking prices on:
1. eBay completed listings (most reliable — actual transaction prices)
2. Facebook Marketplace (local resale speed signal)
3. OfferUp / Craigslist (secondary)

Returns an estimated resale price per item for ROI calculation.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

try:
    from tavily import TavilyClient
    _TAVILY_AVAILABLE = True
except ImportError:
    _TAVILY_AVAILABLE = False


def _get_tavily() -> Any | None:
    api_key = os.getenv("TAVILY_API_KEY", "")
    if not api_key or not _TAVILY_AVAILABLE:
        return None
    return TavilyClient(api_key=api_key)


def _extract_price(text: str) -> float | None:
    """Pull the first dollar amount from a snippet."""
    m = re.search(r'\$\s*([\d,]+(?:\.\d{1,2})?)', text.replace(",", ""))
    if m:
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def lookup_resale_price(item: dict[str, Any]) -> dict[str, Any]:
    """
    Look up resale price for a single manifest item.

    item keys used: description, brand, upc, unit_retail

    Returns: {fb_price, fb_source, confidence}
    """
    tavily = _get_tavily()
    if not tavily:
        return {"fb_price": None, "fb_source": "no_tavily", "confidence": 0}

    description = item.get("description") or ""
    brand = item.get("brand") or ""
    upc = item.get("upc") or ""
    unit_retail = float(item.get("unit_retail") or 0)

    # Build search query — UPC is most precise, fall back to brand+name
    if upc and len(upc) >= 8:
        query = f'"{upc}" sold price eBay OR "facebook marketplace" OR offerup'
    elif brand and description:
        query = f'"{brand} {description}" sold price eBay completed listing'
    elif description:
        query = f'"{description}" sold price eBay OR marketplace'
    else:
        return {"fb_price": None, "fb_source": "no_query", "confidence": 0}

    try:
        results = tavily.search(
            query=query,
            search_depth="basic",
            max_results=5,
            include_answer=True,
        )
    except Exception as exc:
        log.warning("Tavily search failed for '%s': %s", query[:60], exc)
        return {"fb_price": None, "fb_source": "tavily_error", "confidence": 0}

    prices: list[float] = []
    sources: list[str] = []

    # Check answer first
    answer = results.get("answer", "")
    if answer:
        p = _extract_price(answer)
        if p and (unit_retail == 0 or p < unit_retail * 1.5):
            prices.append(p)

    for r in results.get("results", []):
        url = r.get("url", "")
        snippet = r.get("content", "") + " " + r.get("title", "")
        p = _extract_price(snippet)
        if p and p > 1 and (unit_retail == 0 or p < unit_retail * 2):
            prices.append(p)
            sources.append(url)

    if not prices:
        return {"fb_price": None, "fb_source": "no_results", "confidence": 0}

    # Use median to avoid outliers
    prices.sort()
    median_price = prices[len(prices) // 2]

    # Confidence: 3+ results = high, 2 = medium, 1 = low
    confidence = min(3, len(prices))

    source = "ebay_sold" if any("ebay" in s for s in sources) else \
             "marketplace" if any("facebook" in s or "offerup" in s for s in sources) else \
             "web_search"

    return {
        "fb_price": round(median_price, 2),
        "fb_source": source,
        "confidence": confidence,
    }


def enrich_manifest_with_resale(
    items: list[dict[str, Any]],
    max_items: int = 20,
) -> list[dict[str, Any]]:
    """
    Enrich up to max_items manifest items with resale price lookups.

    Prioritizes items by unit_retail descending (highest value items first).
    Returns items with fb_price populated.
    """
    from datetime import datetime, timezone

    # Sort by value, take top N
    sorted_items = sorted(items, key=lambda x: float(x.get("unit_retail") or 0), reverse=True)
    to_enrich = sorted_items[:max_items]

    enriched = []
    total_fb_value = 0.0

    for item in to_enrich:
        result = lookup_resale_price(item)
        item = {**item, **result}
        if result.get("fb_price"):
            qty = int(item.get("qty") or 1)
            total_fb_value += result["fb_price"] * qty
        item["fb_searched_at"] = datetime.now(timezone.utc).isoformat()
        enriched.append(item)

    # Remaining items without lookup — use 15% of retail as placeholder
    remainder = sorted_items[max_items:]
    for item in remainder:
        retail = float(item.get("unit_retail") or 0)
        if retail:
            est = round(retail * 0.15, 2)
            item = {**item, "fb_price": est, "fb_source": "estimated_15pct"}
            total_fb_value += est * int(item.get("qty") or 1)
        enriched.append(item)

    return enriched, round(total_fb_value, 2)
