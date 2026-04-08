"""Supabase Postgres persistence layer."""
from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg2
from psycopg2.extras import Json, execute_values

log = logging.getLogger(__name__)


@contextmanager
def conn():
    dsn = os.environ["DB_URL"]
    c = psycopg2.connect(dsn)
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


LISTING_COLS = [
    "auction_id", "url", "title", "image_url", "manifest_doc_url", "location",
    "listing_type", "condition", "unit_count", "msrp", "current_bid",
    "pct_of_msrp", "per_unit", "time_remaining", "bid_count", "price_label",
    "storefront", "raw_json",
]


def upsert_listings(listings: list[dict[str, Any]]) -> set[str]:
    """Upsert listings. Returns set of auction_ids that are NEW (first_seen == now)."""
    if not listings:
        return set()
    new_ids: set[str] = set()
    with conn() as c, c.cursor() as cur:
        # Determine which are new
        ids = [l["auction_id"] for l in listings]
        cur.execute("select auction_id from bstock_listings where auction_id = any(%s)", (ids,))
        existing = {r[0] for r in cur.fetchall()}
        new_ids = set(ids) - existing

        rows = [
            (
                l["auction_id"], l["url"], l.get("title"), l.get("image_url"),
                l.get("manifest_doc_url"), l.get("location"), l.get("listing_type"),
                l.get("condition"), l.get("unit_count"), l.get("msrp"),
                l.get("current_bid"), l.get("pct_of_msrp"), l.get("per_unit"),
                l.get("time_remaining"), l.get("bid_count"), l.get("price_label"),
                l.get("storefront"), Json(l),
            )
            for l in listings
        ]
        execute_values(
            cur,
            f"""
            insert into bstock_listings ({", ".join(LISTING_COLS)})
            values %s
            on conflict (auction_id) do update set
              current_bid = excluded.current_bid,
              pct_of_msrp = excluded.pct_of_msrp,
              bid_count = excluded.bid_count,
              time_remaining = excluded.time_remaining,
              price_label = excluded.price_label,
              last_seen = now(),
              raw_json = excluded.raw_json
            """,
            rows,
        )
    log.info("Upserted %d listings (%d new)", len(listings), len(new_ids))
    return new_ids


MANIFEST_COLS = [
    "auction_id", "lot_id", "seller_category", "description", "qty",
    "unit_retail", "ext_retail", "item_num", "upc", "vendor", "category",
    "subcategory", "condition", "brand", "color", "model", "notes",
    "real_price", "real_image_url", "real_source_domain", "real_source_url",
    "enriched_at",
]


def insert_manifest_items(auction_id: str, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    with conn() as c, c.cursor() as cur:
        # Clear any existing items for this auction (re-ingest)
        cur.execute("delete from bstock_manifest_items where auction_id = %s", (auction_id,))
        rows = []
        for it in items:
            rows.append((
                auction_id,
                it.get("lot_id"), it.get("seller_category"), it.get("description"),
                it.get("qty"), it.get("unit_retail"), it.get("ext_retail"),
                it.get("item_num"), it.get("upc"), it.get("vendor"),
                it.get("category"), it.get("subcategory"), it.get("condition"),
                it.get("brand"), it.get("color"), it.get("model"), it.get("notes"),
                it.get("real_price"), it.get("real_image_url"),
                it.get("real_source_domain"), it.get("real_source_url"),
                "now()" if it.get("real_price") else None,
            ))
        # enriched_at handled via trigger-less approach — pass NULL or now()
        cur.executemany(
            f"""
            insert into bstock_manifest_items
              (auction_id, lot_id, seller_category, description, qty, unit_retail,
               ext_retail, item_num, upc, vendor, category, subcategory, condition,
               brand, color, model, notes, real_price, real_image_url,
               real_source_domain, real_source_url, enriched_at)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
              case when %s is not null then now() else null end)
            """,
            [(*r[:-1], r[-1]) for r in rows],
        )


def mark_alerted(auction_id: str, tier: str, payload: dict[str, Any], response: str = "") -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            "update bstock_listings set alerted = true, alerted_at = now() where auction_id = %s",
            (auction_id,),
        )
        cur.execute(
            "insert into bstock_alerts (auction_id, alert_tier, payload, webhook_response) values (%s,%s,%s,%s)",
            (auction_id, tier, Json(payload), response),
        )


def get_unalerted_qualifying(min_msrp: float = 2000) -> list[dict[str, Any]]:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            select auction_id, url, title, image_url, manifest_doc_url, location,
                   listing_type, condition, unit_count, msrp, current_bid,
                   pct_of_msrp, per_unit, time_remaining, bid_count, price_label,
                   storefront
            from bstock_listings
            where alerted = false
              and price_label = 'Great Price'
              and listing_type = 'Auction'
              and msrp >= %s
            """,
            (min_msrp,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
