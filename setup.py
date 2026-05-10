#!/usr/bin/env python3
"""
Yoto Setup — generates credentials.json for the sync script.

Run once after you receive your Client ID from yoto.dev:
  python setup.py
"""

import json
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

CREDS_FILE = Path(__file__).parent / "credentials.json"
AUTH_BASE  = "https://login.yotoplay.com"
SCOPE      = "openid profile offline_access"


def main():
    print("🎧  Yoto Setup")
    print("═" * 40)
    client_id = input("Paste your Yoto Client ID: ").strip()
    if not client_id:
        print("No Client ID entered. Exiting.")
        sys.exit(1)

    # Request device code
    print("\n🔄  Requesting device code…")
    data = urllib.parse.urlencode({
        "client_id": client_id,
        "scope":     SCOPE,
    }).encode()
    req = urllib.request.Request(
        f"{AUTH_BASE}/oauth/device/code",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())

    verify_url = resp.get("verification_uri_complete") or resp.get("verification_uri")
    user_code  = resp["user_code"]
    device_code = resp["device_code"]
    interval   = resp.get("interval", 5)

    print(f"\n1. Open this URL in your browser:\n   {verify_url}")
    print(f"\n2. Enter this code if prompted: {user_code}")
    print(f"\nWaiting for you to authorise…")

    # Poll for token
    while True:
        time.sleep(interval)
        try:
            poll_data = urllib.parse.urlencode({
                "grant_type":  "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id":   client_id,
            }).encode()
            poll_req = urllib.request.Request(
                f"{AUTH_BASE}/oauth/token",
                data=poll_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(poll_req) as r:
                token_resp = json.loads(r.read())

            if "access_token" in token_resp:
                creds = {
                    "client_id":     client_id,
                    "access_token":  token_resp["access_token"],
                    "refresh_token": token_resp.get("refresh_token", ""),
                }
                with open(CREDS_FILE, "w") as f:
                    json.dump(creds, f, indent=2)
                print(f"\n✅  Authenticated! Credentials saved to {CREDS_FILE.name}")
                print("\nYou can now run the sync script:")
                print("  python sync.py           # dry run")
                print("  python sync.py --apply   # upload missing tracks")
                break

            error = token_resp.get("error", "")
            if error not in ("authorization_pending", "slow_down"):
                print(f"\n❌  Auth failed: {token_resp.get('error_description', error)}")
                sys.exit(1)

        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_json = json.loads(body)
                error = err_json.get("error", "")
                if error in ("authorization_pending", "slow_down"):
                    continue
                print(f"\n❌  {err_json.get('error_description', error)}")
            except Exception:
                print(f"\n❌  HTTP {e.code}: {body}")
            sys.exit(1)


if __name__ == "__main__":
    main()
