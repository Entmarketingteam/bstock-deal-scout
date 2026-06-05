# B-Stock New-Auction Discovery: Turnstile-Free Alternatives

**Date:** 2026-06-04
**Author:** research pass (read-only — no live scraping, no login, no Railway mutations)
**Problem:** `bstock.com/all-auctions` (the only NEW-lot discovery surface) is behind a route-level
Cloudflare Turnstile interstitial. Plain `httpx` + residential proxy cannot clear it. Browserbase
Enterprise stealth modes are plan-gated. Login (FusionAuth JWT) and the detail endpoint
`/buy/listings/details/{id}` both work and are CF-exempt. **Goal:** an official path that avoids
Turnstile entirely for *discovering* new lots.

---

## Recommended Path (lede)

**Path C — Hybrid: Saved-Search / Seller-Subscription email digest (discovery) → existing
`/buy/listings/details/{id}` endpoint (enrichment).**

This avoids Turnstile **end-to-end** and reuses code that already works:

1. **Discovery (official, no Turnstile):** B-Stock natively offers buyer-facing
   **Saved Searches** and **Seller Subscription emails** — daily digests containing
   *"Just Listed"* and *"Closing Soon"* auctions matching saved filters / followed sellers.
   These arrive over email (or SMS for outbid/win events). Email delivery never touches
   the Cloudflare-protected web route. A saved search configured for our buying criteria
   (condition=New, target storefronts: Kohler `khl-Kohler`, Amazon, etc.) yields a daily
   feed of **new** listing URLs/IDs.
2. **Extraction:** Parse listing IDs from the digest email (each "Just Listed" row links to
   `/buy/listings/details/{id}` or `/all-auctions?...`). An IMAP/Gmail poll on the
   `marketingteam@nickient.com` inbox (already the B-Stock account email) feeds an n8n or
   Python job — same pattern the repo already uses for the alerts pipeline.
3. **Enrichment (already built, CF-exempt):** Pass each new ID to the existing
   `fetch_listing(auction_id)` → `/buy/listings/details/{id}` path, which is confirmed
   Cloudflare-exempt in production logs. This populates the full `Listing` structured fields
   (bid, MSRP, %MSRP, units, manifest via docserv, etc.) exactly as today.

**Why this is strictly stronger than any single channel:** it is the *only* option that is
(a) official/sanctioned, (b) Turnstile-free on both halves, and (c) reuses the existing,
working detail-fetch + parser code. The email half solves discovery; the detail endpoint
solves enrichment. No browser, no captcha solver, no plan upgrade.

**Caveat — latency:** Digests appear to be **daily**, not real-time (no evidence of a
real-time saved-search push tier in the public FAQ). For most lots this is acceptable
(auctions run multiple days). If sub-daily discovery is required for hot lots, keep
Browserbase as a secondary discovery path (see Path A note below) rather than the primary.

---

## Channel-by-Channel Findings (task items 1a–1e)

| # | Channel | Exists? | Avoids Turnstile? | Verdict |
|---|---------|---------|-------------------|---------|
| a | Official buyer API | **No** (public) | n/a | No public/self-serve buyer API. Only third-party scrapers + a Lob/Parabola B2B-ops angle. Eliminated. |
| b | RSS / Atom feed | **No** | n/a | No evidence of any RSS/Atom auction feed. Eliminated. |
| c | Saved-search / seller-subscription email digest | **Yes** | **Yes** | **RECOMMENDED** (see above). Official, email-delivered, CF-free. |
| d | sitemap.xml of auction URLs | Partial | **No** | `robots.txt` → `Sitemap: https://bstock.com/home-portal/sitemap.xml`, but that sitemap **returned 403 to unauthenticated fetch** — it is NOT CF-exempt, contrary to the task hypothesis. Also unlikely to list ephemeral auction detail URLs. Eliminated. |
| e | Mobile-app API (different CF rules) | **No app** | n/a | No B-Stock buyer iOS/Android app exists; platform is web-only. No separate mobile API surface to exploit. Eliminated. |

### Detail on (a) — Official API
- No public B-Stock buyer API or developer portal found. Search surfaced only:
  - **Apify `phmlabs/bstock-deal-finder`** ($29/mo + usage) — a third-party *scraper* that
    monitors live auctions and ranks by bid/MSRP, competition, urgency. It does **not** avoid
    Turnstile; it outsources the same scraping problem. Buy-vs-build fallback only.
  - **Parabola "B-Stock Solutions API"** — an ops/automation connector, not a buyer-discovery feed.
  - **Lob** address-verification integration — internal B-Stock plumbing, irrelevant.
- Verdict: no official self-serve API. A partner/data-feed *could* exist via direct B-Stock
  account-manager request, but that is a sales conversation, not a documented endpoint.

### Detail on (c) — the recommended channel (sources)
- **Seller subscription emails** = "daily notifications that include *Just Listed* and
  *Closing Soon* auctions and listings from the sellers you choose to follow within your
  Buying Preferences." Toggle per-seller in the seller-subscription section.
- **Saved Searches** = created directly from the ALP (Advanced Listing Platform); "get
  notified when inventory matching that search is available."
- **SMS alerts** exist for outbid/lost/won (My Account → Notification Preferences → SMS),
  but those are bid-status events, not new-lot discovery.
- Frequency: public FAQ describes **daily**; no documented real-time tier.

### Detail on (d) — sitemap / robots
- `robots.txt` exposes `Sitemap: https://bstock.com/home-portal/sitemap.xml` plus
  `Disallow: /*/?mode=list` and an auction forgot-password disallow.
- The sitemap URL **403s without auth** — Cloudflare covers it too, so the "sitemaps are
  often CF-exempt" assumption does **not** hold here. Dead end.

---

## Ranked Options (with verdicts)

### Path C — Email digest → detail endpoint (hybrid) — **RECOMMENDED**
- **Feasibility:** High. Both halves are already-working / officially-supported and CF-free.
- **Cost:** ~$0 incremental (email poll + existing detail fetch). No Browserbase, no captcha credits.
- **Effort:** Low–Medium. (1) Configure saved searches in the B-Stock buyer UI for our criteria;
  (2) build an inbox poller (Gmail/IMAP → n8n or Python) that extracts listing IDs from the
  daily digest; (3) feed IDs to existing `fetch_listing()`.
- **Risk:** Daily latency; depends on digest format stability (parse links, not layout).
- **Why #1:** Only option that is official AND Turnstile-free AND reuses working code.

### Path A — Browserbase plan upgrade (current discovery path)
- **Feasibility:** Already deployed; both env vars **confirmed set** (see item 3).
- **IMPORTANT CONTRADICTION TO RESOLVE:** `scraper/bstock.py` lines 73–77 claim the
  **Hobby-tier** mode (`proxies` + `solve_captchas`, *without* `advanced_stealth`) was
  **"verified live: 201 session, Turnstile solved, listings returned."** If that is still
  accurate, **discovery is NOT actually blocked on the current plan, and an Enterprise upgrade
  solves a non-problem.** The task premise says discovery is blocked. One of these is stale.
  - If the Hobby Browserbase path still works → no upgrade needed; the email-digest path is a
    **cost/reliability win**, not a rescue. Keep Browserbase as the real-time secondary.
  - If it regressed (Turnstile got harder, captcha-solving now fails on Hobby) → an upgrade to
    a tier with `advanced_stealth` *might* restore it, but at meaningful monthly cost and with
    no guarantee against future CF tightening.
- **Verdict:** Do NOT upgrade blindly. First confirm whether the documented Hobby-tier success
  still holds (review recent Railway run logs for "Browserbase discovery: N listings"). Treat
  Browserbase as the **secondary/real-time** path behind Path C, not the primary.

### Path B — Captcha-solver swap (2Captcha / CapSolver in place of Browserbase auto-solve)
- **Feasibility:** Medium. Technically works for Turnstile, but you still need a real browser
  context (JA3/cf_clearance) — so it doesn't remove the browser-automation dependency, only
  swaps who solves the captcha. Adds a second vendor + per-solve cost.
- **Verdict:** No advantage over Browserbase's built-in solver for this use case. Only consider
  if Browserbase's solver specifically regresses and is cheaper to replace than upgrade.
  Lower priority than A's "confirm-then-decide" and far below C.

### Fallback — Third-party scraper (Apify deal-finder)
- $29/mo + usage; does not avoid Turnstile, just outsources it. Buy-vs-build escape hatch if
  we want zero maintenance, but no strategic edge and recurring cost. Not recommended over C.

---

## Task Item 3 — Railway env-var check (read-only, confirmed)

Railway MCP returned `Unauthorized` (stale token), so verified via linked `railway` CLI instead.
Project `bstock-deal-scout` (id `9fecf028-...`), service `bstock-deal-scout`, env `production`,
status **Online**:

- `BROWSERBASE_API_KEY` → **set** (`bb_live_...`)
- `BROWSERBASE_PROJECT_ID` → **set** (`e20c6484-...`)

Both present. No changes made.

---

## Existing authenticated endpoints worth documenting (task item 2 — candidates, NOT probed)

From `scraper/bstock.py` and `scraper/fetch_listing.py`, all using the FusionAuth JWT as
`Authorization: Bearer` + `Cookie: token=...; access_token=...`:

| Endpoint | Method | Purpose | CF status |
|----------|--------|---------|-----------|
| `auth.bstock.com/api/login` | POST | FusionAuth JWT (applicationId `1b094c5f-...`) | Works (no CF) |
| `bstock.com/all-auctions?...&_rsc=rsc1` | GET (RSC) | **Discovery** — embedded listings JSON | **Turnstile-blocked** |
| `bstock.com/buy/listings/details/{id}?_rsc=rsc1` | GET (RSC) | Single-listing enrichment | **CF-exempt (confirmed)** |
| `docserv.bstock.com/v1/documents?listingId={id}` | GET | Manifest/document lookup | Works with Bearer JWT |
| `docserv.bstock.com/v1/documents/{doc_id}` | GET | Document download | Works with Bearer JWT |

**Candidate search/listing endpoints to PROBE later (do not probe live now):** the
`/all-auctions` RSC route is the *only* known list endpoint. The ALP "saved search" feature
implies a backing search API (likely a JSON service the SPA calls, possibly behind a path like
`/api/.../search` or an `alp`/`elasticsearch`-style endpoint). If that JSON search API is
**not** behind the same route-level Turnstile (it may be an XHR the SPA fires *after* the page
clears, thus only IP/JA3-gated, not interstitial-gated), it could be a cleaner discovery path
than the RSC HTML route. **Worth a careful authenticated probe** of the network calls the
saved-search UI fires — but that requires a logged-in browser session capture, out of scope
for this read-only pass. Document, do not probe.

**Private subdomains (from `private-marketplaces-brief.md`):** `ferguson.bstock.com`,
`bstock.com/kohler/` — same JWT, same Turnstile on their `/all-auctions`. The hybrid Path C
(saved search per private marketplace, if subscribable) would extend there too.
