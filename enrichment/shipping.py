"""LTL freight cost estimator for B-Stock pallets.

Shipping on B-Stock is LTL freight quoted after bidding. This module
produces a rough estimate based on:
  - Origin city/state (from listing.location)
  - Buyer ZIP (BUYER_ZIP env var)
  - Pallet count (unit_count / ~50 units per pallet, min 1)

Rate table based on typical B-Stock LTL freight (single pallet, no liftgate):
  Same state:    $125–175
  Adjacent:      $175–250
  2–3 zones:     $250–375
  Cross-country: $375–550
  + $75 liftgate if residential delivery
"""
from __future__ import annotations

import os
import re
from typing import Any

# ZIP prefix → rough US region (1-4)
# Region 1 = West, 2 = Mountain/SW, 3 = Midwest/South, 4 = East
_ZIP_REGION: dict[str, int] = {}
for prefix, region in [
    ("9", 1), ("8", 2), ("7", 3), ("6", 3), ("5", 3),
    ("4", 3), ("3", 3), ("2", 4), ("1", 4), ("0", 4),
]:
    _ZIP_REGION[prefix] = region

# State → region
_STATE_REGION: dict[str, int] = {
    "WA": 1, "OR": 1, "CA": 1, "AK": 1, "HI": 1,
    "NV": 2, "AZ": 2, "ID": 2, "MT": 2, "WY": 2, "UT": 2, "CO": 2, "NM": 2,
    "TX": 3, "OK": 3, "KS": 3, "NE": 3, "SD": 3, "ND": 3, "MN": 3,
    "IA": 3, "MO": 3, "WI": 3, "IL": 3, "MI": 3, "IN": 3, "OH": 3,
    "AR": 3, "LA": 3, "MS": 3, "AL": 3, "TN": 3, "KY": 3,
    "GA": 4, "FL": 4, "SC": 4, "NC": 4, "VA": 4, "WV": 4,
    "DC": 4, "MD": 4, "DE": 4, "NJ": 4, "PA": 4, "NY": 4,
    "CT": 4, "RI": 4, "MA": 4, "VT": 4, "NH": 4, "ME": 4,
}

# (origin_region, dest_region) → (min_cost, max_cost) per pallet
_RATE_TABLE: dict[tuple[int, int], tuple[int, int]] = {
    (1, 1): (125, 175), (1, 2): (175, 250), (1, 3): (275, 375), (1, 4): (400, 550),
    (2, 1): (175, 250), (2, 2): (125, 175), (2, 3): (200, 300), (2, 4): (350, 475),
    (3, 1): (275, 375), (3, 2): (200, 300), (3, 3): (125, 175), (3, 4): (200, 300),
    (4, 1): (400, 550), (4, 2): (350, 475), (4, 3): (200, 300), (4, 4): (125, 175),
}


def _region_from_location(location: str) -> int | None:
    """Extract US region (1-4) from a location string like 'DeSoto, TX, United States'."""
    if not location:
        return None
    # Try state abbreviation
    m = re.search(r',\s*([A-Z]{2})\s*(?:,|$)', location)
    if m:
        return _STATE_REGION.get(m.group(1))
    return None


def _region_from_zip(zip_code: str) -> int | None:
    if not zip_code:
        return None
    return _ZIP_REGION.get(zip_code[0])


def estimate_shipping(listing: dict[str, Any]) -> float | None:
    """Return midpoint shipping estimate in dollars, or None if can't estimate."""
    buyer_zip = os.getenv("BUYER_ZIP", "")
    origin_location = listing.get("location") or ""

    origin_region = _region_from_location(origin_location)
    dest_region = _region_from_zip(buyer_zip)

    if not origin_region or not dest_region:
        # Can't estimate without both locations — return flat default
        return 300.0

    rates = _RATE_TABLE.get((origin_region, dest_region))
    if not rates:
        return 300.0

    # Estimate pallet count: ~50 units per pallet, min 1
    units = listing.get("unit_count") or 1
    pallets = max(1, round(units / 50))

    lo, hi = rates
    per_pallet = (lo + hi) / 2

    # Liftgate for residential assumed
    liftgate = 75

    return round(per_pallet * pallets + liftgate, 2)


def landed_cost(listing: dict[str, Any]) -> dict[str, Any]:
    """Return breakdown: bid + shipping = landed cost + ROI inputs."""
    bid = float(listing.get("current_bid") or 0)
    shipping = estimate_shipping(listing) or 0
    total = bid + shipping
    msrp = float(listing.get("msrp") or 0)
    fb_total = float(listing.get("fb_total_value") or 0)

    result = {
        "bid": bid,
        "shipping_estimate": shipping,
        "landed_cost": total,
        "msrp": msrp,
        "pct_of_msrp_landed": round((total / msrp * 100), 2) if msrp else None,
    }
    if fb_total:
        profit = fb_total - total
        roi = round((profit / total) * 100, 1) if total else None
        result["fb_total_value"] = fb_total
        result["estimated_profit"] = profit
        result["roi_pct"] = roi
    return result
