# bstock-deal-scout

Auto-scrapes B-Stock liquidation auctions, scores listings, and alerts on "Great Price" lots via n8n → Slack + email.

## What it does

1. Logs into bstock.com (session cookie persistence)
2. Scrapes `all-auctions?condition=["New"]` every 15 min
3. Parses listing cards: title, MSRP, current bid, % of MSRP, B-Stock's price label, manifest URL
4. Downloads manifest CSVs for qualifying lots
5. Enriches manifest line items with real product images + retail prices via Tavily (Google Shopping)
6. Scores lots and alerts via n8n webhook → Slack `#deals` + email

## Alert rule (v1)

```
price_label == "Great Price"
AND msrp >= $2,000
AND listing_type == "Auction"
AND auction_id not in previously_alerted
```

Tier 2 `HIGH_PRIORITY` if `pct_of_msrp < 5` AND `msrp >= $10,000`.

## Architecture

```
n8n cron (every 15m)
  → POST https://<railway-url>/run
    → Playwright scraper (bstock.com)
      → Supabase upsert (dedup by auction_id)
        → Tavily enrichment (new lots only)
          → Scoring
            → n8n webhook for alerts
              → Slack + email
```

## Environment

All secrets via Doppler — never hardcode.

```bash
# Doppler ent-agency-automation/prd
BSTOCK_EMAIL
BSTOCK_PASSWORD
N8N_API_KEY

# Doppler example-project/prd
DB_URL              # Supabase Postgres
TAVILY_API_KEY
```

Local dev:
```bash
doppler run --project ent-agency-automation --config prd -- \
doppler run --project example-project --config prd -- \
uvicorn main:app --reload
```

## Deploy

```bash
railway up
```

See `sql/schema.sql` for Supabase tables.
See `n8n/workflow.json` for cron workflow import.

## Roadmap

- **v1**: bstock.com main marketplace, Tavily enrichment, Slack + email alerts
- **v2**: B-Stock private marketplaces (Ulta, Home Depot, Costco, Best Buy) — same scraper, different URL list
- **v3**: Liquidation.com, Direct Liquidation, BULQ — separate scraper modules
- **v4**: Auction aggregators (HiBid, Proxibid, GovDeals)
