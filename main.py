"""FastAPI entry point. n8n cron hits POST /run every 15 min."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException

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
    add_to_watchlist,
    get_bid_history,
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
                "order": "roi_score.desc.nullslast",
                "select": "auction_id,title,storefront,location,msrp,current_bid,pct_of_msrp,price_label,time_remaining,has_manifest,shipping_estimate,fb_total_value,roi_score,unit_count,url",
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

    # 4. Proactively fetch manifests for reno-relevant new listings (don't wait for alert)
    enrich_on = os.getenv("ENRICH_MANIFESTS", "true").lower() == "true"
    if enrich_on:
        from storage.db import _client as _db_client
        with _db_client() as c:
            r = c.get(
                "/bstock_listings",
                params={
                    "reno_relevant": "eq.true",
                    "has_manifest": "eq.false",
                    "select": "auction_id,url,title,storefront,current_bid,shipping_estimate,msrp",
                },
            )
            reno_no_manifest = r.json() if r.status_code == 200 else []

        for reno_listing in reno_no_manifest:
            aid = reno_listing["auction_id"]
            detail = fetch_listing(aid)
            if detail and detail.get("manifest_doc_url"):
                raw_items = fetch_and_parse(detail["manifest_doc_url"])
                if raw_items:
                    # Full quality pipeline: tavily enrichment → quality assessment → bid advice
                    enriched = enrich_manifest(raw_items)
                    assessed = assess_items(enriched)
                    ship = float(reno_listing.get("shipping_estimate") or 300)
                    advice = advise(reno_listing, assessed, shipping=ship)

                    insert_manifest_items(aid, assessed)

                    bid = float(reno_listing.get("current_bid") or 0)
                    roi = round((advice["total_fb_value"] - bid - ship) / (bid + ship), 4) \
                          if (bid + ship) > 0 and advice["total_fb_value"] > 0 else None

                    with _db_client() as c:
                        c.patch(
                            f"/bstock_listings?auction_id=eq.{aid}",
                            json={
                                "has_manifest": True,
                                "manifest_doc_url": detail["manifest_doc_url"],
                                "fb_total_value": advice["total_fb_value"],
                                "roi_score": roi,
                                "lot_quality_score": advice["lot_quality_score"],
                                "recommended_max_bid": advice["recommended_max_bid"],
                                "walk_away_price": advice["walk_away_price"],
                                "top_items": advice["top_items"],
                            },
                        )
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

    # 5. Poll watchlist — snapshot bid history for watched listings
    watchlist = get_watchlist()
    snapped = 0
    for wid in watchlist:
        data = fetch_listing(wid)
        if data:
            # Upsert into listings so current state is always fresh
            upsert_listings([{**data, "auction_id": wid}])
            record_bid_snapshot(wid, data)
            snapped += 1
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
