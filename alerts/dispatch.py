"""Send alerts to n8n webhook. n8n handles Slack + email fan-out."""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)


def send_alert(listing: dict[str, Any], tier: str, manifest_items: list[dict[str, Any]] | None = None) -> str:
    webhook_url = os.getenv("N8N_ALERT_WEBHOOK")
    if not webhook_url:
        log.warning("N8N_ALERT_WEBHOOK not set — skipping alert for %s", listing.get("auction_id"))
        return "no_webhook"

    if os.getenv("SHADOW_MODE", "false").lower() == "true":
        log.info("[SHADOW] would alert %s (%s): %s", listing.get("auction_id"), tier, listing.get("title"))
        return "shadow"

    # Enriched photo thumbnails if any
    thumbs = []
    if manifest_items:
        for it in manifest_items:
            if it.get("real_image_url"):
                thumbs.append({
                    "model": it.get("model") or it.get("item_num"),
                    "brand": it.get("brand"),
                    "image": it["real_image_url"],
                    "real_price": it.get("real_price"),
                    "bstock_retail": it.get("unit_retail"),
                    "source": it.get("real_source_domain"),
                    "link": it.get("real_source_url"),
                })

    payload = {
        "tier": tier,
        "listing": listing,
        "manifest_thumbnails": thumbs[:10],
        "summary": {
            "title": listing.get("title"),
            "msrp": listing.get("msrp"),
            "current_bid": listing.get("current_bid"),
            "pct_of_msrp": listing.get("pct_of_msrp"),
            "per_unit": listing.get("per_unit"),
            "time_remaining": listing.get("time_remaining"),
            "url": listing.get("url"),
            "image_url": listing.get("image_url"),
            "location": listing.get("location"),
        },
    }

    try:
        r = httpx.post(webhook_url, json=payload, timeout=15)
        r.raise_for_status()
        return f"ok:{r.status_code}"
    except Exception as exc:
        log.error("Alert dispatch failed for %s: %s", listing.get("auction_id"), exc)
        return f"error:{exc}"
