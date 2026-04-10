"""Download and parse B-Stock manifest CSV files.

Manifests are served at docserv.bstock.com/v1/documents/{id} behind auth.
We authenticate via FusionAuth JWT (same as the scraper).
"""
from __future__ import annotations

import io
import logging
from typing import Any

import httpx
import pandas as pd

from .bstock import get_jwt_token, BROWSER_UA

log = logging.getLogger(__name__)

# Tolerant column mapping — B-Stock's CSV schema varies slightly per lot
COLUMN_MAP = {
    "Lot ID": "lot_id",
    "Seller Category": "seller_category",
    "Item Description": "description",
    "Qty": "qty",
    "Unit Retail": "unit_retail",
    "Ext. Retail": "ext_retail",
    "Item #": "item_num",
    "UPC": "upc",
    "Vendor": "vendor",
    "Category": "category",
    "Subcategory": "subcategory",
    "Condition": "condition",
    "Brand": "brand",
    "Color": "color",
    "Model": "model",
    "Notes/Comments": "notes",
}


def download_manifest_csv(doc_url: str) -> bytes | None:
    """Fetch a manifest CSV using FusionAuth JWT."""
    try:
        token = get_jwt_token()
    except Exception as exc:
        log.warning("Could not get JWT for manifest download: %s", exc)
        return None
    try:
        with httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Cookie": f"token={token}; access_token={token}",
                "User-Agent": BROWSER_UA,
            },
            follow_redirects=True,
            timeout=30,
        ) as client:
            r = client.get(doc_url)
            r.raise_for_status()
            return r.content
    except Exception as exc:
        log.error("Manifest download failed for %s: %s", doc_url, exc)
        return None


def parse_manifest_csv(content: bytes) -> list[dict[str, Any]]:
    """Parse raw CSV bytes into normalized line-item dicts."""
    # Reject binary content (PDFs, etc.) before pandas tries to parse them
    if content[:4] == b"%PDF":
        log.debug("Skipping PDF content (not a CSV manifest)")
        return []
    try:
        df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
    except Exception as exc:
        log.error("Manifest parse failed: %s", exc)
        return []

    # Rename known columns, drop unknowns
    rename = {k: v for k, v in COLUMN_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)

    # Coerce numeric fields
    for col in ("qty", "unit_retail", "ext_retail"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].str.replace(r"[^\d.\-]", "", regex=True), errors="coerce")

    # Keep only mapped cols
    keep = [c for c in COLUMN_MAP.values() if c in df.columns]
    df = df[keep]
    return df.to_dict(orient="records")


def fetch_and_parse(doc_url: str) -> list[dict[str, Any]]:
    raw = download_manifest_csv(doc_url)
    if not raw:
        return []
    return parse_manifest_csv(raw)
