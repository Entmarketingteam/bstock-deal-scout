"""Supabase persistence via PostgREST (REST API).

Uses HTTP instead of psycopg2 because Railway can't reach Supabase via
direct PostgreSQL (IPv6-only) and the transaction pooler isn't reachable
either. PostgREST works over IPv4 with the service role key.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _client() -> httpx.Client:
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    return httpx.Client(
        base_url=f"{base}/rest/v1",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        timeout=30,
    )


def upsert_listings(listings: list[dict[str, Any]]) -> set[str]:
    """Upsert listings via PostgREST. Returns set of NEW auction_ids."""
    if not listings:
        return set()
    ids = [l["auction_id"] for l in listings]
    new_ids: set[str] = set()
    with _client() as c:
        # Find existing
        ids_q = ",".join(f'"{i}"' for i in ids)
        r = c.get(f"/bstock_listings?select=auction_id&auction_id=in.({ids_q})")
        r.raise_for_status()
        existing = {row["auction_id"] for row in r.json()}
        new_ids = set(ids) - existing

        # Build payload (only writable columns — exclude generated `deal_score`)
        payload = []
        for l in listings:
            payload.append({
                "auction_id": l["auction_id"],
                "url": l.get("url"),
                "title": l.get("title"),
                "image_url": l.get("image_url"),
                "manifest_doc_url": l.get("manifest_doc_url"),
                "location": l.get("location"),
                "listing_type": l.get("listing_type"),
                "condition": l.get("condition"),
                "unit_count": l.get("unit_count"),
                "msrp": l.get("msrp"),
                "current_bid": l.get("current_bid"),
                "pct_of_msrp": l.get("pct_of_msrp"),
                "per_unit": l.get("per_unit"),
                "time_remaining": l.get("time_remaining"),
                "bid_count": l.get("bid_count"),
                "price_label": l.get("price_label"),
                "storefront": l.get("storefront"),
                "raw_json": l,
            })

        r = c.post(
            "/bstock_listings",
            params={"on_conflict": "auction_id"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
        )
        if r.status_code >= 400:
            log.error("Upsert failed: %s %s", r.status_code, r.text[:500])
            r.raise_for_status()

    log.info("Upserted %d listings (%d new)", len(listings), len(new_ids))
    return new_ids


def insert_manifest_items(auction_id: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with _client() as c:
        # Clear existing
        r = c.delete(f"/bstock_manifest_items?auction_id=eq.{auction_id}")
        if r.status_code >= 400 and r.status_code != 404:
            log.warning("Manifest clear failed: %s", r.text[:200])

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for it in items:
            rows.append({
                "auction_id": auction_id,
                "lot_id": it.get("lot_id"),
                "seller_category": it.get("seller_category"),
                "description": it.get("description"),
                "qty": it.get("qty"),
                "unit_retail": it.get("unit_retail"),
                "ext_retail": it.get("ext_retail"),
                "item_num": it.get("item_num"),
                "upc": it.get("upc"),
                "vendor": it.get("vendor"),
                "category": it.get("category"),
                "subcategory": it.get("subcategory"),
                "condition": it.get("condition"),
                "brand": it.get("brand"),
                "color": it.get("color"),
                "model": it.get("model"),
                "notes": it.get("notes"),
                "real_price": it.get("real_price"),
                "real_image_url": it.get("real_image_url"),
                "real_source_domain": it.get("real_source_domain"),
                "real_source_url": it.get("real_source_url"),
                "enriched_at": now if it.get("real_price") else None,
            })
        r = c.post("/bstock_manifest_items", json=rows)
        if r.status_code >= 400:
            log.error("Manifest insert failed: %s %s", r.status_code, r.text[:500])
            r.raise_for_status()


def mark_alerted(auction_id: str, tier: str, payload: dict[str, Any], response: str = "") -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _client() as c:
        c.patch(
            f"/bstock_listings?auction_id=eq.{auction_id}",
            json={"alerted": True, "alerted_at": now},
        )
        c.post(
            "/bstock_alerts",
            json={
                "auction_id": auction_id,
                "alert_tier": tier,
                "payload": payload,
                "webhook_response": response,
            },
        )


def get_unalerted_qualifying(min_msrp: float = 2000) -> list[dict[str, Any]]:
    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "select": "auction_id,url,title,image_url,manifest_doc_url,location,listing_type,condition,unit_count,msrp,current_bid,pct_of_msrp,per_unit,time_remaining,bid_count,price_label,storefront",
                "alerted": "eq.false",
                "price_label": "eq.Great Price",
                "listing_type": "eq.Auction",
                "msrp": f"gte.{min_msrp}",
            },
        )
        r.raise_for_status()
        return r.json()
