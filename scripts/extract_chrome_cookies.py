"""Extract bstock.com cookies from your real Chrome browser and upload to Railway.

Steps:
  1. Log into bstock.com in your regular Chrome browser (any tab)
  2. Close ALL Chrome windows completely (so the cookie DB isn't locked)
  3. Run this script: python scripts/extract_chrome_cookies.py
  4. Done — cookies are uploaded to Railway automatically

No Playwright needed. No CF issues. Just your real Chrome.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

try:
    import win32crypt
    from Crypto.Cipher import AES
except ImportError:
    print("Installing required packages...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pycryptodome", "pywin32", "-q"])
    import win32crypt
    from Crypto.Cipher import AES

OUT = Path("bstock_storage.json")

CHROME_USER_DATA = Path.home() / "AppData/Local/Google/Chrome/User Data"
COOKIES_DB = CHROME_USER_DATA / "Default/Network/Cookies"
LOCAL_STATE = CHROME_USER_DATA / "Local State"


def get_chrome_aes_key() -> bytes:
    with open(LOCAL_STATE, "r", encoding="utf-8") as f:
        ls = json.loads(f.read())
    encrypted_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
    encrypted_key = encrypted_key[5:]  # strip 'DPAPI' prefix
    return win32crypt.CryptUnprotectData(encrypted_key, None, None, None, 0)[1]


def decrypt_cookie_value(aes_key: bytes, enc_value: bytes) -> str:
    if not enc_value:
        return ""
    if enc_value[:3] == b"v10" or enc_value[:3] == b"v11":
        iv = enc_value[3:15]
        payload = enc_value[15:-16]
        tag = enc_value[-16:]
        cipher = AES.new(aes_key, AES.MODE_GCM, nonce=iv)
        try:
            return cipher.decrypt_and_verify(payload, tag).decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        return win32crypt.CryptUnprotectData(enc_value, None, None, None, 0)[1].decode("utf-8")
    except Exception:
        return ""


def chrome_time_to_unix(chrome_time: int) -> float:
    """Chrome timestamp (microseconds since 1601-01-01) → Unix timestamp."""
    if chrome_time == 0:
        return 0.0
    return (chrome_time / 1_000_000) - 11644473600


def extract_bstock_cookies() -> list[dict[str, Any]]:
    if not COOKIES_DB.exists():
        raise FileNotFoundError(f"Chrome cookie DB not found: {COOKIES_DB}")

    # Copy the DB since Chrome might have it locked
    tmp = Path(tempfile.mkdtemp()) / "Cookies"
    shutil.copy2(str(COOKIES_DB), str(tmp))

    aes_key = get_chrome_aes_key()

    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
        SELECT host_key, name, encrypted_value, path, expires_utc,
               is_secure, is_httponly, samesite, source_scheme
        FROM cookies
        WHERE host_key LIKE '%bstock%' OR host_key LIKE '%auth.bstock%'
        ORDER BY host_key, name
    """)
    rows = cur.fetchall()
    conn.close()

    cookies = []
    for row in rows:
        value = decrypt_cookie_value(aes_key, row["encrypted_value"])
        # Map Chrome's same_site values
        samesite_map = {-1: "None", 0: "None", 1: "Lax", 2: "Strict"}
        same_site = samesite_map.get(row["samesite"], "None")

        cookies.append({
            "name": row["name"],
            "value": value,
            "domain": row["host_key"],
            "path": row["path"] or "/",
            "expires": chrome_time_to_unix(row["expires_utc"]),
            "httpOnly": bool(row["is_httponly"]),
            "secure": bool(row["is_secure"]),
            "sameSite": same_site,
        })

    return cookies


def build_storage_state(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a Playwright-compatible storage_state dict."""
    return {
        "cookies": cookies,
        "origins": [],
    }


def main() -> None:
    print(f"\n{'='*60}")
    print("BSTOCK CHROME COOKIE EXTRACTOR")
    print(f"{'='*60}")
    print("Make sure you are logged into bstock.com in Chrome")
    print("AND that all Chrome windows are closed before proceeding.")
    print()

    try:
        cookies = extract_bstock_cookies()
    except PermissionError:
        print("ERROR: Chrome cookie DB is locked — close all Chrome windows first.")
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    if not cookies:
        print("ERROR: No bstock.com cookies found in Chrome.")
        print()
        print("To fix:")
        print("  1. Open Chrome and go to https://bstock.com/acct/signin")
        print("  2. Log in with marketingteam@nickient.com")
        print("  3. Close ALL Chrome windows")
        print("  4. Run this script again")
        sys.exit(1)

    print(f"Found {len(cookies)} bstock.com cookies:")
    auth_signals = []
    for c in cookies:
        print(f"  [{c['domain']}] {c['name']} (httpOnly={c['httpOnly']})")
        if any(x in c["name"].lower() for x in ["session", "token", "auth", "login"]):
            auth_signals.append(c["name"])

    if auth_signals:
        print(f"\nAuth cookies detected: {auth_signals}")
    else:
        print("\nWARNING: No obvious auth cookies — you may not be fully logged in.")

    state = build_storage_state(cookies)
    raw = json.dumps(state, indent=2).encode("utf-8")
    OUT.write_bytes(raw)
    b64 = base64.b64encode(raw).decode()

    print(f"\n[OK] Saved {len(raw)} bytes to {OUT}")
    print("Uploading to Railway...")

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
        print(f"Blob saved to {blob_file}")
        print('Run: railway variables --set "BSTOCK_STORAGE_STATE_B64=$(cat bstock_storage_b64.txt)"')

    print("\nDone! Railway will use these cookies on the next /run trigger.")


if __name__ == "__main__":
    main()
