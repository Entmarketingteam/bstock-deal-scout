"""Deal scoring rules."""
from __future__ import annotations

from typing import Any

MIN_MSRP = 2000
HIGH_PRIORITY_PCT = 5.0
HIGH_PRIORITY_MSRP = 10_000

# Brands/storefronts to watch closely — alert on "Good Price" too, lower MSRP floor
TARGET_BRANDS = [
    "kohler",
    "winston",
    "weston",
]
TARGET_BRAND_MIN_MSRP = 500
TARGET_BRAND_LABELS = {"Great Price", "Good Price"}


def _is_target_brand(listing: dict[str, Any]) -> bool:
    haystack = " ".join([
        listing.get("title") or "",
        listing.get("storefront") or "",
    ]).lower()
    return any(brand in haystack for brand in TARGET_BRANDS)


def qualifies_for_alert(listing: dict[str, Any]) -> bool:
    listing_type = listing.get("listing_type")
    msrp = listing.get("msrp") or 0
    price_label = listing.get("price_label")

    if listing_type != "Auction":
        return False

    # Target brands: alert on Great Price or Good Price, lower MSRP floor
    if _is_target_brand(listing):
        return price_label in TARGET_BRAND_LABELS and msrp >= TARGET_BRAND_MIN_MSRP

    # Everything else: Great Price only, $2k+ MSRP
    return price_label == "Great Price" and msrp >= MIN_MSRP


def tier(listing: dict[str, Any]) -> str:
    # Target brand hits always get flagged as high priority
    if _is_target_brand(listing):
        return "high_priority"

    if (
        (listing.get("pct_of_msrp") or 100) < HIGH_PRIORITY_PCT
        and (listing.get("msrp") or 0) >= HIGH_PRIORITY_MSRP
    ):
        return "high_priority"
    return "normal"
