"""FastAPI entry point. n8n cron hits POST /run every 15 min."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from alerts.dispatch import send_alert
from enrichment.bid_advisor import advise
from enrichment.quality_assessor import assess_items
from enrichment.resale_lookup import enrich_manifest_with_resale
from enrichment.shipping import estimate_shipping, landed_cost
from enrichment.tavily_lookup import enrich_manifest
from scoring import has_manifest, is_reno_relevant, qualifies_for_alert, tier
from scraper.bstock import scrape_listings
from scraper.manifest import fetch_and_parse
from scraper.fetch_listing import fetch_listing
from storage.db import (
    add_resale,
    add_to_watchlist,
    get_bid_history,
    get_resales,
    get_unalerted_qualifying,
    get_watchlist,
    insert_manifest_items,
    mark_alerted,
    record_bid_snapshot,
    remove_from_watchlist,
    upsert_listings,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("bstock-deal-scout")

app = FastAPI(title="bstock-deal-scout", version="0.1.0")


def _synthetic_items(listing: dict) -> list[dict]:
    """
    Create synthetic manifest items from listing title + MSRP + unit count
    when no CSV manifest exists (e.g. Kohler branded pallets).

    Parses titles like:
      "1 Pallet of Kitchen Faucets by Kohler, 12 Units, New Condition, $X MSRP"
      "3 Pallets of Bluetooth Shower Heads by Kohler, 165 Units"
    """
    import re as _re
    title = listing.get("title") or ""
    msrp = float(listing.get("msrp") or 0)
    unit_count = int(listing.get("unit_count") or 0)

    # Extract brand from "by <Brand>"
    brand_m = _re.search(r'\bby\s+([A-Za-z][A-Za-z &]+?)(?:\s*,|\s*$)', title, _re.IGNORECASE)
    brand = brand_m.group(1).strip() if brand_m else ""

    # Extract item description from "Pallet(s) of <Description>"
    desc_m = _re.search(r'pallets?\s+of\s+(.+?)(?:\s+by\s|\s*,)', title, _re.IGNORECASE)
    description = desc_m.group(1).strip() if desc_m else title[:60]

    # Determine condition
    condition = "New" if "new" in title.lower() else "Unknown"

    if not (brand or description) or unit_count == 0 or msrp == 0:
        return []

    unit_retail = round(msrp / unit_count, 2) if unit_count > 0 else 0

    return [{
        "brand": brand,
        "description": description,
        "qty": unit_count,
        "unit_retail": unit_retail,
        "ext_retail": msrp,
        "condition": condition,
        "lot_id": listing.get("auction_id"),
    }]


def _require_auth(x_trigger_secret: str | None) -> None:
    expected = os.getenv("TRIGGER_SECRET")
    if expected and x_trigger_secret != expected:
        raise HTTPException(status_code=401, detail="bad trigger secret")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/watch/{auction_id}")
def watch(auction_id: str, reason: str = "", x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    add_to_watchlist(auction_id, reason)
    # Immediately snap current state
    data = fetch_listing(auction_id)
    if data:
        upsert_listings([{**data, "auction_id": auction_id}])
        record_bid_snapshot(auction_id, data)
    return {"watching": auction_id, "current": data}


@app.delete("/watch/{auction_id}")
def unwatch(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    remove_from_watchlist(auction_id)
    return {"unwatched": auction_id}


@app.get("/bundles")
def bundle_deals(x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """
    Group current reno listings into complementary bundles for contractor/STR pitches.

    Returns curated package groupings with per-bathroom cost calculations.
    Designed to support an AI lookbook for BowTiedBroke-style STR developers.
    """
    _require_auth(x_trigger_secret)
    from storage.db import _client

    # Category detection patterns
    CATEGORIES = {
        "shower": ["shower", "bluetooth shower", "shower head", "shower trim"],
        "bath_fixtures": ["bath spout", "valve trim", "faucet", "tub", "bathtub", "bath faucet"],
        "accessories": ["towel", "soap dispenser", "hotelier", "bath accessory", "robe hook", "toilet paper"],
        "kitchen": ["kitchen faucet", "kitchen"],
        "hardware": ["door", "cabinet", "knob", "pull", "handle", "lock", "deadbolt"],
        "lighting": ["light", "chandelier", "ceiling fan", "lamp"],
    }

    def _categorize(listing: dict) -> list[str]:
        text = (listing.get("title") or "").lower()
        matched = []
        for cat, keywords in CATEGORIES.items():
            if any(kw in text for kw in keywords):
                matched.append(cat)
        return matched or ["other"]

    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "reno_relevant": "eq.true",
                "order": "roi_score.desc.nullslast",
                "select": "auction_id,title,storefront,location,msrp,current_bid,roi_score,lot_quality_score,fb_total_value,recommended_max_bid,walk_away_price,unit_count,shipping_estimate,url,time_remaining",
            },
        )
        r.raise_for_status()
        listings = r.json()

    # Classify + enrich each listing
    classified = []
    for row in listings:
        cats = _categorize(row)
        lc = landed_cost(row)
        units = int(row.get("unit_count") or 1)
        classified.append({
            **row,
            "categories": cats,
            "landed_cost": lc["total_landed"],
            "bstock_fee": lc["bstock_fee"],
            "shipping_estimate": lc["shipping_estimate"],
            "per_unit_cost": lc["per_unit_landed"],
            "per_unit_msrp": round(float(row.get("msrp") or 0) / units, 2) if units else 0,
        })

    # Bundle: "Complete Bath Suite" — shower + fixtures + accessories
    bath_bundle = [l for l in classified if any(c in l["categories"] for c in ["shower", "bath_fixtures", "accessories"])]
    kitchen_bundle = [l for l in classified if "kitchen" in l["categories"]]
    hardware_bundle = [l for l in classified if "hardware" in l["categories"]]

    def _bundle_summary(bundle: list[dict], name: str, bathrooms_per_unit: float = 1.0) -> dict:
        if not bundle:
            return {"name": name, "lots": [], "total_bid": 0, "total_units": 0}
        total_bid = sum(float(l.get("current_bid") or 0) for l in bundle)
        total_landed = sum(l["landed_cost"] for l in bundle)
        total_msrp = sum(float(l.get("msrp") or 0) for l in bundle)
        total_fb = sum(float(l.get("fb_total_value") or 0) for l in bundle)
        total_units = sum(int(l.get("unit_count") or 0) for l in bundle)
        # Estimate bathrooms: conservative based on accessory lot
        bathrooms = max(1, int(min(int(l.get("unit_count") or 0) for l in bundle) * bathrooms_per_unit))
        return {
            "name": name,
            "lots": [{"id": l["auction_id"], "title": l["title"], "bid": l.get("current_bid"), "units": l.get("unit_count"), "per_unit_cost": l["per_unit_cost"]} for l in bundle],
            "total_bid": total_bid,
            "total_landed": round(total_landed, 2),
            "total_msrp": total_msrp,
            "total_fb_value": total_fb,
            "total_units": total_units,
            "estimated_bathrooms": bathrooms,
            "cost_per_bathroom": round(total_landed / bathrooms, 2) if bathrooms else None,
            "msrp_per_bathroom": round(total_msrp / bathrooms, 2) if bathrooms else None,
            "bundle_roi": round((total_fb - total_landed) / total_landed, 4) if total_landed and total_fb else None,
        }

    bundles = [
        _bundle_summary(bath_bundle, "Complete Bath Suite (Shower + Fixtures + Accessories)"),
        _bundle_summary(kitchen_bundle, "Kitchen Package"),
        _bundle_summary(hardware_bundle, "Door & Cabinet Hardware Package"),
    ]

    # All premium hardware (bath + kitchen + hardware, skip outdoor/garden)
    premium = [l for l in classified if l["categories"] != ["other"]]
    bundles.append(_bundle_summary(premium, "Full Hardware Package (All Premium Lots)", bathrooms_per_unit=0.5))

    return {
        "total_reno_listings": len(listings),
        "bundles": [b for b in bundles if b["lots"]],
        "all_classified": [
            {k: v for k, v in l.items() if k != "raw_json"}
            for l in classified
        ],
    }


@app.get("/reno")
def reno_deals(x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """All reno-relevant listings with landed cost + ROI breakdown."""
    _require_auth(x_trigger_secret)
    from storage.db import _client
    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "reno_relevant": "eq.true",
                "has_manifest": "eq.true",
                "order": "roi_score.desc.nullslast",
                "select": "auction_id,title,storefront,location,msrp,current_bid,pct_of_msrp,price_label,time_remaining,has_manifest,shipping_estimate,fb_total_value,roi_score,unit_count,url,lot_quality_score,recommended_max_bid,walk_away_price,top_items",
            },
        )
        r.raise_for_status()
        rows = r.json()

    deals = []
    for row in rows:
        lc = landed_cost(row)
        deals.append({**row, **lc})

    return {"count": len(deals), "deals": deals}


@app.get("/landed-cost/{auction_id}")
def get_landed_cost(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    from storage.db import _client
    with _client() as c:
        r = c.get(f"/bstock_listings?auction_id=eq.{auction_id}&select=*")
        r.raise_for_status()
        rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="listing not found")
    return landed_cost(rows[0])


@app.get("/advise/{auction_id}")
def advise_lot(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """Full bid recommendation for a specific lot — runs live quality assessment."""
    _require_auth(x_trigger_secret)
    from storage.db import _client
    import os

    with _client() as c:
        r = c.get(f"/bstock_listings?auction_id=eq.{auction_id}&select=*")
        r.raise_for_status()
        rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="listing not found")
    listing = rows[0]

    # Get existing manifest items
    with _client() as c:
        r = c.get(f"/bstock_manifest_items?auction_id=eq.{auction_id}&order=unit_retail.desc")
        items = r.json() if r.status_code == 200 else []

    # If no items, try fetching manifest now
    if not items:
        detail = fetch_listing(auction_id)
        if detail and detail.get("manifest_doc_url"):
            from scraper.manifest import fetch_and_parse
            raw = fetch_and_parse(detail["manifest_doc_url"])
            if raw:
                items = enrich_manifest(raw)

    if not items:
        raise HTTPException(status_code=404, detail="no manifest items found")

    # Run full quality assessment + bid advice
    assessed = assess_items(items)
    ship = estimate_shipping(listing)
    result = advise(listing, assessed, shipping=ship or 300)

    return {
        "auction_id": auction_id,
        "title": listing.get("title"),
        "current_bid": listing.get("current_bid"),
        "shipping_estimate": ship,
        **result,
    }


@app.get("/history/{auction_id}")
def history(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    snapshots = get_bid_history(auction_id)
    return {"auction_id": auction_id, "snapshots": snapshots, "count": len(snapshots)}


# ── lookbook helpers (module-level so they're not recreated per request) ──────

def _condition_pill(cond: str) -> str:
    words = set(cond.lower().split())
    if "new" in words or "overstock" in words:
        return f'<span class="pill green">✅ {cond or "New"}</span>'
    if "salvage" in words:
        return f'<span class="pill" style="background:#fee2e2;color:#dc2626">⚠️ Salvage</span>'
    if "return" in words or "returns" in words:
        return f'<span class="pill" style="background:#fef9c3;color:#92400e">↩ {cond or "Returns"}</span>'
    return ""


def _time_info(time_val: str) -> tuple[str, str]:
    from datetime import datetime, timezone
    try:
        end_dt = datetime.fromisoformat(time_val.replace("Z", "+00:00"))
        diff = end_dt - datetime.now(timezone.utc)
        if diff.total_seconds() <= 0:
            return "ENDED", "#dc2626"
        h = int(diff.total_seconds() // 3600)
        m = int((diff.total_seconds() % 3600) // 60)
        color = "#dc2626" if h < 4 else ("#ca8a04" if h < 12 else "#16a34a")
        return f"{h}h {m}m", color
    except Exception:
        return time_val[:16] if time_val else "—", "#888"


def _finish_badge(text: str) -> str:
    t = text.lower()
    if "brushed nickel" in t or "-bn" in t:
        finish, color = "Brushed Nickel", "#7c6f5a"
    elif "matte black" in t or "-bl" in t:
        finish, color = "Matte Black", "#222"
    elif "polished chrome" in t or "-cp" in t:
        finish, color = "Polished Chrome", "#5a7c8a"
    elif "multiple" in t or "various" in t:
        finish, color = "Multiple Finishes", "#6b8f6b"
    else:
        return ""
    return f'<span class="finish-badge" style="background:{color}">{finish}</span>'


def _dalle_prompt(title: str, finish: str) -> str:
    """Build DALL-E 3 prompt for a cabin bathroom mockup based on lot title + finish."""
    title_low = title.lower()
    finish_desc = {
        "Brushed Nickel": "brushed nickel",
        "Matte Black": "matte black",
        "Polished Chrome": "polished chrome",
    }.get(finish, "brushed nickel")

    if any(kw in title_low for kw in ["towel", "hotelier", "robe", "accessory", "accessories"]):
        product_desc = f"{finish_desc} towel bars, towel rings, and bathroom accessories mounted on wall"
    elif any(kw in title_low for kw in ["shower head", "shower trim", "shower", "bluetooth shower"]):
        product_desc = f"{finish_desc} rainfall shower head and trim kit"
    elif any(kw in title_low for kw in ["kitchen faucet", "kitchen"]):
        product_desc = f"{finish_desc} kitchen faucet over farmhouse sink"
    elif any(kw in title_low for kw in ["bath spout", "valve trim", "tub", "faucet"]):
        product_desc = f"{finish_desc} tub filler faucet and bath hardware"
    else:
        product_desc = f"{finish_desc} bathroom hardware and fixtures"

    return (
        f"Professional interior design photograph of a luxury mountain cabin bathroom. "
        f"Warm cedar wood walls, natural stone tile floor, frameless glass shower. "
        f"Featured hardware: {product_desc}. "
        f"Warm ambient lighting, cozy rustic-modern aesthetic. "
        f"Photorealistic, 4K, architectural digest style. No people, no text."
    )


def _ensure_storage_bucket() -> bool:
    """Create bstock-mockups public storage bucket if it doesn't exist."""
    import httpx as _httpx
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    with _httpx.Client(base_url=supabase_url, headers=headers, timeout=15) as sc:
        r = sc.get("/storage/v1/bucket/bstock-mockups")
        if r.status_code == 200:
            return True
        r2 = sc.post("/storage/v1/bucket", json={"id": "bstock-mockups", "name": "bstock-mockups", "public": True})
        return r2.status_code in (200, 201)


def _generate_and_store_mockup(auction_id: str, title: str, finish: str, openai_key: str) -> str | None:
    """Generate one DALL-E 3 mockup, upload to Supabase Storage, return permanent URL."""
    import httpx as _httpx

    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    storage_headers = {"apikey": key, "Authorization": f"Bearer {key}"}

    prompt = _dalle_prompt(title, finish)

    with _httpx.Client(timeout=90) as c:
        r = c.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
            json={"model": "dall-e-3", "prompt": prompt, "n": 1, "size": "1024x1024",
                  "quality": "standard", "response_format": "url"},
        )
        if r.status_code != 200:
            log.error("DALL-E 3 error for %s %s: %s", auction_id, finish, r.text[:300])
            return None

        img_url = r.json()["data"][0]["url"]
        img_r = c.get(img_url)
        if img_r.status_code != 200:
            log.error("Failed to download DALL-E image for %s %s", auction_id, finish)
            return None
        img_bytes = img_r.content

    finish_slug = finish.lower().replace(" ", "-").replace("/", "-")
    path = f"{auction_id}/{finish_slug}.png"

    with _httpx.Client(base_url=supabase_url, timeout=30) as sc:
        ru = sc.post(
            f"/storage/v1/object/bstock-mockups/{path}",
            content=img_bytes,
            headers={**storage_headers, "Content-Type": "image/png", "x-upsert": "true"},
        )
        if ru.status_code not in (200, 201):
            log.error("Storage upload failed for %s: %s %s", path, ru.status_code, ru.text[:200])
            return None

    return f"{supabase_url}/storage/v1/object/public/bstock-mockups/{path}"


@app.post("/generate-mockups")
def generate_mockups(  # noqa: C901
    auction_ids: list[str] | None = None,
    finishes: list[str] | None = None,
    x_trigger_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """
    Generate AI cabin-bathroom mockups via DALL-E 3 for reno-relevant lots.
    Downloads each image and uploads to Supabase Storage for permanent hosting.
    URLs are saved to bstock_listings.ai_mockup_url (JSONB).

    - auction_ids: specific lots (default: all reno-relevant with quality >= 5)
    - finishes: list of finish names (default: ["Brushed Nickel", "Matte Black"])
    """
    _require_auth(x_trigger_secret)

    openai_key = os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

    _ensure_storage_bucket()
    finishes = finishes or ["Brushed Nickel", "Matte Black"]

    from storage.db import _client
    with _client() as c:
        params: dict[str, Any] = {
            "reno_relevant": "eq.true",
            "select": "auction_id,title,lot_quality_score,ai_mockup_url",
        }
        if auction_ids:
            ids_q = ",".join(f'"{i}"' for i in auction_ids)
            params["auction_id"] = f"in.({ids_q})"
        else:
            params["lot_quality_score"] = "gte.5"
        r = c.get("/bstock_listings", params=params)
        r.raise_for_status()
        lots = r.json()

    if not lots:
        return {"generated": 0, "message": "No qualifying lots found"}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _process_lot(lot: dict) -> dict:
        aid = lot["auction_id"]
        title = lot.get("title") or ""
        existing = lot.get("ai_mockup_url") or {}
        urls = dict(existing) if isinstance(existing, dict) else {}
        generated: list[str] = []

        for finish in finishes:
            finish_key = finish.lower().replace(" ", "_").replace("/", "_")
            if urls.get(finish_key):
                continue
            url = _generate_and_store_mockup(aid, title, finish, openai_key)
            if url:
                urls[finish_key] = url
                generated.append(finish)
                log.info("Generated mockup %s %s → %s", aid, finish, url)

        if generated:
            from storage.db import _client as _c2
            with _c2() as c:
                c.patch(f"/bstock_listings?auction_id=eq.{aid}", json={"ai_mockup_url": urls})

        return {"auction_id": aid, "title": title[:50], "generated": generated, "urls": urls}

    results = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_process_lot, lot): lot for lot in lots}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:
                lot = futures[fut]
                log.error("Mockup gen failed for %s: %s", lot.get("auction_id"), e)
                results.append({"auction_id": lot.get("auction_id"), "error": str(e)})

    total_gen = sum(len(r.get("generated", [])) for r in results)
    return {
        "lots_processed": len(results),
        "images_generated": total_gen,
        "cost_estimate_usd": round(total_gen * 0.04, 2),
        "results": results,
    }


@app.get("/lookbook-report", response_class=HTMLResponse)
def lookbook_report() -> HTMLResponse:  # noqa: C901
    """
    Visual contractor lookbook — individual product cards per manifest item, each with
    its own image and retailer link. Lot sections show bid/timing info above the product grid.
    Intentionally public — designed to be shared with buyers/contractors.
    """
    from storage.db import _client

    SKIP_KEYWORDS = ("outdoor", "garden", "power equipment")

    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "reno_relevant": "eq.true",
                "order": "roi_score.desc.nullslast",
                "select": "auction_id,title,storefront,msrp,current_bid,unit_count,shipping_estimate,roi_score,recommended_max_bid,walk_away_price,url,location,time_remaining,image_url,ai_mockup_url",
            },
        )
        r.raise_for_status()
        listings = [
            l for l in r.json()
            if not any(kw in (l.get("title") or "").lower() for kw in SKIP_KEYWORDS)
        ]

        items_by_lot: dict[str, list] = {}
        if listings:
            ids_q = ",".join(f'"{l["auction_id"]}"' for l in listings)
            mr = c.get("/bstock_manifest_items", params={
                "auction_id": f"in.({ids_q})",
                "order": "unit_retail.desc.nullslast",
                "select": "auction_id,description,brand,qty,unit_retail,condition,real_image_url,real_source_url,hd_url,lowes_url,mfr_url,hd_price,lowes_price",
            })
            for it in (mr.json() if mr.status_code == 200 else []):
                items_by_lot.setdefault(it["auction_id"], []).append(it)


    def _retailer_links(it: dict) -> str:
        links = []
        if it.get("hd_url"):
            links.append(f'<a href="{it["hd_url"]}" target="_blank" class="retailer-btn hd-btn">Home Depot</a>')
        if it.get("lowes_url"):
            links.append(f'<a href="{it["lowes_url"]}" target="_blank" class="retailer-btn lw-btn">Lowe\'s</a>')
        if it.get("mfr_url") and not it.get("hd_url"):
            links.append(f'<a href="{it["mfr_url"]}" target="_blank" class="retailer-btn mfr-btn">Manufacturer</a>')
        if it.get("real_source_url") and not links:
            links.append(f'<a href="{it["real_source_url"]}" target="_blank" class="retailer-btn mfr-btn">View Product</a>')
        return "".join(links)

    def _product_card(it: dict) -> str:
        img = it.get("real_image_url") or ""
        img_html = (
            f'<img src="{img}" class="prod-img" loading="lazy" onerror="this.closest(\'.prod-img-wrap\').style.display=\'none\'">'
            if img else ""
        )
        retail = it.get("unit_retail") or it.get("hd_price") or it.get("lowes_price") or 0
        retail_str = f"${float(retail):,.2f}" if retail else "—"
        qty = it.get("qty") or 1
        brand = (it.get("brand") or "").strip()
        desc = (it.get("description") or "").strip()
        name = f"{brand} {desc}".strip()[:65]
        finish_badge = _finish_badge(f"{brand} {desc} {it.get('hd_url','')}".lower())
        links_html = _retailer_links(it)
        return f"""
          <div class="prod-card">
            <div class="prod-img-wrap">{img_html}</div>
            <div class="prod-body">
              <div class="prod-name">{name}</div>
              <div class="prod-meta">
                {finish_badge}
                <span class="prod-retail">{retail_str} retail</span>
                <span class="prod-qty">Qty: {qty}</span>
              </div>
              <div class="prod-links">{links_html}</div>
            </div>
          </div>"""

    def _lot_section(l: dict) -> str:
        from enrichment.shipping import landed_cost as _lc
        aid = l["auction_id"]
        lc = _lc(l)
        bid = lc["bid"]
        bstock_fee = lc["bstock_fee"]
        ship = lc["shipping_estimate"]
        total_landed = lc["total_landed"]
        per_unit = lc["per_unit_landed"]
        msrp = lc["msrp"]
        units = int(l.get("unit_count") or 1)
        discount = round((1 - total_landed / msrp) * 100, 0) if msrp else 0
        rec = l.get("recommended_max_bid")
        time_str, time_color = _time_info(l.get("time_remaining") or "")
        bstock_url = l.get("url") or "#"

        # AI mockups for this lot
        mockup_urls = l.get("ai_mockup_url") or {}
        if isinstance(mockup_urls, str):
            import json as _j
            try:
                mockup_urls = _j.loads(mockup_urls)
            except Exception:
                mockup_urls = {}
        mockup_html = ""
        for key, label in [("brushed_nickel", "Brushed Nickel"), ("matte_black", "Matte Black")]:
            u = mockup_urls.get(key)
            if u:
                mockup_html += f'<div class="mockup-wrap"><img src="{u}" class="mockup-img" loading="lazy"><div class="mockup-label">{label}</div></div>'
        if mockup_html:
            mockup_html = f'<div class="mockup-row">{mockup_html}</div>'

        # Product cards for this lot — skip entirely if no manifest items
        items = items_by_lot.get(aid, [])
        if not items:
            return ""
        prod_cards = "".join(_product_card(it) for it in items)

        return f"""
      <div class="lot-section">
        <div class="lot-header">
          <div class="lot-header-left">
            <div class="lot-title">{l.get('title','')[:80]}</div>
            <div class="lot-pills">
              <span class="pill">📦 {units} units</span>
              <span class="pill">💵 MSRP ${msrp:,.0f}</span>
              <span class="pill green">🏷 {discount:.0f}% below MSRP</span>
              <span class="pill">📍 {l.get('location') or '—'}</span>
              {_condition_pill(l.get('condition') or '')}
            </div>
          </div>
          <div class="lot-header-right">
            <div class="lot-bid">${bid:,.0f}<span class="lot-bid-label">winning bid</span></div>
            <div class="cost-breakdown">
              <span class="cost-line">+ ${bstock_fee:,.0f} B-Stock fee ({lc['bstock_fee_pct']:.0f}%)</span>
              <span class="cost-line">+ ${ship:,.0f} est. freight</span>
              <span class="cost-total">= ${total_landed:,.0f} total landed</span>
            </div>
            <div class="lot-per-unit">${per_unit:,.2f}/unit landed</div>
            <div style="color:{time_color};font-weight:700;font-size:13px;margin-top:4px">⏱ {time_str}</div>
            {f'<div class="rec-bid-pill">Max bid: ${rec:,.0f}</div>' if rec else ''}
            <a href="{bstock_url}" target="_blank" class="bstock-auction-btn">View on B-Stock →</a>
          </div>
        </div>
        {mockup_html}
        <div class="prod-grid">
          {prod_cards}
        </div>
      </div>"""

    # ── aggregate stats ───────────────────────────────────────────────────────
    total_msrp = sum(float(l.get("msrp") or 0) for l in listings)
    total_bid = sum(float(l.get("current_bid") or 0) for l in listings)
    total_units = sum(int(l.get("unit_count") or 0) for l in listings)
    avg_discount = round((1 - total_bid / total_msrp) * 100, 0) if total_msrp else 0
    total_products = sum(len(v) for v in items_by_lot.values())

    sections_html = "".join(_lot_section(l) for l in listings)

    # ── AI renders standalone section (full-width at bottom) ──────────────────
    ai_cards_html = ""
    for l in listings:
        mockup_urls = l.get("ai_mockup_url") or {}
        if isinstance(mockup_urls, str):
            import json as _j
            try:
                mockup_urls = _j.loads(mockup_urls)
            except Exception:
                mockup_urls = {}
        short = (l.get("title") or "")[:40]
        for key, label in [("brushed_nickel", "Brushed Nickel"), ("matte_black", "Matte Black")]:
            u = mockup_urls.get(key)
            if u:
                ai_cards_html += f'<div class="ai-card"><img src="{u}" loading="lazy"><div class="ai-card-body"><div class="ai-card-label">{label}</div><div class="ai-card-sub">{short}</div></div></div>'

    if ai_cards_html:
        ai_section = f'<div class="ai-section"><h2>AI Bathroom Visualizations</h2><p class="ai-sub">DALL-E 3 renders — these fixtures installed in a mountain cabin bathroom</p><div class="ai-grid">{ai_cards_html}</div></div>'
    else:
        ai_section = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kohler + Signature Hardware — Contractor Lookbook</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;background:#f4f4f2;color:#1a1a1a}}
  a{{color:inherit}}
  /* ── Header ── */
  .header{{background:#1a1a1a;color:#fff;padding:36px 48px 28px}}
  .header h1{{font-size:28px;font-weight:800;letter-spacing:-.5px}}
  .header .sub{{color:#888;font-size:13px;margin-top:5px}}
  /* ── Stats bar ── */
  .stats-bar{{background:#fff;border-bottom:1px solid #eee;padding:18px 48px;display:flex;gap:40px;flex-wrap:wrap}}
  .s{{text-align:center}}
  .s .lbl{{font-size:9px;text-transform:uppercase;letter-spacing:.6px;color:#aaa}}
  .s .val{{font-size:20px;font-weight:800;margin-top:2px}}
  /* ── Pitch bar ── */
  .pitch-bar{{background:#fffbeb;border-bottom:1px solid #fde68a;padding:14px 48px;font-size:13px;color:#78350f;line-height:1.5}}
  /* ── Lot section ── */
  .lot-section{{background:#fff;margin:24px 48px;border-radius:12px;box-shadow:0 1px 6px rgba(0,0,0,.07);overflow:hidden}}
  .lot-header{{display:flex;justify-content:space-between;align-items:flex-start;padding:20px 24px;background:#fafafa;border-bottom:1px solid #eee;gap:20px;flex-wrap:wrap}}
  .lot-header-left{{flex:1;min-width:200px}}
  .lot-title{{font-size:15px;font-weight:700;line-height:1.4;margin-bottom:10px}}
  .lot-pills{{display:flex;flex-wrap:wrap;gap:6px}}
  .pill{{background:#f0f0f0;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:500;color:#444}}
  .pill.green{{background:#dcfce7;color:#166534}}
  .lot-header-right{{text-align:right;flex-shrink:0}}
  .lot-bid{{font-size:26px;font-weight:800;line-height:1}}
  .lot-bid-label{{font-size:10px;color:#aaa;font-weight:400;margin-left:4px;text-transform:uppercase}}
  .cost-breakdown{{margin-top:4px;display:flex;flex-direction:column;gap:1px}}
  .cost-line{{font-size:11px;color:#999}}
  .cost-total{{font-size:12px;font-weight:700;color:#111;border-top:1px solid #e0e0e0;padding-top:2px;margin-top:2px}}
  .lot-per-unit{{font-size:11px;color:#888;margin-top:3px}}
  .rec-bid-pill{{display:inline-block;margin-top:6px;background:#1a1a1a;color:#fff;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}}
  .bstock-auction-btn{{display:inline-block;margin-top:8px;padding:6px 14px;border-radius:6px;font-size:11px;font-weight:600;text-decoration:none;background:#f0f0f0;color:#555;border:1px solid #ddd}}
  .bstock-auction-btn:hover{{background:#e5e5e5}}
  /* ── AI mockup row ── */
  .mockup-row{{display:flex;gap:12px;padding:16px 24px;background:#f9f8ff;border-bottom:1px solid #ede9fe;overflow-x:auto}}
  .mockup-wrap{{flex-shrink:0;text-align:center}}
  .mockup-img{{width:280px;height:200px;object-fit:cover;border-radius:8px;display:block}}
  .mockup-label{{font-size:10px;color:#7c3aed;font-weight:600;margin-top:5px;text-transform:uppercase;letter-spacing:.4px}}
  /* ── Product grid ── */
  .prod-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:1px;background:#eee}}
  .prod-card{{background:#fff;padding:16px;display:flex;flex-direction:column;gap:10px}}
  .prod-img-wrap{{height:140px;display:flex;align-items:center;justify-content:center;background:#f9f9f9;border-radius:6px;overflow:hidden}}
  .prod-img{{max-width:100%;max-height:140px;object-fit:contain}}
  .prod-body{{flex:1;display:flex;flex-direction:column;gap:6px}}
  .prod-name{{font-size:12px;font-weight:600;line-height:1.4;color:#1a1a1a}}
  .prod-meta{{display:flex;flex-wrap:wrap;gap:4px;align-items:center}}
  .finish-badge{{font-size:9px;font-weight:700;color:#fff;padding:2px 7px;border-radius:10px;text-transform:uppercase;letter-spacing:.3px}}
  .prod-retail{{font-size:12px;font-weight:700;color:#16a34a}}
  .prod-qty{{font-size:10px;color:#888}}
  .prod-links{{display:flex;flex-wrap:wrap;gap:5px;margin-top:auto}}
  .retailer-btn{{padding:5px 10px;border-radius:5px;font-size:10px;font-weight:700;text-decoration:none;text-transform:uppercase;letter-spacing:.3px}}
  .hd-btn{{background:#f96302;color:#fff}}
  .hd-btn:hover{{background:#e05600}}
  .lw-btn{{background:#004990;color:#fff}}
  .lw-btn:hover{{background:#003870}}
  .mfr-btn{{background:#333;color:#fff}}
  .mfr-btn:hover{{background:#111}}
  .no-manifest{{padding:24px;color:#aaa;font-size:12px;text-align:center}}
  /* ── AI section ── */
  .ai-section{{padding:32px 48px 40px}}
  .ai-section h2{{font-size:18px;font-weight:800;margin-bottom:4px}}
  .ai-sub{{font-size:12px;color:#888;margin-bottom:16px}}
  .ai-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:16px}}
  .ai-card{{background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  .ai-card img{{width:100%;height:200px;object-fit:cover;display:block}}
  .ai-card-body{{padding:10px 14px}}
  .ai-card-label{{font-size:12px;font-weight:700}}
  .ai-card-sub{{font-size:10px;color:#888;margin-top:2px}}
  /* ── Footer ── */
  footer{{padding:28px 48px;font-size:11px;color:#aaa;border-top:1px solid #eee;background:#fff}}
</style>
</head>
<body>

<div class="header">
  <h1>Kohler + Signature Hardware</h1>
  <div class="sub">B-Stock Liquidation · All New Condition · Sourced for STR &amp; Cabin Developers</div>
</div>

<div class="stats-bar">
  <div class="s"><div class="lbl">Total MSRP</div><div class="val">${total_msrp:,.0f}</div></div>
  <div class="s"><div class="lbl">Current Bid</div><div class="val">${total_bid:,.0f}</div></div>
  <div class="s"><div class="lbl">Total Units</div><div class="val">{total_units:,}</div></div>
  <div class="s"><div class="lbl">Avg Discount</div><div class="val">{avg_discount:.0f}%+</div></div>
  <div class="s"><div class="lbl">Lots</div><div class="val">{len(listings)}</div></div>
  <div class="s"><div class="lbl">Individual Products</div><div class="val">{total_products}</div></div>
</div>

<div class="pitch-bar">
  <strong>Who this is for:</strong> Building 10–50 unit STR cabins? These Kohler lots let you outfit every bathroom with matching premium hardware at pennies on the dollar.
  Confirmed finish on towel bars &amp; accessories: <strong>Brushed Nickel</strong> — universally matched for mountain/cabin builds. Each product links directly to Home Depot or Lowe's for specs.
</div>

{sections_html}

{ai_section}

<footer>
  B-Stock Deal Scout · Live auction data · Each product card links to the manufacturer/retailer page · B-Stock links require a buyer account
</footer>

</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(secret: str | None = None) -> HTMLResponse:
    """Analytics dashboard — requires ?secret=TRIGGER_SECRET in URL."""
    trigger_secret = os.getenv("TRIGGER_SECRET", "")
    if not secret or secret != trigger_secret:
        return HTMLResponse(
            '<html><body style="background:#0f0f0f;color:#e8e8e8;font-family:sans-serif;padding:48px;text-align:center">'
            '<h2>Access denied</h2><p style="color:#666;margin-top:8px">Append ?secret=YOUR_TRIGGER_SECRET to the URL</p>'
            '</body></html>',
            status_code=403,
        )
    from dashboard import render_dashboard
    from storage.db import _client
    # Fetch all listings
    with _client() as c:
        r = c.get("/bstock_listings", params={
            "order": "created_at.desc",
            "select": "auction_id,title,storefront,msrp,current_bid,unit_count,lot_quality_score,recommended_max_bid,walk_away_price,has_manifest,shipping_estimate,time_remaining,condition,location,url,verdict",
            "limit": "500",
        })
        listings = r.json() if r.status_code == 200 else []
    # Fetch bid history for all lots
    history_map: dict[str, list] = {}
    if listings:
        aids_q = ",".join(f'"{l["auction_id"]}"' for l in listings)
        with _client() as c:
            r = c.get("/bstock_bid_history", params={
                "auction_id": f"in.({aids_q})",
                "order": "snapped_at.asc",
                "select": "auction_id,snapped_at,current_bid,bid_count",
            })
            for row in (r.json() if r.status_code == 200 else []):
                history_map.setdefault(row["auction_id"], []).append(row)
    resales = get_resales()
    html = render_dashboard(listings, history_map, resales, secret)
    return HTMLResponse(content=html, status_code=200)


@app.post("/log-sale")
def log_sale(
    payload: dict[str, Any],
    x_trigger_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Record an actual resale outcome. Body: auction_id, buy_price, sell_price, sell_channel, days_to_sell, notes."""
    _require_auth(x_trigger_secret)
    required = ["auction_id", "buy_price", "sell_price"]
    for field in required:
        if field not in payload:
            raise HTTPException(status_code=422, detail=f"Missing field: {field}")
    result = add_resale(
        auction_id=payload["auction_id"],
        buy_price=float(payload["buy_price"]),
        sell_price=float(payload["sell_price"]),
        sell_channel=payload.get("sell_channel", ""),
        days_to_sell=payload.get("days_to_sell"),
        notes=payload.get("notes", ""),
    )
    profit = float(payload["sell_price"]) - float(payload["buy_price"])
    roi = round((profit / float(payload["buy_price"])) * 100, 1) if float(payload["buy_price"]) else 0
    return {"ok": True, "profit": round(profit, 2), "roi_pct": roi, **result}


@app.post("/run")
def run(x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    log.info("=== Deal scout run start ===")

    # 1. Scrape
    listings = [l.to_dict() for l in scrape_listings()]
    log.info("Scraped %d listings", len(listings))

    # 2. Annotate listings with shipping estimate + reno relevance
    for l in listings:
        l["shipping_estimate"] = estimate_shipping(l)
        l["reno_relevant"] = is_reno_relevant(l)

    # 3. Upsert + identify new
    new_ids = upsert_listings(listings) if listings else set()

    # 3b. Auto-watchlist ALL lots from target storefronts — we want final bid data on every one
    #     regardless of quality score or manifest status
    WATCH_STOREFRONTS = ("winston", "kohler", "signature hardware")
    for l in listings:
        sf = (l.get("storefront") or "").lower()
        if any(ws in sf for ws in WATCH_STOREFRONTS):
            add_to_watchlist(l["auction_id"], reason=f"auto: target storefront ({l.get('storefront','')})")
            log.info("Auto-watchlisted %s from %s", l["auction_id"], l.get("storefront"))

    # 4. Proactively fetch manifests for ALL new listings (not just reno_relevant)
    #    — any lot with a real manifest gets full quality/ROI analysis
    enrich_on = os.getenv("ENRICH_MANIFESTS", "true").lower() == "true"
    if enrich_on:
        from storage.db import _client as _db_client
        with _db_client() as c:
            r = c.get(
                "/bstock_listings",
                params={
                    "has_manifest": "eq.false",
                    "select": "auction_id,url,title,storefront,current_bid,shipping_estimate,msrp,unit_count,condition",
                },
            )
            reno_no_manifest = r.json() if r.status_code == 200 else []

        for reno_listing in reno_no_manifest:
            aid = reno_listing["auction_id"]
            detail = fetch_listing(aid)

            # Only process lots with a real manifest doc — no synthetic fallback
            if not detail or not detail.get("manifest_doc_url"):
                log.debug("No manifest doc for %s — skipping enrichment", aid)
                continue

            raw_items = fetch_and_parse(detail["manifest_doc_url"])

            if raw_items:
                enriched = enrich_manifest(raw_items)
                assessed = assess_items(enriched)
                ship = float(reno_listing.get("shipping_estimate") or 300)
                advice = advise(reno_listing, assessed, shipping=ship)

                insert_manifest_items(aid, assessed)

                bid = float(reno_listing.get("current_bid") or 0)
                roi = round((advice["total_fb_value"] - bid - ship) / (bid + ship), 4) \
                      if (bid + ship) > 0 and advice["total_fb_value"] > 0 else None

                manifest_url = (detail or {}).get("manifest_doc_url")
                with _db_client() as c:
                    patch_payload = {
                        "has_manifest": bool(manifest_url),
                        "fb_total_value": advice["total_fb_value"],
                        "roi_score": roi,
                        "lot_quality_score": advice["lot_quality_score"],
                        "recommended_max_bid": advice["recommended_max_bid"],
                        "walk_away_price": advice["walk_away_price"],
                        "top_items": advice["top_items"],
                    }
                    if manifest_url:
                        patch_payload["manifest_doc_url"] = manifest_url
                    pr = c.patch(
                        f"/bstock_listings?auction_id=eq.{aid}",
                        json=patch_payload,
                    )
                    if pr.status_code >= 400:
                        log.error("Listings PATCH failed for %s: %s %s", aid, pr.status_code, pr.text[:300])
                log.info(
                    "Reno assessed %s: quality=%.1f fb_total=$%,.0f rec_bid=$%,.0f verdict=%s",
                    aid, advice["lot_quality_score"], advice["total_fb_value"],
                    advice["recommended_max_bid"], advice["verdict"],
                )

    # 5. Find qualifying unalerted
    qualifying = get_unalerted_qualifying()
    log.info("%d listings qualify for alert", len(qualifying))

    alerts_sent = 0

    for listing in qualifying:
        manifest_items: list[dict[str, Any]] = []
        if enrich_on and listing.get("manifest_doc_url"):
            raw_items = fetch_and_parse(listing["manifest_doc_url"])
            if raw_items:
                manifest_items = enrich_manifest(raw_items)
                # Run full quality + bid advice pipeline
                manifest_items = assess_items(manifest_items)
                ship = float(listing.get("shipping_estimate") or 300)
                advice = advise(listing, manifest_items, shipping=ship)
                listing["fb_total_value"] = advice["total_fb_value"]
                listing["lot_quality_score"] = advice["lot_quality_score"]
                listing["recommended_max_bid"] = advice["recommended_max_bid"]
                listing["walk_away_price"] = advice["walk_away_price"]
                bid = float(listing.get("current_bid") or 0)
                if (bid + ship) > 0 and advice["total_fb_value"] > 0:
                    listing["roi_score"] = round(
                        (advice["total_fb_value"] - bid - ship) / (bid + ship), 4
                    )
                insert_manifest_items(listing["auction_id"], manifest_items)

        t = tier(listing)
        result = send_alert(listing, t, manifest_items)
        if result.startswith("ok") or result == "shadow":
            mark_alerted(listing["auction_id"], t, {"listing": listing, "items": len(manifest_items)}, result)
            alerts_sent += 1

    # 5. Poll watchlist — snapshot bid history + alert on bid spikes
    watchlist = get_watchlist()
    snapped = 0
    for wid in watchlist:
        data = fetch_listing(wid)

        # If fetch_listing failed or returned no current_bid, fall back to
        # the already-stored row in bstock_listings for bid data
        if not data or data.get("current_bid") is None:
            from storage.db import _client
            with _client() as c:
                r = c.get(f"/bstock_listings?auction_id=eq.{wid}&select=auction_id,current_bid,bid_count,pct_of_msrp,time_remaining,price_label,title,storefront,url")
                db_rows = r.json() if r.status_code == 200 else []
            if db_rows:
                db_row = db_rows[0]
                data = {**(data or {}), **{k: v for k, v in db_row.items() if v is not None}}
                log.debug("Watchlist %s: using DB fallback for bid data (current_bid=$%s)", wid, data.get("current_bid"))

        if data and data.get("current_bid") is not None:
            upsert_listings([{**data, "auction_id": wid}])
            prev_snap = get_bid_history(wid)
            record_bid_snapshot(wid, data)
            snapped += 1

            # Bid velocity alert: fire if bid jumped >$200 or >20% since last snap
            if prev_snap:
                prev_bid = float(prev_snap[-1].get("current_bid") or 0)
                curr_bid = float(data.get("current_bid") or 0)
                bid_jump = curr_bid - prev_bid
                bid_jump_pct = (bid_jump / prev_bid) if prev_bid else 0

                if bid_jump >= 200 or bid_jump_pct >= 0.20:
                    # Fetch listing detail for rec_max_bid context
                    from storage.db import _client as _db_client
                    with _db_client() as c:
                        r = c.get(f"/bstock_listings?auction_id=eq.{wid}&select=*")
                        full_listing = r.json()[0] if r.status_code == 200 and r.json() else data

                    rec_max = full_listing.get("recommended_max_bid") or 0
                    headroom = rec_max - curr_bid if rec_max else None

                    spike_listing = {
                        **full_listing,
                        "title": f"⚡ BID SPIKE: {data.get('title',wid)}",
                        "current_bid": curr_bid,
                        "time_remaining": data.get("time_remaining"),
                        "url": data.get("url"),
                    }
                    spike_summary = {
                        **spike_listing,
                        "title": spike_listing["title"],
                        "msrp": full_listing.get("msrp"),
                        "current_bid": curr_bid,
                        "pct_of_msrp": full_listing.get("pct_of_msrp"),
                        "per_unit": full_listing.get("per_unit"),
                        "time_remaining": data.get("time_remaining"),
                        "url": data.get("url"),
                        "image_url": full_listing.get("image_url"),
                        "location": full_listing.get("location"),
                        "bid_jump": bid_jump,
                        "bid_jump_pct": round(bid_jump_pct * 100, 1),
                        "rec_max_bid": rec_max,
                        "headroom": headroom,
                    }
                    send_alert({"auction_id": wid, **spike_summary, "summary": spike_summary}, "high_priority")
                    log.warning(
                        "BID SPIKE %s: +$%.0f (+%.0f%%) → $%.0f | headroom: $%.0f",
                        wid, bid_jump, bid_jump_pct * 100, curr_bid, headroom or 0,
                    )
        else:
            log.warning("Watchlist fetch failed for %s", wid)

    log.info("=== Run complete: %d scraped, %d new, %d alerted, %d watchlist snapped ===",
             len(listings), len(new_ids), alerts_sent, snapped)
    return {
        "scraped": len(listings),
        "new": len(new_ids),
        "qualifying": len(qualifying),
        "alerts_sent": alerts_sent,
        "watchlist_snapped": snapped,
    }
