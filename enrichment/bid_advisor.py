"""Lot-level bid recommendation engine.

Takes a list of assessed manifest items and the listing data, produces:
  - recommended_max_bid  : bid this to hit 50% gross margin
  - walk_away_price      : absolute ceiling (35% gross margin)
  - lot_quality_score    : weighted average quality across items
  - top_items            : best 5 items by bid_contribution (JSON-serializable)
  - summary              : human-readable recommendation string

Margin logic:
  target_resale = sum(fb_price × qty) for all items
  We want: gross_margin = (target_resale - landed_cost) / target_resale

  At 50% margin: max_bid = target_resale × 0.50 - shipping
  At 35% margin: walk_away = target_resale × 0.65 - shipping

  bid_contribution per item = fb_price × qty × 0.50  (item's share of max bid)
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Margin targets
CONSERVATIVE_MARGIN = 0.50   # recommended_max_bid: keep 50% gross margin
WALK_AWAY_MARGIN = 0.35      # walk_away_price: floor at 35% gross margin


def advise(
    listing: dict[str, Any],
    items: list[dict[str, Any]],
    shipping: float = 300.0,
) -> dict[str, Any]:
    """
    Generate a bid recommendation for a lot.

    Args:
        listing: bstock_listings row (needs current_bid, msrp, title, etc.)
        items:   assessed manifest items (need fb_price, qty, quality_score, etc.)
        shipping: estimated freight cost in dollars

    Returns:
        dict with: recommended_max_bid, walk_away_price, lot_quality_score,
                   top_items, summary, per_item_breakdown
    """
    # ── Condition-aware margin targets ───────────────────────────────────────
    lot_condition = (listing.get("condition") or listing.get("inventoryType") or "").lower()
    if "salvage" in lot_condition:
        effective_conservative = 0.65  # 65% margin for salvage — huge uncertainty
        effective_walk_away = 0.50
        condition_note = "Salvage margins applied (65%/50%)"
    elif "return" in lot_condition:
        effective_conservative = 0.55  # 55% for returns — unknown mix
        effective_walk_away = 0.42
        condition_note = "Returns margins applied (55%/42%)"
    else:
        effective_conservative = CONSERVATIVE_MARGIN
        effective_walk_away = WALK_AWAY_MARGIN
        condition_note = ""

    # ── Aggregate resale value ───────────────────────────────────────────────
    total_fb_value = 0.0
    total_items = 0
    quality_weighted_sum = 0.0
    quality_weight_total = 0.0

    item_rows = []
    for item in items:
        qty = int(item.get("qty") or 1)
        fb = float(item.get("fb_price") or 0)
        retail = float(item.get("unit_retail") or 0)
        q_score = float(item.get("quality_score") or 3.0)
        brand = item.get("brand") or ""
        desc = item.get("description") or ""

        item_fb_total = fb * qty
        total_fb_value += item_fb_total
        total_items += qty

        # Weighted quality (weight = retail value so high-value items drive score)
        if retail > 0:
            quality_weighted_sum += q_score * retail * qty
            quality_weight_total += retail * qty

        bid_contribution = round(item_fb_total * effective_conservative, 2)
        item_rows.append({
            "brand": brand,
            "description": desc[:60],
            "qty": qty,
            "unit_retail": retail,
            "fb_price": fb,
            "fb_total": round(item_fb_total, 2),
            "quality_score": q_score,
            "quality_notes": item.get("quality_notes") or "",
            "bid_contribution": bid_contribution,
            "ebay_sold_price": item.get("ebay_sold_price"),
            "hd_price": item.get("hd_price"),
            "lowes_price": item.get("lowes_price"),
        })

    # ── Lot-level metrics ────────────────────────────────────────────────────
    lot_quality = (
        round(quality_weighted_sum / quality_weight_total, 1)
        if quality_weight_total > 0
        else 3.0
    )

    # Bid recommendations (condition-adjusted margins)
    recommended_max_bid = max(0, round(total_fb_value * (1 - effective_conservative) - shipping, 0))
    walk_away_price = max(0, round(total_fb_value * (1 - effective_walk_away) - shipping, 0))

    current_bid = float(listing.get("current_bid") or 0)
    landed_at_current = current_bid + shipping

    # ── Top items ────────────────────────────────────────────────────────────
    top_items = sorted(item_rows, key=lambda x: x["fb_total"], reverse=True)[:5]

    # ── Summary ──────────────────────────────────────────────────────────────
    if current_bid <= recommended_max_bid:
        verdict = "BID ✅" if current_bid <= recommended_max_bid * 0.7 else "BID (near limit) ⚠️"
    elif current_bid <= walk_away_price:
        verdict = "MARGINAL — only if confident ⚠️"
    else:
        verdict = "PASS — current bid exceeds walk-away ❌"

    msrp = float(listing.get("msrp") or 0)
    margin_at_current = (
        round((total_fb_value - landed_at_current) / total_fb_value * 100, 1)
        if total_fb_value > 0 else 0
    )

    summary_lines = [
        f"Lot: {listing.get('title', '')[:70]}",
        f"Condition: {lot_condition.title() or 'Unknown'}" + (f" [{condition_note}]" if condition_note else ""),
        f"Items: {total_items} units across {len(items)} SKUs",
        f"Est. resale value: ${total_fb_value:,.0f} | MSRP: ${msrp:,.0f}",
        f"Shipping estimate: ${shipping:,.0f}",
        f"",
        f"VERDICT: {verdict}",
        f"  Recommended max bid: ${recommended_max_bid:,.0f}  ({effective_conservative*100:.0f}% gross margin)",
        f"  Walk-away ceiling:   ${walk_away_price:,.0f}  ({effective_walk_away*100:.0f}% gross margin)",
        f"  Current bid:         ${current_bid:,.0f}  → landed ${landed_at_current:,.0f}",
        f"  Margin at current:   {margin_at_current:.1f}%",
        f"",
        f"Lot quality score: {lot_quality}/10",
        f"Top 3 items:",
    ]
    for item in top_items[:3]:
        summary_lines.append(
            f"  • {item['brand']} {item['description'][:45]} "
            f"(×{item['qty']}) → ${item['fb_price']:,.0f}/ea "
            f"[Q:{item['quality_score']}/10]"
        )

    return {
        "recommended_max_bid": recommended_max_bid,
        "walk_away_price": walk_away_price,
        "lot_quality_score": lot_quality,
        "total_fb_value": round(total_fb_value, 2),
        "shipping": shipping,
        "top_items": top_items,
        "verdict": verdict,
        "margin_at_current": margin_at_current,
        "summary": "\n".join(summary_lines),
        "per_item_breakdown": sorted(item_rows, key=lambda x: x["fb_total"], reverse=True),
    }
