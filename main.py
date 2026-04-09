"""FastAPI entry point. n8n cron hits POST /run every 15 min."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from alerts.dispatch import send_alert
from enrichment.tavily_lookup import enrich_manifest
from scoring import qualifies_for_alert, tier
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

    # 2. Upsert + identify new
    new_ids = upsert_listings(listings) if listings else set()

    # 3. Find qualifying unalerted
    qualifying = get_unalerted_qualifying()
    log.info("%d listings qualify for alert", len(qualifying))

    alerts_sent = 0
    enrich_on = os.getenv("ENRICH_MANIFESTS", "true").lower() == "true"

    for listing in qualifying:
        manifest_items: list[dict[str, Any]] = []
        if enrich_on and listing.get("manifest_doc_url"):
            raw_items = fetch_and_parse(listing["manifest_doc_url"])
            if raw_items:
                manifest_items = enrich_manifest(raw_items)
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
