"""Quality assessment for manifest items.

For each item, we query up to 4 sources and build a confidence-weighted
resale price + quality score:

  1. eBay completed (sold) listings  — actual market transaction prices
  2. Home Depot                       — retail availability + current price
  3. Lowe's                           — retail availability + current price
  4. Manufacturer (Kohler, Moen, etc) — MSRP / spec confirmation

Sources are queried via Tavily search with targeted site: filters.
An eBay API key (EBAY_API_KEY) will be used if present for higher
accuracy; falls back to Tavily search otherwise.

Quality score (1–10):
  10 = Multiple sold eBay comps, available at HD/Lowe's, new condition
   7 = 1-2 eBay sold comps OR HD/Lowe's price found
   5 = Only manufacturer MSRP available
   3 = No comps found, discontinued / niche product
   1 = Cannot find any price signal
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

log = logging.getLogger(__name__)

try:
    from tavily import TavilyClient
    _TAVILY_OK = True
except ImportError:
    _TAVILY_OK = False

# ─── helpers ────────────────────────────────────────────────────────────────

def _tavily() -> Any | None:
    key = os.getenv("TAVILY_API_KEY", "")
    return TavilyClient(api_key=key) if (key and _TAVILY_OK) else None


def _extract_price(text: str) -> float | None:
    """Pull lowest plausible dollar amount from a text snippet."""
    prices = []
    for m in re.finditer(r'\$\s*([\d,]+(?:\.\d{1,2})?)', text):
        try:
            p = float(m.group(1).replace(",", ""))
            if 1 < p < 50_000:
                prices.append(p)
        except ValueError:
            pass
    return min(prices) if prices else None


def _extract_prices(text: str) -> list[float]:
    prices = []
    for m in re.finditer(r'\$\s*([\d,]+(?:\.\d{1,2})?)', text):
        try:
            p = float(m.group(1).replace(",", ""))
            if 1 < p < 50_000:
                prices.append(p)
        except ValueError:
            pass
    return prices


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# ─── per-source lookups ─────────────────────────────────────────────────────

def _search_ebay_sold(client: Any, item: dict) -> dict[str, Any]:
    """Search eBay completed listings for sold prices.

    Uses real eBay API if credentials available, otherwise Tavily search.
    """
    # Prefer real eBay API
    try:
        from enrichment.ebay_api import available as ebay_available, sold_comps
        if ebay_available():
            return sold_comps(item)
    except ImportError:
        pass

    # Tavily fallback
    if not client:
        return {"ebay_sold_price": None, "ebay_sold_count": 0}

    upc = item.get("upc") or ""
    brand = item.get("brand") or ""
    desc = item.get("description") or ""

    if upc and len(upc) >= 8:
        q = f'site:ebay.com "{upc}" sold completed'
    elif brand and desc:
        q = f'site:ebay.com "{brand} {desc}" sold completed listing price'
    else:
        return {"ebay_sold_price": None, "ebay_sold_count": 0}

    try:
        r = client.search(query=q, search_depth="basic", max_results=8, include_answer=True)
        all_prices: list[float] = []
        for result in r.get("results", []):
            snippet = result.get("content", "") + " " + result.get("title", "")
            all_prices.extend(_extract_prices(snippet))
        if r.get("answer"):
            all_prices.extend(_extract_prices(r["answer"]))

        unit_retail = float(item.get("unit_retail") or 0)
        if unit_retail > 0:
            all_prices = [p for p in all_prices if p <= unit_retail * 1.1 and p > 0]

        if not all_prices:
            return {"ebay_sold_price": None, "ebay_sold_count": 0}

        median_price = _median(all_prices)
        return {
            "ebay_sold_price": round(median_price, 2) if median_price else None,
            "ebay_sold_count": len(all_prices),
        }
    except Exception as exc:
        log.debug("eBay Tavily fallback error: %s", exc)
        return {"ebay_sold_price": None, "ebay_sold_count": 0}


def _search_home_depot(client: Any, item: dict) -> dict[str, Any]:
    upc = item.get("upc") or ""
    brand = item.get("brand") or ""
    desc = item.get("description") or ""

    if upc and len(upc) >= 8:
        q = f'site:homedepot.com "{upc}"'
    elif brand and desc:
        q = f'site:homedepot.com "{brand}" "{desc[:40]}" price'
    else:
        return {"hd_price": None}

    try:
        r = client.search(query=q, search_depth="basic", max_results=4)
        for result in r.get("results", []):
            if "homedepot.com" in result.get("url", ""):
                p = _extract_price(result.get("content", "") + result.get("title", ""))
                if p:
                    return {"hd_price": round(p, 2), "hd_url": result.get("url")}
        return {"hd_price": None}
    except Exception as exc:
        log.debug("HD search error: %s", exc)
        return {"hd_price": None}


def _search_lowes(client: Any, item: dict) -> dict[str, Any]:
    upc = item.get("upc") or ""
    brand = item.get("brand") or ""
    desc = item.get("description") or ""

    if upc and len(upc) >= 8:
        q = f'site:lowes.com "{upc}"'
    elif brand and desc:
        q = f'site:lowes.com "{brand}" "{desc[:40]}" price'
    else:
        return {"lowes_price": None}

    try:
        r = client.search(query=q, search_depth="basic", max_results=4)
        for result in r.get("results", []):
            if "lowes.com" in result.get("url", ""):
                p = _extract_price(result.get("content", "") + result.get("title", ""))
                if p:
                    return {"lowes_price": round(p, 2), "lowes_url": result.get("url")}
        return {"lowes_price": None}
    except Exception as exc:
        log.debug("Lowes search error: %s", exc)
        return {"lowes_price": None}


def _search_manufacturer(client: Any, item: dict) -> dict[str, Any]:
    brand = (item.get("brand") or "").lower()
    desc = item.get("description") or ""
    item_num = item.get("item_num") or ""
    upc = item.get("upc") or ""

    # Map brands to their direct site
    brand_sites = {
        "kohler": "kohler.com",
        "moen": "moen.com",
        "delta": "deltafaucet.com",
        "american standard": "americanstandard-us.com",
        "grohe": "grohe.com",
        "hansgrohe": "hansgrohe.com",
        "ferguson": "ferguson.com",
    }
    site = next((v for k, v in brand_sites.items() if k in brand), None)
    if not site:
        return {"mfr_price": None}

    if item_num:
        q = f'site:{site} "{item_num}" price'
    elif upc:
        q = f'site:{site} "{upc}"'
    else:
        q = f'site:{site} "{desc[:50]}" price'

    try:
        r = client.search(query=q, search_depth="basic", max_results=4)
        for result in r.get("results", []):
            if site in result.get("url", ""):
                p = _extract_price(result.get("content", "") + result.get("title", ""))
                if p:
                    return {"mfr_price": round(p, 2), "mfr_url": result.get("url")}
        return {"mfr_price": None}
    except Exception as exc:
        log.debug("Mfr search error: %s", exc)
        return {"mfr_price": None}


# ─── quality score ───────────────────────────────────────────────────────────

def _quality_score(item: dict) -> tuple[float, str]:
    """Return (score 1-10, notes string) based on price signal coverage."""
    ebay = item.get("ebay_sold_price")
    ebay_count = item.get("ebay_sold_count") or 0
    hd = item.get("hd_price")
    lowes = item.get("lowes_price")
    mfr = item.get("mfr_price")
    condition = (item.get("condition") or "").lower()
    unit_retail = float(item.get("unit_retail") or 0)

    score = 1.0
    notes = []

    # eBay sold comps — strongest signal
    if ebay and ebay_count >= 3:
        score += 4.0
        notes.append(f"eBay: {ebay_count} sold comps @ ${ebay:,.0f}")
    elif ebay and ebay_count >= 1:
        score += 2.5
        notes.append(f"eBay: {ebay_count} sold comp @ ${ebay:,.0f}")

    # Retail availability — means item is current, easy to compare/sell
    if hd and lowes:
        score += 2.5
        notes.append(f"HD ${hd:,.0f} / Lowe's ${lowes:,.0f}")
    elif hd:
        score += 1.5
        notes.append(f"HD ${hd:,.0f}")
    elif lowes:
        score += 1.5
        notes.append(f"Lowe's ${lowes:,.0f}")

    # Manufacturer site confirmation
    if mfr:
        score += 1.0
        notes.append(f"Mfr MSRP ${mfr:,.0f}")

    # Condition adjustment — stronger penalties for non-new lots
    if "overstock" in condition or "new" in condition:
        score += 0.5
        notes.append("New/Overstock ✓")
    elif "salvage" in condition:
        score -= 3.0
        notes.append("Salvage — expect heavy damage ↓↓↓")
    elif "return" in condition:
        if "grade a" in condition:
            score -= 1.5
            notes.append("Returns Grade A ↓")
        elif "grade b" in condition:
            score -= 2.5
            notes.append("Returns Grade B — mixed quality ↓↓")
        elif "grade c" in condition:
            score -= 3.0
            notes.append("Returns Grade C — heavy damage likely ↓↓↓")
        else:
            score -= 2.0
            notes.append("Customer Returns ↓↓")

    # Retail price vs eBay ratio — big discount means high quality deal
    if ebay and unit_retail > 0:
        ratio = ebay / unit_retail
        if ratio > 0.6:
            notes.append(f"sells at {ratio*100:.0f}% of retail")
        elif ratio > 0.4:
            notes.append(f"sells at {ratio*100:.0f}% of retail")
        else:
            score -= 0.5
            notes.append(f"low resale ratio {ratio*100:.0f}%")

    score = max(1.0, min(10.0, round(score, 1)))
    return score, " | ".join(notes)


# ─── main entry point ────────────────────────────────────────────────────────

def assess_items(
    items: list[dict[str, Any]],
    max_items: int = 25,
    workers: int = 4,
) -> list[dict[str, Any]]:
    """
    Run quality assessment on top N manifest items (by unit_retail).

    Runs eBay, HD, Lowe's, and manufacturer lookups in parallel per item.
    Returns items with quality fields populated.
    """
    client = _tavily()
    if not client:
        log.warning("Tavily not available — skipping quality assessment")
        return items

    # Sort by value, assess top N
    sorted_items = sorted(items, key=lambda x: float(x.get("unit_retail") or 0), reverse=True)
    to_assess = sorted_items[:max_items]
    remainder = sorted_items[max_items:]

    def _assess_one(item: dict) -> dict:
        # Run all 4 lookups in parallel
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {
                ex.submit(_search_ebay_sold, client, item): "ebay",
                ex.submit(_search_home_depot, client, item): "hd",
                ex.submit(_search_lowes, client, item): "lowes",
                ex.submit(_search_manufacturer, client, item): "mfr",
            }
            results = {}
            for f in as_completed(futures):
                try:
                    results.update(f.result())
                except Exception as exc:
                    log.debug("Lookup failed: %s", exc)

        item = {**item, **results}
        score, notes = _quality_score(item)
        item["quality_score"] = score
        item["quality_notes"] = notes

        # Best available resale price — adjusted for condition
        # New/Overstock: sell at 65% of retail comp; Returns: 50%; Salvage: 35%
        cond = (item.get("condition") or "").lower()
        if "salvage" in cond:
            hd_mult, retail_mult = 0.40, 0.10
        elif "return" in cond:
            if "grade a" in cond:
                hd_mult, retail_mult = 0.55, 0.13
            elif "grade b" in cond or "grade c" in cond:
                hd_mult, retail_mult = 0.45, 0.10
            else:
                hd_mult, retail_mult = 0.50, 0.12
        else:
            hd_mult, retail_mult = 0.65, 0.15

        resale = (
            item.get("ebay_sold_price")
            or (item.get("hd_price") and item["hd_price"] * hd_mult)
            or (item.get("lowes_price") and item["lowes_price"] * hd_mult)
            or float(item.get("unit_retail") or 0) * retail_mult
        )
        item["fb_price"] = round(resale, 2) if resale else None

        return item

    # Assess items with parallel workers across items too
    assessed = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_assess_one, item): i for i, item in enumerate(to_assess)}
        # Preserve order
        results_by_idx = {}
        for f in as_completed(futures):
            idx = futures[f]
            try:
                results_by_idx[idx] = f.result()
            except Exception as exc:
                log.warning("Item assessment failed at idx %d: %s", idx, exc)
                results_by_idx[idx] = to_assess[idx]

        assessed = [results_by_idx.get(i, to_assess[i]) for i in range(len(to_assess))]

    # Remainder items — estimate only
    for item in remainder:
        retail = float(item.get("unit_retail") or 0)
        item["fb_price"] = round(retail * 0.15, 2) if retail else None
        item["quality_score"] = 3.0
        item["quality_notes"] = "estimated — not in top-25 by value"

    return assessed + remainder
