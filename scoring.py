"""Deal scoring rules."""
from __future__ import annotations

from typing import Any

MIN_MSRP = 2000

# Home renovation keywords — items likely to flip fast on Facebook Marketplace
RENO_KEYWORDS = [
    "bathtub", "tub", "faucet", "faucets", "shower", "toilet", "vanity",
    "sink", "plumbing", "water heater", "tankless", "valve", "trim",
    "tile", "flooring", "floor", "cabinet", "cabinets", "door", "hardware",
    "lighting", "light fixture", "chandelier", "ceiling fan",
    "paint", "stain", "caulk", "grout", "adhesive",
    "kohler", "moen", "delta", "american standard", "grohe", "hansgrohe",
    "onewest", "one west", "winston", "weston", "ferguson", "signature hardware",
]
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
    # Never alert on lots without a real manifest — we can't verify what we're buying
    if not listing.get("manifest_doc_url"):
        return False

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


def is_reno_relevant(listing: dict[str, Any]) -> bool:
    haystack = " ".join([
        listing.get("title") or "",
        listing.get("storefront") or "",
        listing.get("condition") or "",
    ]).lower()
    return any(kw in haystack for kw in RENO_KEYWORDS)


def has_manifest(listing: dict[str, Any]) -> bool:
    return bool(listing.get("manifest_doc_url"))


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
