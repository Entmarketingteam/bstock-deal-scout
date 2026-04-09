"""Fetch a single B-Stock listing by auction_id for watchlist polling."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from scraper.bstock import get_jwt_token, BROWSER_UA, _listing_from_rsc_obj

log = logging.getLogger(__name__)

DETAIL_RSC_URL = "https://bstock.com/buy/listings/details/{auction_id}?_rsc=rsc1"


def fetch_listing(auction_id: str) -> dict[str, Any] | None:
    """Fetch current state of a single listing. Returns a dict or None on failure."""
    token = get_jwt_token()
    url = DETAIL_RSC_URL.format(auction_id=auction_id)

    try:
        resp = httpx.get(
            url,
            headers={
                "User-Agent": BROWSER_UA,
                "Cookie": f"token={token}; access_token={token}",
                "Authorization": f"Bearer {token}",
                "RSC": "1",
                "Accept-Encoding": "gzip",
            },
            timeout=30,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("Detail fetch failed for %s: %s", auction_id, resp.status_code)
            return None
    except Exception as exc:
        log.error("Detail fetch error for %s: %s", auction_id, exc)
        return None

    body = resp.text

    # Strategy 1: look for the listing object by id field matching auction_id
    # RSC embeds JSON objects — find one containing our auction_id
    pattern = re.compile(
        r'\{[^{}]*"id"\s*:\s*"' + re.escape(auction_id) + r'"[^{}]*\}',
        re.DOTALL,
    )
    m = pattern.search(body)
    if m:
        try:
            obj = json.loads(m.group(0))
            listing = _listing_from_rsc_obj(obj)
            if listing:
                return listing.to_dict()
        except (json.JSONDecodeError, Exception):
            pass

    # Strategy 2: scan all JSON objects in the RSC stream for one with
    # numberOfBids or percentMsrp that also has our id
    for chunk in re.finditer(r'\{(?:[^{}]|\{[^{}]*\})*\}', body):
        try:
            obj = json.loads(chunk.group(0))
            if (obj.get("id") == auction_id or obj.get("listingId") == auction_id):
                if "percentMsrp" in obj or "numberOfBids" in obj or "retailPrice" in obj:
                    listing = _listing_from_rsc_obj(obj)
                    if listing:
                        return listing.to_dict()
        except (json.JSONDecodeError, Exception):
            continue

    # Strategy 3: look for key bid/price fields anywhere in body and extract surrounding object
    # Search for numberOfBids and work outward
    nb_match = re.search(r'"numberOfBids"\s*:\s*(\d+)', body)
    if nb_match:
        # Try to find surrounding object
        idx = nb_match.start()
        # Walk back to find {
        depth = 0
        start = idx
        for i in range(idx, -1, -1):
            if body[i] == '}':
                depth += 1
            elif body[i] == '{':
                if depth == 0:
                    start = i
                    break
                depth -= 1
        # Walk forward to find matching }
        depth = 0
        end = idx
        for i in range(start, len(body)):
            if body[i] == '{':
                depth += 1
            elif body[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        try:
            obj = json.loads(body[start:end + 1])
            listing = _listing_from_rsc_obj(obj)
            if listing:
                d = listing.to_dict()
                d["auction_id"] = auction_id  # override since id may not be present
                return d
        except (json.JSONDecodeError, Exception):
            pass

    log.warning("Could not parse listing data for %s from detail RSC", auction_id)
    return None
