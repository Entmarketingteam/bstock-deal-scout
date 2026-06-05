"""Throwaway: verify _scrape_via_browserbase clears Cloudflare Turnstile on Hobby plan.

Bypasses the direct-RSC fast-path AND the DB fallback so neither masks a 0.
Calls _scrape_via_browserbase DIRECTLY with a live FusionAuth JWT.
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("verify")

import scraper.bstock as bs

def main():
    mode = os.getenv("BROWSERBASE_MODES", "(module default)")
    log.info("=== BB VERIFY START  modes=%s ===", mode)
    log.info("BB key set=%s project set=%s", bool(bs._BROWSERBASE_API_KEY), bool(bs._BROWSERBASE_PROJECT_ID))

    token = bs.get_jwt_token()
    log.info("JWT acquired len=%d", len(token))

    conditions = ["New"]
    listings = bs._scrape_via_browserbase(token, conditions)

    print("\n========================================")
    print(f"RESULT: {len(listings)} listings via Browserbase (modes={mode})")
    print("========================================\n")
    for l in listings[:5]:
        print(f"  {l.auction_id}  {(l.title or '')[:50]}  bid={l.current_bid}")
    return len(listings)

if __name__ == "__main__":
    n = main()
    sys.exit(0 if n > 0 else 2)
