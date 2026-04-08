"""Deal scoring rules."""
from __future__ import annotations

from typing import Any

MIN_MSRP = 2000
HIGH_PRIORITY_PCT = 5.0
HIGH_PRIORITY_MSRP = 10_000


def qualifies_for_alert(listing: dict[str, Any]) -> bool:
    return (
        listing.get("price_label") == "Great Price"
        and listing.get("listing_type") == "Auction"
        and (listing.get("msrp") or 0) >= MIN_MSRP
    )


def tier(listing: dict[str, Any]) -> str:
    if (
        (listing.get("pct_of_msrp") or 100) < HIGH_PRIORITY_PCT
        and (listing.get("msrp") or 0) >= HIGH_PRIORITY_MSRP
    ):
        return "high_priority"
    return "normal"
