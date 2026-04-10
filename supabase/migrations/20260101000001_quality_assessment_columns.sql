-- Migration: add quality assessment + operational columns
-- Safe to re-run (uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS)

-- bstock_listings: operational + quality assessment columns
ALTER TABLE bstock_listings
  ADD COLUMN IF NOT EXISTS has_manifest       boolean        DEFAULT false,
  ADD COLUMN IF NOT EXISTS shipping_estimate  numeric(12,2),
  ADD COLUMN IF NOT EXISTS reno_relevant      boolean        DEFAULT false,
  ADD COLUMN IF NOT EXISTS fb_total_value     numeric(12,2),
  ADD COLUMN IF NOT EXISTS roi_score          numeric(8,4),
  ADD COLUMN IF NOT EXISTS lot_quality_score  numeric(4,1),
  ADD COLUMN IF NOT EXISTS recommended_max_bid numeric(12,2),
  ADD COLUMN IF NOT EXISTS walk_away_price    numeric(12,2),
  ADD COLUMN IF NOT EXISTS top_items          jsonb;

-- bstock_manifest_items: eBay + retail enrichment + quality scoring columns
ALTER TABLE bstock_manifest_items
  ADD COLUMN IF NOT EXISTS fb_price           numeric(12,2),
  ADD COLUMN IF NOT EXISTS fb_source          text,
  ADD COLUMN IF NOT EXISTS fb_searched_at     timestamptz,
  ADD COLUMN IF NOT EXISTS quality_score      numeric(4,1),
  ADD COLUMN IF NOT EXISTS quality_notes      text,
  ADD COLUMN IF NOT EXISTS ebay_sold_price    numeric(12,2),
  ADD COLUMN IF NOT EXISTS ebay_sold_count    int,
  ADD COLUMN IF NOT EXISTS ebay_low           numeric(12,2),
  ADD COLUMN IF NOT EXISTS ebay_high          numeric(12,2),
  ADD COLUMN IF NOT EXISTS ebay_active_low    numeric(12,2),
  ADD COLUMN IF NOT EXISTS ebay_price_source  text,
  ADD COLUMN IF NOT EXISTS hd_price           numeric(12,2),
  ADD COLUMN IF NOT EXISTS hd_url             text,
  ADD COLUMN IF NOT EXISTS lowes_price        numeric(12,2),
  ADD COLUMN IF NOT EXISTS lowes_url          text,
  ADD COLUMN IF NOT EXISTS mfr_price          numeric(12,2),
  ADD COLUMN IF NOT EXISTS mfr_url            text;

-- bstock_watchlist: tracked listings
CREATE TABLE IF NOT EXISTS bstock_watchlist (
  auction_id text PRIMARY KEY REFERENCES bstock_listings(auction_id) ON DELETE CASCADE,
  reason     text,
  added_at   timestamptz DEFAULT now(),
  active     boolean DEFAULT true
);

-- bstock_bid_history: time-series bid snapshots for watched listings
CREATE TABLE IF NOT EXISTS bstock_bid_history (
  id           bigserial PRIMARY KEY,
  auction_id   text REFERENCES bstock_listings(auction_id) ON DELETE CASCADE,
  snapped_at   timestamptz DEFAULT now(),
  current_bid  numeric(12,2),
  bid_count    int,
  pct_of_msrp  numeric(6,2),
  time_remaining text,
  price_label  text
);

CREATE INDEX IF NOT EXISTS idx_bid_history_auction ON bstock_bid_history(auction_id);
CREATE INDEX IF NOT EXISTS idx_bid_history_snapped ON bstock_bid_history(snapped_at);
CREATE INDEX IF NOT EXISTS idx_bstock_listings_reno ON bstock_listings(reno_relevant) WHERE reno_relevant = true;
CREATE INDEX IF NOT EXISTS idx_bstock_listings_manifest ON bstock_listings(has_manifest);
