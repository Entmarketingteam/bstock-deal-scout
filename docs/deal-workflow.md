# B-Stock Deal Evaluation Workflow

Repeatable playbook developed from live deal analysis on 2026-04-10.

---

## The 6-Step Process

### Step 1 — Find the Lot + Scrape the Manifest

Most B-Stock storefronts are client-side rendered (React/Next.js) — **no product data without login**. Exceptions:
- Winston Water Cooler (`bstock.com/buy/seller/winston-water-cooler`) — data embedded in Next.js server-push HTML, scrapeable
- Kohler (`bstock.com/kohler/...`) — fully CSR, no data. Use listing title only.

Pull the manifest SKU/UPC from the listing detail page. Even one model number is enough to research resale value.

### Step 2 — Identify the Product + Street Price

Search authorized dealer sites — **not Amazon, not MSRP**. Street price is what a contractor pays today:
- tubz.com, QualityBath.com, Frank Webb Home, SupplyHouse.com
- Home Depot / Lowe's (for plumbing fixtures)
- Manufacturer direct (Hydro Systems, Kohler, etc.)

MSRP is often 25–40% above street price. Use street price for your resale floor comp.

**Example:** Hydro Systems OPA6333STO — MSRP $6,015 → street price $4,330 at QualityBath. That's the price your buyer can pay today from a real dealer. Your price has to beat that + be worth the inconvenience of buying from an individual.

### Step 3 — Pick the Right Resale Channel

| Product Type | Best Channel | Velocity | Key Constraint |
|---|---|---|---|
| Freestanding/soaking tubs | FB Marketplace local, Craigslist | Medium | Must be local pickup — freight on 200 lb tub destroys margin |
| Towel bars / bath accessories (200+ units) | STR developer direct, FB groups | Fast if whole-lot | Too many units for eBay individual; pitch contractors/builders |
| Shower trim / valve trim | Contractor direct | Slow | Config-specific (pressure-balance, model compat) — bad eBay item |
| Kitchen faucets | eBay, Facebook | Medium | Check sold comps for specific model |
| Commercial water heaters | Local supply house (Ferguson, Waxman), Facebook local | Slow | Freight-only; narrow commercial buyer pool |
| Discontinued residential water heaters | eBay national | Medium | Discontinued = demand from contractors mid-job |
| Solid surface tubs (Hydro Systems, MTI, etc.) | Authorized showroom trade-in, GC direct | Fast at right price | Showrooms often buy to resell; call before you even bid |

### Step 4 — Run the Bid Math

```
Max bid = (conservative resale price) - (freight/LTL cost) - ($500 minimum margin)
Walk away = price where you break even
```

- Local pickup (no freight): higher max bid, faster exit
- LTL freight items: budget $300–900 depending on weight/distance
- B-Stock "Typical Selling Price" shown on listings is a reasonable (conservative) floor comp

**Example — Hydro Systems Opal tub ($6,015 MSRP, Dallas TX):**
```
Conservative resale: $2,000 (FB Marketplace)
Freight: $0 (local pickup)
Margin floor: $500
Max bid: $2,000 - $0 - $500 = $1,500
Walk away: $2,000 (margin gone)
Actual start bid: $602 with 0 bids → strong buy
```

### Step 5 — Find the Buyer BEFORE Bidding

Don't win a lot without knowing who you're selling to. Make the call before the auction closes:

- **High-end freestanding tubs in Dallas** → The Jarrell Company (214-363-7211, 2651 Fondren Dr) — authorized Hydro Systems showroom, they'll quote you a buy price on the spot
- **Kohler hardware lots** → STR cabin developer Facebook groups, Instagram `#cabinbuild` builders, hotel/short-term rental operators
- **Commercial HVAC/plumbing** → Local supply houses (Ferguson, Johnstone, Waxman), Facebook local contractor groups
- **Whole lots (200+ units)** → PMCs, hotel operators, GCs doing multi-unit builds — pitch as "complete [room type] package at X cents on the dollar"

### Step 6 — Snipe in the Last 5 Minutes

B-Stock auctions end at a fixed time — **no auto-extend** like eBay. This means:
- Don't bid early. You drive up the price and give competitors time to respond.
- Watch the lot via the `/watch/{auction_id}` endpoint — bid velocity alerts will fire if the price jumps significantly
- Place your max bid in the last 3–5 minutes
- Set a hard walk-away number and don't chase it past that

---

## Example Deals (2026-04-10)

### Deal 1 — Hydro Systems Opal Freestanding Tub ✅ Strong Buy

| Field | Value |
|---|---|
| Lot | `69d4334aab8b3998cc1e2692` |
| Storefront | Winston Water Cooler |
| Product | Hydro Systems OPA6333STO-WHI — 63"×33" STON solid surface soaking tub |
| Material | HydroLuxe STON (mineral composite, stone resin class — not acrylic) |
| Condition | Brand new overstock, 10-year warranty |
| MSRP | $6,015 |
| Street price | $4,330–$4,511 (authorized dealers) |
| Start bid | $602, 0 bids |
| Location | Dallas TX (local pickup viable) |
| Max bid | $1,500 |
| Walk away | $2,800 |
| Best exit | Call The Jarrell Company (214-363-7211) before bidding — they may buy for $2,000+ for showroom resale |
| FB Marketplace | List at $2,200 for local Dallas pickup, expect $1,800–2,500 |

**Why it's good:** STON material (not cheap acrylic), new condition, street price $4,300+ from real dealers, starting bid is 10% of MSRP with zero competition. Local pickup in Dallas eliminates all freight risk.

---

### Deal 2 — 2 Pallets Water Heaters ⚠️ Situational

| Field | Value |
|---|---|
| Lot | `69d436d4eca1d16f616402dc` |
| Storefront | Winston Water Cooler |
| Products | Lochinvar Shield LP 125MBH (commercial, 65-gal) + AO Smith GDHE-75 Vertex 100 (discontinued) |
| MSRP | $21,053 combined |
| Start bid | $3,000 (2 units) |
| Location | Glendale AZ (LTL freight required if not local) |
| Exit: Lochinvar | Facebook AZ to plumbing contractor at $3,500–5,500 |
| Exit: AO Smith | eBay national at $1,800–3,200 (discontinued = demand) |
| Max bid | ~$5,000 combined |
| Walk away | $6,500 |

**Why it's situational:** Strong margin math on paper, but Lochinvar commercial heater has a narrow buyer pool (commercial contractors only). If you're not in Phoenix/Glendale metro, LTL freight ($400–600 per unit) compresses the margin fast. AO Smith being discontinued is the stronger individual item.

---

### Deal 3 — Kohler STR Hardware Package 🏗️ Contractor Play

| Field | Value |
|---|---|
| Lots | khl3365, khl3366, khl3364, khl3363, khl3338, sgn3150 |
| Content | Kohler towel bars, shower trim, valve trim, shower heads, kitchen faucets + Signature Hardware fixtures |
| Units | 600+ across all lots |
| Confirmed finish | Brushed Nickel (khl3365 — product code 97497-BN) |
| Best exit | Whole-lot sale to STR developer or PMC doing 10–50 unit cabin build |
| Wrong channel | eBay individual — too config-specific, too many units, slow velocity |
| Pitch asset | Lookbook at `/lookbook-report` — real product images + AI bathroom renders |

**Why it's a contractor play not a flipper play:** 600+ units of matching Kohler BN hardware is exactly what a cabin developer needs for 50 bathrooms at once. Per-bathroom cost at current bids is under $100 for full trim package. Pitch this as a complete package, not individual items.

---

## What Makes a B-Stock Deal Work

**Green flags:**
- Brand new / overstock condition (not customer returns)
- High-quality brand with strong authorized-dealer street price (floor for your resale)
- Local pickup possible (eliminates freight risk on heavy/fragile items)
- Zero or few current bids → you can win at or near starting bid
- Whole-lot buyer exists before you bid (showroom, contractor, developer)
- B-Stock "Typical Selling Price" is well above your buy-in cost

**Red flags:**
- Customer returns / mixed condition (Grade A/B/C) — condition risk on expensive items
- LTL freight-only + heavy/fragile + narrow buyer pool (e.g. commercial HVAC in wrong city)
- Config-specific items (shower valve trim, pressure-balance — model compatibility limits buyer pool)
- MSRP inflated vs actual street price (check authorized dealers, not just MSRP)
- No way to verify condition before paying (especially on high-value single items)

---

## Resale Channel Quick Reference

| Channel | Best for | Notes |
|---|---|---|
| Facebook Marketplace | Anything local, heavy items, luxury goods | Best for local pickup; search buyer pool by posting early |
| Craigslist | Same as FB, secondary | Lower traffic but less competition from other sellers |
| eBay | Individual items, national shipping, discontinued models | Avoid for oversized/freight, config-specific plumbing |
| Authorized showroom trade | New-condition premium fixtures (tubs, faucets) | Call before bidding — showrooms buy overstock to resell at margin |
| Facebook groups / Instagram | STR developers, contractor communities, `#cabinbuild` | For whole-lot contractor pitch; use lookbook link as asset |
| Local supply houses | Commercial HVAC, plumbing, electrical | Ferguson, Waxman, etc. buy new overstock at 40–60% wholesale |
| Direct contractor outreach | Any large lot of a single trade item | LinkedIn PMC/GC search + cold DM with lot details |

---

## Tools Built Into This System

| Endpoint | Use |
|---|---|
| `POST /run` | Cron: scrape → enrich → alert. n8n hits every 15min |
| `GET /reno` | All reno-relevant lots with ROI calc |
| `GET /bundles` | Group lots into contractor packages with per-bathroom cost |
| `GET /lookbook-report` | Shareable HTML lookbook — product images + AI renders |
| `POST /generate-mockups` | Generate DALL-E 3 cabin bathroom renders, cache in Supabase Storage |
| `GET /advise/{id}` | Full bid recommendation with quality score + walk-away price |
| `POST /watch/{id}` | Add to watchlist — get bid velocity spike alerts |
