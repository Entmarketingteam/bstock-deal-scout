"""FastAPI entry point. n8n cron hits POST /run every 15 min."""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from alerts.dispatch import send_alert
from enrichment.bid_advisor import advise
from enrichment.quality_assessor import assess_items
from enrichment.resale_lookup import enrich_manifest_with_resale
from enrichment.shipping import estimate_shipping, landed_cost
from enrichment.tavily_lookup import enrich_manifest
from scoring import has_manifest, is_reno_relevant, qualifies_for_alert, tier
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


def _synthetic_items(listing: dict) -> list[dict]:
    """
    Create synthetic manifest items from listing title + MSRP + unit count
    when no CSV manifest exists (e.g. Kohler branded pallets).

    Parses titles like:
      "1 Pallet of Kitchen Faucets by Kohler, 12 Units, New Condition, $X MSRP"
      "3 Pallets of Bluetooth Shower Heads by Kohler, 165 Units"
    """
    import re as _re
    title = listing.get("title") or ""
    msrp = float(listing.get("msrp") or 0)
    unit_count = int(listing.get("unit_count") or 0)

    # Extract brand from "by <Brand>"
    brand_m = _re.search(r'\bby\s+([A-Za-z][A-Za-z &]+?)(?:\s*,|\s*$)', title, _re.IGNORECASE)
    brand = brand_m.group(1).strip() if brand_m else ""

    # Extract item description from "Pallet(s) of <Description>"
    desc_m = _re.search(r'pallets?\s+of\s+(.+?)(?:\s+by\s|\s*,)', title, _re.IGNORECASE)
    description = desc_m.group(1).strip() if desc_m else title[:60]

    # Determine condition
    condition = "New" if "new" in title.lower() else "Unknown"

    if not (brand or description) or unit_count == 0 or msrp == 0:
        return []

    unit_retail = round(msrp / unit_count, 2) if unit_count > 0 else 0

    return [{
        "brand": brand,
        "description": description,
        "qty": unit_count,
        "unit_retail": unit_retail,
        "ext_retail": msrp,
        "condition": condition,
        "lot_id": listing.get("auction_id"),
    }]


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


@app.get("/bundles")
def bundle_deals(x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """
    Group current reno listings into complementary bundles for contractor/STR pitches.

    Returns curated package groupings with per-bathroom cost calculations.
    Designed to support an AI lookbook for BowTiedBroke-style STR developers.
    """
    _require_auth(x_trigger_secret)
    from storage.db import _client

    # Category detection patterns
    CATEGORIES = {
        "shower": ["shower", "bluetooth shower", "shower head", "shower trim"],
        "bath_fixtures": ["bath spout", "valve trim", "faucet", "tub", "bathtub", "bath faucet"],
        "accessories": ["towel", "soap dispenser", "hotelier", "bath accessory", "robe hook", "toilet paper"],
        "kitchen": ["kitchen faucet", "kitchen"],
        "hardware": ["door", "cabinet", "knob", "pull", "handle", "lock", "deadbolt"],
        "lighting": ["light", "chandelier", "ceiling fan", "lamp"],
    }

    def _categorize(listing: dict) -> list[str]:
        text = (listing.get("title") or "").lower()
        matched = []
        for cat, keywords in CATEGORIES.items():
            if any(kw in text for kw in keywords):
                matched.append(cat)
        return matched or ["other"]

    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "reno_relevant": "eq.true",
                "order": "roi_score.desc.nullslast",
                "select": "auction_id,title,storefront,location,msrp,current_bid,roi_score,lot_quality_score,fb_total_value,recommended_max_bid,walk_away_price,unit_count,shipping_estimate,url,time_remaining",
            },
        )
        r.raise_for_status()
        listings = r.json()

    # Classify + enrich each listing
    classified = []
    for row in listings:
        cats = _categorize(row)
        bid = float(row.get("current_bid") or 0)
        ship = float(row.get("shipping_estimate") or 300)
        units = int(row.get("unit_count") or 1)
        classified.append({
            **row,
            "categories": cats,
            "landed_cost": bid + ship,
            "per_unit_cost": round((bid + ship) / units, 2) if units else 0,
            "per_unit_msrp": round(float(row.get("msrp") or 0) / units, 2) if units else 0,
        })

    # Bundle: "Complete Bath Suite" — shower + fixtures + accessories
    bath_bundle = [l for l in classified if any(c in l["categories"] for c in ["shower", "bath_fixtures", "accessories"])]
    kitchen_bundle = [l for l in classified if "kitchen" in l["categories"]]
    hardware_bundle = [l for l in classified if "hardware" in l["categories"]]

    def _bundle_summary(bundle: list[dict], name: str, bathrooms_per_unit: float = 1.0) -> dict:
        if not bundle:
            return {"name": name, "lots": [], "total_bid": 0, "total_units": 0}
        total_bid = sum(float(l.get("current_bid") or 0) for l in bundle)
        total_landed = sum(l["landed_cost"] for l in bundle)
        total_msrp = sum(float(l.get("msrp") or 0) for l in bundle)
        total_fb = sum(float(l.get("fb_total_value") or 0) for l in bundle)
        total_units = sum(int(l.get("unit_count") or 0) for l in bundle)
        # Estimate bathrooms: conservative based on accessory lot
        bathrooms = max(1, int(min(int(l.get("unit_count") or 0) for l in bundle) * bathrooms_per_unit))
        return {
            "name": name,
            "lots": [{"id": l["auction_id"], "title": l["title"], "bid": l.get("current_bid"), "units": l.get("unit_count"), "per_unit_cost": l["per_unit_cost"]} for l in bundle],
            "total_bid": total_bid,
            "total_landed": round(total_landed, 2),
            "total_msrp": total_msrp,
            "total_fb_value": total_fb,
            "total_units": total_units,
            "estimated_bathrooms": bathrooms,
            "cost_per_bathroom": round(total_landed / bathrooms, 2) if bathrooms else None,
            "msrp_per_bathroom": round(total_msrp / bathrooms, 2) if bathrooms else None,
            "bundle_roi": round((total_fb - total_landed) / total_landed, 4) if total_landed and total_fb else None,
        }

    bundles = [
        _bundle_summary(bath_bundle, "Complete Bath Suite (Shower + Fixtures + Accessories)"),
        _bundle_summary(kitchen_bundle, "Kitchen Package"),
        _bundle_summary(hardware_bundle, "Door & Cabinet Hardware Package"),
    ]

    # All premium hardware (bath + kitchen + hardware, skip outdoor/garden)
    premium = [l for l in classified if l["categories"] != ["other"]]
    bundles.append(_bundle_summary(premium, "Full Hardware Package (All Premium Lots)", bathrooms_per_unit=0.5))

    return {
        "total_reno_listings": len(listings),
        "bundles": [b for b in bundles if b["lots"]],
        "all_classified": [
            {k: v for k, v in l.items() if k != "raw_json"}
            for l in classified
        ],
    }


@app.get("/reno")
def reno_deals(x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """All reno-relevant listings with landed cost + ROI breakdown."""
    _require_auth(x_trigger_secret)
    from storage.db import _client
    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "reno_relevant": "eq.true",
                "order": "roi_score.desc.nullslast",
                "select": "auction_id,title,storefront,location,msrp,current_bid,pct_of_msrp,price_label,time_remaining,has_manifest,shipping_estimate,fb_total_value,roi_score,unit_count,url,lot_quality_score,recommended_max_bid,walk_away_price,top_items",
            },
        )
        r.raise_for_status()
        rows = r.json()

    deals = []
    for row in rows:
        lc = landed_cost(row)
        deals.append({**row, **lc})

    return {"count": len(deals), "deals": deals}


@app.get("/landed-cost/{auction_id}")
def get_landed_cost(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    from storage.db import _client
    with _client() as c:
        r = c.get(f"/bstock_listings?auction_id=eq.{auction_id}&select=*")
        r.raise_for_status()
        rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="listing not found")
    return landed_cost(rows[0])


@app.get("/advise/{auction_id}")
def advise_lot(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    """Full bid recommendation for a specific lot — runs live quality assessment."""
    _require_auth(x_trigger_secret)
    from storage.db import _client
    import os

    with _client() as c:
        r = c.get(f"/bstock_listings?auction_id=eq.{auction_id}&select=*")
        r.raise_for_status()
        rows = r.json()
    if not rows:
        raise HTTPException(status_code=404, detail="listing not found")
    listing = rows[0]

    # Get existing manifest items
    with _client() as c:
        r = c.get(f"/bstock_manifest_items?auction_id=eq.{auction_id}&order=unit_retail.desc")
        items = r.json() if r.status_code == 200 else []

    # If no items, try fetching manifest now
    if not items:
        detail = fetch_listing(auction_id)
        if detail and detail.get("manifest_doc_url"):
            from scraper.manifest import fetch_and_parse
            raw = fetch_and_parse(detail["manifest_doc_url"])
            if raw:
                items = enrich_manifest(raw)

    if not items:
        raise HTTPException(status_code=404, detail="no manifest items found")

    # Run full quality assessment + bid advice
    assessed = assess_items(items)
    ship = estimate_shipping(listing)
    result = advise(listing, assessed, shipping=ship or 300)

    return {
        "auction_id": auction_id,
        "title": listing.get("title"),
        "current_bid": listing.get("current_bid"),
        "shipping_estimate": ship,
        **result,
    }


@app.get("/history/{auction_id}")
def history(auction_id: str, x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    snapshots = get_bid_history(auction_id)
    return {"auction_id": auction_id, "snapshots": snapshots, "count": len(snapshots)}


@app.get("/lookbook-report", response_class=HTMLResponse)
def lookbook_report(x_trigger_secret: str | None = Header(default=None)) -> HTMLResponse:
    """
    Generate an HTML contractor lookbook for current premium hardware lots.
    Targeted at STR developers / builders (BowTiedBroke-style cabin builds).
    Returns self-contained HTML — save as PDF or email directly.
    """
    _require_auth(x_trigger_secret)
    from storage.db import _client

    with _client() as c:
        r = c.get(
            "/bstock_listings",
            params={
                "reno_relevant": "eq.true",
                "order": "msrp.desc",
                "select": "auction_id,title,msrp,current_bid,unit_count,shipping_estimate,fb_total_value,roi_score,lot_quality_score,recommended_max_bid,url,location,time_remaining",
            },
        )
        r.raise_for_status()
        listings = [l for l in r.json() if "outdoor" not in (l.get("title") or "").lower() and "garden" not in (l.get("title") or "").lower()]

    # Build per-lot data
    lot_rows = []
    for l in listings:
        bid = float(l.get("current_bid") or 0)
        ship = float(l.get("shipping_estimate") or 300)
        units = int(l.get("unit_count") or 1)
        msrp = float(l.get("msrp") or 0)
        landed = bid + ship
        lot_rows.append({
            **l,
            "landed": landed,
            "per_unit_landed": round(landed / units, 2) if units else 0,
            "per_unit_msrp": round(msrp / units, 2) if units else 0,
            "discount_pct": round((1 - landed / msrp) * 100, 0) if msrp else 0,
        })

    total_msrp = sum(float(l.get("msrp") or 0) for l in lot_rows)
    total_bid = sum(float(l.get("current_bid") or 0) for l in lot_rows)
    total_units = sum(int(l.get("unit_count") or 0) for l in lot_rows)

    def _row(l: dict) -> str:
        roi_str = f"{l.get('roi_score') or 0:.1f}x" if l.get("roi_score") else "—"
        rec_bid = f"${l.get('recommended_max_bid') or 0:,.0f}" if l.get("recommended_max_bid") else "—"
        return f"""
        <tr>
          <td><a href="{l.get('url','#')}" target="_blank">{l.get('title','')[:60]}</a></td>
          <td class="num">${l.get('msrp') or 0:,.0f}</td>
          <td class="num">{l.get('unit_count') or '—'}</td>
          <td class="num">${l.get('current_bid') or 0:,.0f}</td>
          <td class="num">${l['per_unit_msrp']:,.2f}</td>
          <td class="num">${l['per_unit_landed']:,.2f}</td>
          <td class="num highlight">{l['discount_pct']:.0f}% off</td>
          <td class="num">{roi_str}</td>
          <td class="num">{rec_bid}</td>
          <td class="small">{l.get('time_remaining','—')}</td>
        </tr>"""

    rows_html = "".join(_row(l) for l in lot_rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>B-Stock Premium Hardware — Contractor Lookbook</title>
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; margin: 0; padding: 40px; color: #1a1a1a; max-width: 1100px; }}
  h1 {{ font-size: 28px; font-weight: 700; letter-spacing: -0.5px; margin-bottom: 4px; }}
  .subtitle {{ color: #555; font-size: 14px; margin-bottom: 32px; }}
  .stat-row {{ display: flex; gap: 24px; margin-bottom: 40px; }}
  .stat {{ background: #f5f5f5; border-radius: 8px; padding: 16px 24px; flex: 1; }}
  .stat .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #888; }}
  .stat .value {{ font-size: 28px; font-weight: 700; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ background: #1a1a1a; color: white; padding: 10px 12px; text-align: left; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #eee; vertical-align: top; }}
  tr:hover td {{ background: #fafafa; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .highlight {{ color: #16a34a; font-weight: 700; }}
  .small {{ font-size: 11px; color: #888; }}
  a {{ color: #1a1a1a; }}
  .pitch {{ background: #fffbeb; border: 1px solid #fbbf24; border-radius: 8px; padding: 20px 24px; margin: 32px 0; }}
  .pitch h3 {{ margin: 0 0 8px; font-size: 15px; }}
  .pitch p {{ margin: 0; font-size: 13px; color: #555; line-height: 1.6; }}
  footer {{ margin-top: 40px; font-size: 11px; color: #aaa; }}
</style>
</head>
<body>
<h1>Kohler + Signature Hardware — Contractor Lot Package</h1>
<div class="subtitle">B-Stock Premium Liquidation · Sourced for STR/Cabin Developers · All New Condition</div>

<div class="stat-row">
  <div class="stat">
    <div class="label">Total MSRP Across Lots</div>
    <div class="value">${total_msrp:,.0f}</div>
  </div>
  <div class="stat">
    <div class="label">Current Total Bid</div>
    <div class="value">${total_bid:,.0f}</div>
  </div>
  <div class="stat">
    <div class="label">Total Units</div>
    <div class="value">{total_units:,}</div>
  </div>
  <div class="stat">
    <div class="label">Avg Discount vs MSRP</div>
    <div class="value">{round((1 - total_bid/total_msrp)*100) if total_msrp else 0:.0f}%+</div>
  </div>
</div>

<div class="pitch">
  <h3>Who this is for</h3>
  <p>
    Building 10–50 unit STR properties (cabins, mountain retreats, lake houses)?
    These Kohler lots let you spec and install premium hardware — same brand used in $500/night properties —
    at &lt;5% of retail. Buy the bath trim, shower heads, and accessories together and you can
    outfit 50+ bathrooms with matching Kohler fixtures for roughly <strong>${round(total_bid / max(1, total_units // 3)):,}/bathroom</strong> all-in landed.
    Signature Hardware adds door + cabinet packages to complete the interior package.
  </p>
</div>

<table>
  <thead>
    <tr>
      <th>Lot</th>
      <th class="num">MSRP</th>
      <th class="num">Units</th>
      <th class="num">Bid</th>
      <th class="num">$/Unit MSRP</th>
      <th class="num">$/Unit Landed</th>
      <th class="num">Savings</th>
      <th class="num">Resale ROI</th>
      <th class="num">Max Bid</th>
      <th>Time Left</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>

<footer>
  Generated by B-Stock Deal Scout · Data from live B-Stock auctions · Bid prices update every 15min
</footer>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


@app.post("/run")
def run(x_trigger_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _require_auth(x_trigger_secret)
    log.info("=== Deal scout run start ===")

    # 1. Scrape
    listings = [l.to_dict() for l in scrape_listings()]
    log.info("Scraped %d listings", len(listings))

    # 2. Annotate listings with shipping estimate + reno relevance
    for l in listings:
        l["shipping_estimate"] = estimate_shipping(l)
        l["reno_relevant"] = is_reno_relevant(l)

    # 3. Upsert + identify new
    new_ids = upsert_listings(listings) if listings else set()

    # 4. Proactively fetch manifests for reno-relevant new listings (don't wait for alert)
    enrich_on = os.getenv("ENRICH_MANIFESTS", "true").lower() == "true"
    if enrich_on:
        from storage.db import _client as _db_client
        with _db_client() as c:
            r = c.get(
                "/bstock_listings",
                params={
                    "reno_relevant": "eq.true",
                    "has_manifest": "eq.false",
                    "select": "auction_id,url,title,storefront,current_bid,shipping_estimate,msrp,unit_count,condition",
                },
            )
            reno_no_manifest = r.json() if r.status_code == 200 else []

        for reno_listing in reno_no_manifest:
            aid = reno_listing["auction_id"]
            detail = fetch_listing(aid)

            raw_items = []
            if detail and detail.get("manifest_doc_url"):
                raw_items = fetch_and_parse(detail["manifest_doc_url"])

            # Fallback: synthesize manifest items from listing title + unit count
            # Used when no CSV manifest exists (e.g. Kohler brand lots)
            if not raw_items:
                raw_items = _synthetic_items(reno_listing)

            if raw_items:
                enriched = enrich_manifest(raw_items)
                assessed = assess_items(enriched)
                ship = float(reno_listing.get("shipping_estimate") or 300)
                advice = advise(reno_listing, assessed, shipping=ship)

                insert_manifest_items(aid, assessed)

                bid = float(reno_listing.get("current_bid") or 0)
                roi = round((advice["total_fb_value"] - bid - ship) / (bid + ship), 4) \
                      if (bid + ship) > 0 and advice["total_fb_value"] > 0 else None

                manifest_url = (detail or {}).get("manifest_doc_url")
                with _db_client() as c:
                    patch_payload = {
                        "has_manifest": bool(manifest_url),
                        "fb_total_value": advice["total_fb_value"],
                        "roi_score": roi,
                        "lot_quality_score": advice["lot_quality_score"],
                        "recommended_max_bid": advice["recommended_max_bid"],
                        "walk_away_price": advice["walk_away_price"],
                        "top_items": advice["top_items"],
                    }
                    if manifest_url:
                        patch_payload["manifest_doc_url"] = manifest_url
                    pr = c.patch(
                        f"/bstock_listings?auction_id=eq.{aid}",
                        json=patch_payload,
                    )
                    if pr.status_code >= 400:
                        log.error("Listings PATCH failed for %s: %s %s", aid, pr.status_code, pr.text[:300])
                log.info(
                    "Reno assessed %s: quality=%.1f fb_total=$%,.0f rec_bid=$%,.0f verdict=%s",
                    aid, advice["lot_quality_score"], advice["total_fb_value"],
                    advice["recommended_max_bid"], advice["verdict"],
                )

    # 5. Find qualifying unalerted
    qualifying = get_unalerted_qualifying()
    log.info("%d listings qualify for alert", len(qualifying))

    alerts_sent = 0

    for listing in qualifying:
        manifest_items: list[dict[str, Any]] = []
        if enrich_on and listing.get("manifest_doc_url"):
            raw_items = fetch_and_parse(listing["manifest_doc_url"])
            if raw_items:
                manifest_items = enrich_manifest(raw_items)
                # Run full quality + bid advice pipeline
                manifest_items = assess_items(manifest_items)
                ship = float(listing.get("shipping_estimate") or 300)
                advice = advise(listing, manifest_items, shipping=ship)
                listing["fb_total_value"] = advice["total_fb_value"]
                listing["lot_quality_score"] = advice["lot_quality_score"]
                listing["recommended_max_bid"] = advice["recommended_max_bid"]
                listing["walk_away_price"] = advice["walk_away_price"]
                bid = float(listing.get("current_bid") or 0)
                if (bid + ship) > 0 and advice["total_fb_value"] > 0:
                    listing["roi_score"] = round(
                        (advice["total_fb_value"] - bid - ship) / (bid + ship), 4
                    )
                insert_manifest_items(listing["auction_id"], manifest_items)

        t = tier(listing)
        result = send_alert(listing, t, manifest_items)
        if result.startswith("ok") or result == "shadow":
            mark_alerted(listing["auction_id"], t, {"listing": listing, "items": len(manifest_items)}, result)
            alerts_sent += 1

    # 5. Poll watchlist — snapshot bid history + alert on bid spikes
    watchlist = get_watchlist()
    snapped = 0
    for wid in watchlist:
        data = fetch_listing(wid)
        if data:
            upsert_listings([{**data, "auction_id": wid}])
            prev_snap = get_bid_history(wid)
            record_bid_snapshot(wid, data)
            snapped += 1

            # Bid velocity alert: fire if bid jumped >$200 or >20% since last snap
            if prev_snap:
                prev_bid = float(prev_snap[-1].get("current_bid") or 0)
                curr_bid = float(data.get("current_bid") or 0)
                bid_jump = curr_bid - prev_bid
                bid_jump_pct = (bid_jump / prev_bid) if prev_bid else 0

                if bid_jump >= 200 or bid_jump_pct >= 0.20:
                    # Fetch listing detail for rec_max_bid context
                    from storage.db import _client as _db_client
                    with _db_client() as c:
                        r = c.get(f"/bstock_listings?auction_id=eq.{wid}&select=*")
                        full_listing = r.json()[0] if r.status_code == 200 and r.json() else data

                    rec_max = full_listing.get("recommended_max_bid") or 0
                    headroom = rec_max - curr_bid if rec_max else None

                    spike_listing = {
                        **full_listing,
                        "title": f"⚡ BID SPIKE: {data.get('title',wid)}",
                        "current_bid": curr_bid,
                        "time_remaining": data.get("time_remaining"),
                        "url": data.get("url"),
                    }
                    spike_summary = {
                        **spike_listing,
                        "title": spike_listing["title"],
                        "msrp": full_listing.get("msrp"),
                        "current_bid": curr_bid,
                        "pct_of_msrp": full_listing.get("pct_of_msrp"),
                        "per_unit": full_listing.get("per_unit"),
                        "time_remaining": data.get("time_remaining"),
                        "url": data.get("url"),
                        "image_url": full_listing.get("image_url"),
                        "location": full_listing.get("location"),
                        "bid_jump": bid_jump,
                        "bid_jump_pct": round(bid_jump_pct * 100, 1),
                        "rec_max_bid": rec_max,
                        "headroom": headroom,
                    }
                    send_alert({"auction_id": wid, **spike_summary, "summary": spike_summary}, "high_priority")
                    log.warning(
                        "BID SPIKE %s: +$%.0f (+%.0f%%) → $%.0f | headroom: $%.0f",
                        wid, bid_jump, bid_jump_pct * 100, curr_bid, headroom or 0,
                    )
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
