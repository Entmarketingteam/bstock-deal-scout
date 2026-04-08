-- B-Stock Deal Scout schema
-- Run against Supabase DB_URL in example-project/prd

create table if not exists bstock_sources (
    id text primary key,
    name text not null,
    url text not null,
    source_type text default 'bstock',
    priority int default 2,
    tags text[],
    active boolean default true,
    created_at timestamptz default now()
);

create table if not exists bstock_listings (
    auction_id text primary key,
    source_id text references bstock_sources(id),
    url text not null,
    title text,
    image_url text,
    manifest_doc_url text,
    location text,
    listing_type text,      -- Auction | Make An Offer
    condition text,          -- Overstock | Customer Returns | etc
    unit_count int,
    msrp numeric(12,2),
    current_bid numeric(12,2),
    pct_of_msrp numeric(6,2),
    per_unit numeric(12,2),
    time_remaining text,
    ends_at timestamptz,
    bid_count int,
    price_label text,        -- Great Price | Good Price | Fair Price
    storefront text,
    deal_score numeric(6,4) generated always as (
        case when msrp > 0 then coalesce(current_bid, 0) / msrp else null end
    ) stored,
    alerted boolean default false,
    alerted_at timestamptz,
    first_seen timestamptz default now(),
    last_seen timestamptz default now(),
    raw_json jsonb
);

create index if not exists idx_bstock_listings_price_label on bstock_listings(price_label);
create index if not exists idx_bstock_listings_first_seen on bstock_listings(first_seen);
create index if not exists idx_bstock_listings_alerted on bstock_listings(alerted) where alerted = false;
create index if not exists idx_bstock_listings_storefront on bstock_listings(storefront);

create table if not exists bstock_manifest_items (
    id bigserial primary key,
    auction_id text references bstock_listings(auction_id) on delete cascade,
    lot_id text,
    seller_category text,
    description text,
    qty int,
    unit_retail numeric(12,2),
    ext_retail numeric(12,2),
    item_num text,
    upc text,
    vendor text,
    category text,
    subcategory text,
    condition text,
    brand text,
    color text,
    model text,
    notes text,
    real_price numeric(12,2),       -- enriched
    real_image_url text,             -- enriched
    real_source_domain text,         -- enriched
    real_source_url text,            -- enriched
    enriched_at timestamptz,
    created_at timestamptz default now()
);

create index if not exists idx_manifest_items_auction on bstock_manifest_items(auction_id);
create index if not exists idx_manifest_items_item_num on bstock_manifest_items(item_num);

create table if not exists bstock_alerts (
    id bigserial primary key,
    auction_id text references bstock_listings(auction_id),
    alert_tier text,                 -- normal | high_priority
    payload jsonb,
    sent_at timestamptz default now(),
    webhook_response text
);

create index if not exists idx_alerts_auction on bstock_alerts(auction_id);
create index if not exists idx_alerts_sent on bstock_alerts(sent_at);
