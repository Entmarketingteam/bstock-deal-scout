# B-Stock Deal Scout v2: Private Marketplaces Brief

This document scopes the expansion of the **B-Stock Deal Scout** from the centralized public feed to brand-specific **Private Marketplaces**, specifically targeting **Kohler** and **Ferguson**.

---

## 1. Executive Summary

In B-Stock's ecosystem, high-value enterprise retailers often operate in two modes:
1. **Public Storefronts:** Listed and searchable on the central index (`bstock.com/all-auctions`).
2. **Private Marketplaces:** Hosted on custom, restricted subdomains (e.g., `ferguson.bstock.com`). These auctions are **omitted from the central public feed** and are only visible to approved buyers logged into that specific subdomain.

### The Opportunity
By scoping and implementing v2 Private Marketplaces, we will:
* Capture **exclusive contractor-grade plumbing and hardware lots** that are hidden from public scrapers.
* Reduce bidding competition significantly, since private subdomains require approval and have lower traffic.
* Maximize the utility of our residential proxy and Browserbase Turnstile-bypass setup by applying it across custom origins.

---

## 2. Target Platforms & Endpoint Mapping

### Kohler
* **Marketplace URL:** `https://bstock.com/kohler/`
* **Scoping Result:** Kohler operates as a **Public Storefront** on the central domain. 
  * Its listings are visible via: `https://bstock.com/all-auctions?storefront=%5B%22khl-Kohler%22%5D`.
  * **Scraper Status:** Already supported in v1. Detail queries go through `https://bstock.com/buy/listings/details/khlXXXX`.
  * **v2 Optimization:** Create a dedicated daily run/filter specifically for `khl-Kohler` to push priority alerts, as Kohler items have high street-price resale values for STR/residential builders.

### Ferguson
* **Marketplace URL:** `https://ferguson.bstock.com/`
* **Scoping Result:** Ferguson operates a **Private Subdomain**. These listings are **not** present on the main `bstock.com/all-auctions` feed.
  * **Listings Endpoint:** `https://ferguson.bstock.com/all-auctions?{condition_qs}&offset={offset}&_rsc=rsc1`
  * **Detail Endpoint:** `https://ferguson.bstock.com/buy/listings/details/{auction_id}?_rsc=rsc1`
  * **Scraper Status:** Requires subdomain routing implementation in `bstock.py` and `fetch_listing.py`.

---

## 3. Authentication & Session Sharing

B-Stock uses **Single Sign-On (SSO)** managed by **FusionAuth** under the central domain `auth.bstock.com`.

```
                    [ Central FusionAuth IdP ]
                     (auth.bstock.com/api/login)
                                 │
                     User JWT Token Generated
                                 │
         ┌───────────────────────┴───────────────────────┐
         ▼                                               ▼
[ Public Storefronts ]                         [ Private Subdomain ]
(bstock.com/all-auctions)                     (ferguson.bstock.com)
  Cookie: token=JWT                             Cookie: token=JWT
```

### Authentication Mechanics
1. **SSO Flow:** When logging into `ferguson.bstock.com`, the site redirects the user to `auth.bstock.com/api/login` to authenticate and returns a JWT.
2. **Token Compatibility:** The JWT retrieved via our existing central login function (`get_jwt_token()`) is **globally valid across all B-Stock subdomains**, provided the user's account has been approved for that specific private marketplace.
3. **Session Injection:** When requesting Ferguson's endpoints, we can inject the exact same cached JWT into the headers:
   ```python
   headers = {
       "Authorization": f"Bearer {token}",
       "Cookie": f"token={token}; access_token={token}"
   }
   ```

---

## 4. Cloudflare & Browserbase on Private Subdomains

Like the central platform, B-Stock private subdomains employ **Cloudflare Turnstile** protection on the `/all-auctions` route. 

### Bypass Strategy
Our existing **Browserbase + residential proxy** Turnstile solver is highly adaptable and can be generalized to run on subdomains:
1. **Dynamic Origin:** Instead of hardcoding `https://bstock.com` as the browser origin, we pass the target marketplace origin (`https://ferguson.bstock.com`).
2. **Cookie Injection:** We launch the browser, navigate to the target subdomain, and inject the central JWT token cookie with `domain=".bstock.com"` so it propagates to the subdomain.
3. **Execution:** Drive the headless browser to clear the Turnstile interstitial on `https://ferguson.bstock.com/all-auctions` and retrieve the RSC page content directly.

---

## 5. Implementation Blueprint

To deploy v2, we will implement the following changes in the `bstock-deal-scout` codebase:

### Step 1: Support Dynamic Domains in `bstock.py`
Refactor listing discovery to dynamically accept a target domain parameter:
```python
def scrape_listings(domain: str = "bstock.com", offset: int = 0) -> list[Listing]:
    # 1. Get auth token
    token = get_jwt_token()
    
    # 2. Format URL based on domain
    url = f"https://{domain}/all-auctions?condition=%5B%22New%22%5D&offset={offset}&_rsc=rsc1"
    
    # 3. Fetch (either via httpx or Browserbase depending on Turnstile challenge)
    # ...
```

### Step 2: Update `fetch_listing.py`
Update the detailed listing fetcher to route requests to the correct subdomain based on the listing ID prefix (e.g., `sgn` listings to `ferguson.bstock.com` and `khl` to `bstock.com`):
```python
def get_domain_for_listing(auction_id: str) -> str:
    if auction_id.startswith("sgn"):
        return "ferguson.bstock.com"
    return "bstock.com"

def fetch_listing(auction_id: str) -> dict[str, Any] | None:
    token = get_jwt_token()
    domain = get_domain_for_listing(auction_id)
    url = f"https://{domain}/buy/listings/details/{auction_id}?_rsc=rsc1"
    # ...
```

### Step 3: Database Schema Update (Supabase)
Add a `domain` or `marketplace` column to the `bstock_listings` table in Supabase to track where each listing was discovered:
```sql
ALTER TABLE bstock_listings ADD COLUMN IF NOT EXISTS domain text DEFAULT 'bstock.com';
```

---

## 6. Next Steps & Schedule

1. **Verify Account Permissions:** Confirm Ethan/Emily's production account has active approvals for `ferguson.bstock.com`.
2. **Apply Database Migration:** Run the SQL column addition in the Supabase console.
3. **Apply Code Changes:** Refactor `scraper/bstock.py` and `scraper/fetch_listing.py` to support dynamic domain routing.
4. **Deploy & Validate:** Deploy to Railway (`SHADOW_MODE=false`) and run a targeted test scan of `ferguson.bstock.com` to verify alerts are pushed to the Slack/email n8n pipeline.
