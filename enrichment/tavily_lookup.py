"""Enrich manifest line items with real retail prices and product images.

For each SKU we build a query from brand + model + description and ask
Tavily (or Serper as a fallback) to search Google Shopping / the open web.
We filter results to trusted retail domains and take the highest-confidence
match as the "true" market price.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

import httpx

log = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"

TRUSTED_DOMAINS = {
    "ferguson.com": 1.0,
    "fergusonhome.com": 1.0,
    "winstonwatercooler.com": 1.0,
    "homedepot.com": 0.95,
    "lowes.com": 0.95,
    "build.com": 0.95,
    "wayfair.com": 0.85,
    "amazon.com": 0.85,
    "walmart.com": 0.8,
    "kohler.com": 1.0,
    "hydrosystems.com": 1.0,
    "signaturehardware.com": 1.0,
    "grainger.com": 0.95,
    "zoro.com": 0.9,
    "plumbingsupply.com": 0.9,
    "qualitybath.com": 0.9,
}


def _build_query(item: dict[str, Any]) -> str:
    parts = []
    if item.get("brand") and item["brand"] not in ("N/A", "", None):
        parts.append(str(item["brand"]))
    if item.get("model") and item["model"] not in ("N/A", "", None):
        parts.append(str(item["model"]))
    desc = item.get("description", "")
    if desc:
        # First 6 words of description usually = brand + model + type
        parts.append(" ".join(str(desc).split()[:6]))
    return " ".join(parts).strip()


def _extract_price(text: str | None) -> float | None:
    import re
    if not text:
        return None
    m = re.search(r"\$([\d,]+(?:\.\d{2})?)", text)
    return float(m.group(1).replace(",", "")) if m else None


def _domain_of(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).netloc.replace("www.", "").lower()


@lru_cache(maxsize=2048)
def enrich_item(query: str) -> dict[str, Any] | None:
    """Run a single Tavily search. Cached by exact query string."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key or not query:
        return None
    try:
        r = httpx.post(
            TAVILY_URL,
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 8,
                "include_images": True,
                "include_domains": list(TRUSTED_DOMAINS.keys()),
            },
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        log.warning("Tavily lookup failed for %r: %s", query, exc)
        return None

    best = None
    best_score = 0.0
    for result in data.get("results", []):
        url = result.get("url", "")
        domain = _domain_of(url)
        domain_score = TRUSTED_DOMAINS.get(domain, 0)
        if domain_score == 0:
            continue
        price = _extract_price(result.get("content", "")) or _extract_price(result.get("title", ""))
        if not price:
            continue
        score = domain_score * (result.get("score", 0.5) or 0.5)
        if score > best_score:
            best_score = score
            best = {
                "real_price": price,
                "real_source_domain": domain,
                "real_source_url": url,
                "real_image_url": (data.get("images") or [None])[0],
                "confidence": round(score, 3),
            }
    return best


def enrich_manifest(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for item in items:
        q = _build_query(item)
        result = enrich_item(q) if q else None
        if result:
            item.update(result)
        enriched.append(item)
    return enriched
