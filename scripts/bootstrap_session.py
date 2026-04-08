"""One-time helper: opens a visible Chromium, you log in manually, saves cookies.

Run locally:
  python scripts/bootstrap_session.py

Then upload the printed base64 blob to Railway as env var BSTOCK_STORAGE_STATE_B64.
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("bstock_storage.json")


def main() -> None:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = context.new_page()
        page.goto('https://bstock.com/all-auctions?condition=%5B%22New%22%5D')

        print("\n" + "=" * 60)
        print("BSTOCK SESSION BOOTSTRAP")
        print("=" * 60)
        print("1. Pass the Cloudflare challenge (it'll clear on its own)")
        print("2. Log in to B-Stock with marketingteam@nickient.com")
        print("3. Verify you can see the auction listings")
        print("4. Come back here and press ENTER")
        print("=" * 60 + "\n")
        input("Press ENTER when logged in and auctions are visible... ")

        context.storage_state(path=str(OUT))
        browser.close()

    raw = OUT.read_bytes()
    b64 = base64.b64encode(raw).decode()
    print(f"\n✅ Saved {len(raw)} bytes to {OUT}")
    print(f"\nBase64 blob ({len(b64)} chars) — set this as BSTOCK_STORAGE_STATE_B64 on Railway:\n")
    print(b64)
    print()
    print("Command to set it:")
    print(f'  cd ~/projects/bstock-deal-scout && railway variables --set "BSTOCK_STORAGE_STATE_B64={b64}"')


if __name__ == "__main__":
    main()
