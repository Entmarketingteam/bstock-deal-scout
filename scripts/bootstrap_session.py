"""Auto-login bootstrap: launches real Chrome, logs in, saves cookies, uploads to Railway.

Run locally:
  doppler run --project ent-agency-automation --config prd -- doppler run --project example-project --config prd -- python scripts/bootstrap_session.py

No manual interaction needed (unless CF shows a Turnstile checkbox — click it once if so).
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext

OUT = Path("bstock_storage.json")
LOGIN_URL = "https://bstock.com/acct/signin"
LISTINGS_URL = 'https://bstock.com/all-auctions?condition=%5B%22New%22%5D'


def _wait_clear(page: Page, timeout_ms: int = 90000, label: str = "") -> bool:
    """Wait for Cloudflare challenge to resolve."""
    label_str = f" on {label}" if label else ""
    print(f"  Waiting for page to load{label_str}...")
    try:
        page.wait_for_function(
            """() => {
                const t = document.title.toLowerCase();
                const b = (document.body && document.body.innerText || '').toLowerCase();
                return !t.includes('just a moment') && !b.includes('verifying') && document.readyState === 'complete';
            }""",
            timeout=timeout_ms,
        )
        print(f"  OK: {page.title()[:80]}")
        return True
    except Exception as exc:
        print(f"  Timeout ({timeout_ms}ms) — {exc}")
        print(f"  Title: {page.title()}  URL: {page.url}")
        return False


def main() -> None:
    email = os.environ.get("BSTOCK_EMAIL", "marketingteam@nickient.com")
    password = os.environ.get("BSTOCK_PASSWORD", "")
    if not password:
        print("ERROR: BSTOCK_PASSWORD not set. Run with:")
        print("  doppler run --project ent-agency-automation --config prd -- doppler run --project example-project --config prd -- python scripts/bootstrap_session.py")
        sys.exit(1)

    proxy_user = os.getenv("WEBSHARE_PROXY_USER", "")
    proxy_pass = os.getenv("WEBSHARE_PROXY_PASS", "")

    print(f"\n{'='*60}")
    print("BSTOCK AUTO-LOGIN BOOTSTRAP")
    print(f"{'='*60}")
    print(f"Email:   {email}")
    print(f"Proxy:   {'Webshare port 80' if proxy_user else 'none (direct IP)'}")
    print()

    with sync_playwright() as pw:
        # Build launch kwargs
        # ignore_default_args strips Playwright's automation flags that CF detects:
        # --enable-automation sets navigator.webdriver=true → infinite CF loop
        launch_kwargs: dict[str, Any] = {
            "channel": "chrome",
            "headless": False,
            "ignore_default_args": [
                "--enable-automation",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-client-side-phishing-detection",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-features=ImprovedCookieControls,LazyFrameLoading,GlobalMediaControls,DestroyProfileOnBrowserClose,MediaRouter,DialMediaRouteProvider,AcceptCHFrame,AutoExpandDetailsElement,CertificateTransparencyComponentUpdater,AvoidUnnecessaryBeforeUnloadCheckSync,Translate,HttpsUpgrades,PaintHolding,ThirdPartyStoragePartitioning,LensOverlay,PlzDedicatedWorker",
                "--disable-hang-monitor",
                "--disable-ipc-flooding-protection",
                "--disable-popup-blocking",
                "--disable-prompt-on-repost",
                "--disable-renderer-backgrounding",
                "--metrics-recording-only",
                "--no-first-run",
                "--password-store=basic",
                "--use-mock-keychain",
                "--no-service-autorun",
                "--export-tagged-pdf",
            ],
            "args": [
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-search-engine-choice-screen",
            ],
        }

        # Build context kwargs
        context_kwargs: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "locale": "en-US",
            "timezone_id": "America/New_York",
        }
        # NOTE: No proxy for bootstrap — user's residential IP is fine for CF.
        # The proxy is only needed on Railway (data center IP). Local Chrome
        # on a home/office IP passes CF naturally once --enable-automation is removed.

        browser: Browser = pw.chromium.launch(**launch_kwargs)
        context: BrowserContext = browser.new_context(**context_kwargs)

        # Patch navigator.webdriver so CF's JS fingerprint check passes
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
                configurable: true,
            });
            // Remove Chrome automation-related properties
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
        """)

        page: Page = context.new_page()

        # ── Step 1: Load login page ───────────────────────────────────────────
        print(f"Step 1: Loading {LOGIN_URL} ...")
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
        _wait_clear(page, timeout_ms=90000, label="bstock.com")
        page.wait_for_timeout(2000)
        print(f"  URL: {page.url}")

        # ── Check if already logged in ────────────────────────────────────────
        if page.locator("a[href*='/buy/listings/details/']").count() > 0:
            print("Already logged in — skipping login flow")
        else:
            # ── Step 2: Fill email ────────────────────────────────────────────
            print(f"\nStep 2: Enter email ({email})")
            email_filled = False
            for sel in ["input[name='email']", "input[type='email']", "input[placeholder*='mail' i]"]:
                try:
                    loc = page.locator(sel).first
                    loc.wait_for(state="visible", timeout=15000)
                    loc.click()
                    page.wait_for_timeout(400)
                    loc.fill(email)
                    page.wait_for_timeout(300)
                    email_filled = True
                    print(f"  Filled email using: {sel}")
                    break
                except Exception:
                    continue

            if not email_filled:
                print(f"  FATAL: email input not found. Title={page.title()} URL={page.url}")
                _save_and_upload(context)
                context.close()
                browser.close()
                return

            # Click continue/Let's go
            for btn_sel in [
                "button:has-text(\"Let's go\")",
                "button:has-text('Continue')",
                "button:has-text('Next')",
                "button[type='submit']",
            ]:
                try:
                    btn = page.locator(btn_sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        print(f"  Clicked: {btn_sel}")
                        break
                except Exception:
                    continue

            page.wait_for_timeout(2000)

            # ── Step 3: Password (may involve auth.bstock.com redirect) ───────
            print(f"\nStep 3: Waiting for password page (current: {page.url[:60]})")
            # auth.bstock.com also has CF — wait generously
            _wait_clear(page, timeout_ms=120000, label="password page")
            page.wait_for_timeout(2000)
            print(f"  URL after wait: {page.url}")

            pw_filled = False
            for pw_sel in ["input[name='password']", "input[type='password']"]:
                try:
                    loc = page.locator(pw_sel).first
                    loc.wait_for(state="visible", timeout=20000)
                    loc.click()
                    page.wait_for_timeout(400)
                    loc.fill(password)
                    page.wait_for_timeout(300)
                    pw_filled = True
                    print(f"  Filled password using: {pw_sel}")
                    break
                except Exception:
                    continue

            if not pw_filled:
                print(f"  FATAL: password input not found. URL={page.url} Title={page.title()}")
                _save_and_upload(context)
                context.close()
                browser.close()
                return

            for btn_sel in [
                "button:has-text('Sign in')",
                "button:has-text('Log in')",
                "button:has-text(\"Let's go\")",
                "button[type='submit']",
            ]:
                try:
                    btn = page.locator(btn_sel).first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        print(f"  Clicked: {btn_sel}")
                        break
                except Exception:
                    continue

            # ── Step 4: Wait for redirect back to bstock.com ──────────────────
            print("\nStep 4: Waiting for login redirect...")
            try:
                page.wait_for_url("*bstock.com/**", timeout=45000)
            except Exception:
                pass
            _wait_clear(page, timeout_ms=30000)
            page.wait_for_timeout(2000)
            print(f"  Landed on: {page.url}")

        # ── Step 5: Verify listings ────────────────────────────────────────────
        if "all-auctions" not in page.url:
            print("\nStep 5: Navigating to listings...")
            page.goto(LISTINGS_URL, wait_until="domcontentloaded", timeout=60000)
            _wait_clear(page, timeout_ms=45000)
            page.wait_for_timeout(3000)

        card_count = page.locator("a[href*='/buy/listings/details/']").count()
        print(f"\nListing cards visible: {card_count}")
        if card_count == 0:
            print(f"  WARNING: 0 listings — may not be logged in. URL={page.url}")

        _save_and_upload(context)
        context.close()
        browser.close()


def _save_and_upload(context: BrowserContext) -> None:
    """Save storage state and upload to Railway."""
    print("\nCapturing storage state...")
    context.storage_state(path=str(OUT))
    raw = OUT.read_bytes()

    # Inspect what we got
    state = json.loads(raw)
    cookies = state.get("cookies", [])
    print(f"  Cookies: {len(cookies)}")
    bstock_cookies = [c["name"] for c in cookies if "bstock" in c["domain"]]
    print(f"  bstock.com cookies: {bstock_cookies}")

    b64 = base64.b64encode(raw).decode()
    print(f"  Saved {len(raw)} bytes to {OUT}")

    # Upload to Railway
    print("\nUploading to Railway...")
    result = subprocess.run(
        ["railway", "variables", "--set", f"BSTOCK_STORAGE_STATE_B64={b64}"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True,
        text=True,
        timeout=90,
    )
    if result.returncode == 0:
        print("[OK] BSTOCK_STORAGE_STATE_B64 uploaded to Railway!")
    else:
        print(f"[WARN] Railway upload failed: {result.stderr[:300]}")
        blob_file = Path("bstock_storage_b64.txt")
        blob_file.write_text(b64)
        print(f"Blob saved to {blob_file} ({len(b64)} chars)")
        print("Run manually: railway variables --set \"BSTOCK_STORAGE_STATE_B64=$(cat bstock_storage_b64.txt)\"")


if __name__ == "__main__":
    main()
