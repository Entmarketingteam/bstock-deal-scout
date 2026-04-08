"""One-time helper: opens your real Chrome, you log in, saves cookies.

Run locally (close all regular Chrome windows first so we can reuse the profile):
  python scripts/bootstrap_session.py

Then run the printed `railway variables --set ...` command.
"""
from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("bstock_storage.json")
# Fresh Playwright profile dir — isolated from your main Chrome profile
# so you don't fight file locks. You'll need to log in once here.
USER_DATA_DIR = Path.home() / ".bstock_scraper_profile"


def main() -> None:
    USER_DATA_DIR.mkdir(exist_ok=True)
    with sync_playwright() as pw:
        # channel="chrome" uses your installed Chrome binary (not Playwright's Chromium)
        # Real Chrome has a much better chance of passing Cloudflare Turnstile.
        proxy_args = {}
        if os.getenv("WEBSHARE_PROXY_USER"):
            proxy_args["proxy"] = {
                "server": "http://p.webshare.io:6045",
                "username": os.environ["WEBSHARE_PROXY_USER"],
                "password": os.environ["WEBSHARE_PROXY_PASS"],
            }
            print("Using Webshare residential proxy")

        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=False,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            **proxy_args,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto('https://bstock.com/all-auctions?condition=%5B%22New%22%5D')

        print("\n" + "=" * 60)
        print("BSTOCK SESSION BOOTSTRAP")
        print("=" * 60)
        print("1. Wait for Cloudflare to clear (may take 5-15 seconds)")
        print("2. If it doesn't clear, click the checkbox if one appears")
        print("3. Log in with marketingteam@nickient.com")
        print("4. Verify you can see the auction listings page")
        print("5. Come back here and press ENTER")
        print("=" * 60 + "\n")
        input("Press ENTER when logged in and auctions are visible... ")

        context.storage_state(path=str(OUT))
        context.close()

    raw = OUT.read_bytes()
    b64 = base64.b64encode(raw).decode()
    print(f"\n[OK] Saved {len(raw)} bytes to {OUT}")
    print(f"\nBase64 blob ({len(b64)} chars) — run this to upload to Railway:\n")
    print(f'cd ~/projects/bstock-deal-scout && railway variables --set "BSTOCK_STORAGE_STATE_B64={b64}"')
    print()


if __name__ == "__main__":
    main()
