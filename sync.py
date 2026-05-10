#!/usr/bin/env python3
"""
Yoto ↔ Dropbox Sync
====================
Compares your local Dropbox "Yoto Cards" folder with your Yoto library
and uploads any audio files that aren't in Yoto yet.

Usage:
  python sync.py            # dry run — shows what would change
  python sync.py --apply    # actually upload missing tracks
  python sync.py --status   # show current library status only

Credentials are read from credentials.json (next to this script).
Run setup.py first to authenticate and generate credentials.json.
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

# ── CONFIG ──────────────────────────────────────────────────────────────
SCRIPT_DIR    = Path(__file__).parent
DROPBOX_CARDS = SCRIPT_DIR.parent / "Yoto Cards"
CREDS_FILE    = SCRIPT_DIR / "credentials.json"
API_BASE      = "https://api.yotoplay.com"
AUTH_BASE     = "https://login.yotoplay.com"

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg"}

# Cards whose name contains these strings belong to that child.
# Everything else is treated as shared.
PERSONAL_PATTERNS = {
    "elijah": ["elijah"],
    "lev":    ["lev"],
}


# ── CREDENTIALS ─────────────────────────────────────────────────────────
def load_credentials():
    if not CREDS_FILE.exists():
        print(f"❌  No credentials found at {CREDS_FILE}")
        print("    Run: python setup.py")
        sys.exit(1)
    with open(CREDS_FILE) as f:
        return json.load(f)


def save_credentials(creds):
    with open(CREDS_FILE, "w") as f:
        json.dump(creds, f, indent=2)


def refresh_token(creds):
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id":     creds["client_id"],
    }).encode()
    req = urllib.request.Request(
        f"{AUTH_BASE}/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    creds["access_token"] = resp["access_token"]
    if "refresh_token" in resp:
        creds["refresh_token"] = resp["refresh_token"]
    save_credentials(creds)
    return creds


# ── API ──────────────────────────────────────────────────────────────────
def api_call(creds, path, method="GET", body=None):
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {
        "Authorization": f"Bearer {creds['access_token']}",
        "Content-Type":  "application/json",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            text = r.read()
            return json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Try refreshing
            creds = refresh_token(creds)
            headers["Authorization"] = f"Bearer {creds['access_token']}"
            req2 = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req2) as r:
                text = r.read()
                return json.loads(text) if text else None
        raise


def get_upload_url(creds, filepath: Path, filename: str):
    """Get a signed S3 upload URL. Requires SHA256 of the file."""
    import hashlib
    with open(filepath, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()
    params = urllib.parse.urlencode({"sha256": sha256, "filename": filename})
    return api_call(creds, f"/media/transcode/audio/uploadUrl?{params}")


def upload_file(upload_url, filepath, content_type="audio/mpeg"):
    with open(filepath, "rb") as f:
        file_data = f.read()
    req = urllib.request.Request(
        upload_url,
        data=file_data,
        headers={"Content-Type": content_type},
        method="PUT",
    )
    with urllib.request.urlopen(req) as r:
        return r.status


def update_card(creds, card_id, card_title, chapters):
    return api_call(creds, "/content", "POST", {
        "cardId":   card_id,
        "content":  {"chapters": chapters},
        "metadata": {"title": card_title},
    })


# ── HELPERS ──────────────────────────────────────────────────────────────
CONTENT_TYPES = {
    ".mp3":  "audio/mpeg",
    ".m4a":  "audio/mp4",
    ".aac":  "audio/aac",
    ".wav":  "audio/wav",
    ".flac": "audio/flac",
    ".ogg":  "audio/ogg",
}


def get_content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "audio/mpeg")


def clean_track_name(filename: str) -> str:
    """Turn '01-dan_auerbach-run_that_race.mp3' into 'Dan Auerbach - Run That Race'."""
    name = Path(filename).stem
    # Strip leading track number
    import re
    name = re.sub(r"^\d+[-_.\s]+", "", name)
    # Replace underscores/hyphens with spaces
    name = name.replace("_", " ").replace("-", " - ").strip()
    # Title case
    return name.title()


def assign_owner(card_title: str) -> str:
    t = card_title.lower()
    for owner, patterns in PERSONAL_PATTERNS.items():
        if any(p in t for p in patterns):
            return owner
    return "shared"


def format_size(n: int) -> str:
    if n < 1024:       return f"{n} B"
    if n < 1024**2:    return f"{n/1024:.0f} KB"
    return f"{n/1024**2:.1f} MB"


def scan_dropbox(base_dir: Path) -> dict:
    """
    Returns a dict: { folder_name: [Path, ...] }
    for every subfolder of base_dir that contains audio files.
    """
    result = {}
    if not base_dir.exists():
        return result
    for folder in sorted(base_dir.iterdir()):
        if not folder.is_dir():
            continue
        audio_files = sorted([
            f for f in folder.rglob("*")
            if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
        ])
        if audio_files:
            result[folder.name] = audio_files
    return result


def get_existing_tracks(card) -> set:
    """Return a set of track titles already in a Yoto card."""
    chapters = card.get("content", {}).get("chapters") or card.get("chapters") or []
    return {(ch.get("title") or ch.get("key") or "").lower() for ch in chapters}


# ── MAIN ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sync Dropbox Yoto Cards to Yoto library")
    parser.add_argument("--apply",  action="store_true", help="Upload missing tracks (default is dry run)")
    parser.add_argument("--status", action="store_true", help="Show library status and exit")
    args = parser.parse_args()

    creds = load_credentials()

    print("🔄  Loading Yoto library…")
    data  = api_call(creds, "/content/mine")
    cards = data.get("cards") or data or []
    print(f"    Found {len(cards)} cards in your Yoto library.\n")

    if args.status:
        print_status(cards)
        return

    print(f"📁  Scanning Dropbox folder: {DROPBOX_CARDS}")
    dropbox_folders = scan_dropbox(DROPBOX_CARDS)
    print(f"    Found {len(dropbox_folders)} folders with audio.\n")

    if not args.apply:
        print("ℹ️   DRY RUN — no changes will be made. Use --apply to upload.\n")
        print("─" * 60)

    total_missing   = 0
    total_uploaded  = 0
    total_errors    = 0

    for folder_name, audio_files in dropbox_folders.items():
        # Try to find matching Yoto card by name
        matching_card = next(
            (c for c in cards if folder_name.lower() in
             (c.get("title") or c.get("metadata", {}).get("title") or "").lower()),
            None
        )

        owner = assign_owner(folder_name)
        owner_label = f"[{owner.upper()}]" if owner != "shared" else "[SHARED]"

        if not matching_card:
            print(f"📭  {owner_label} '{folder_name}' — no matching Yoto card found (skipping)")
            continue

        card_id    = matching_card.get("id") or matching_card.get("cardId")
        card_title = matching_card.get("title") or matching_card.get("metadata", {}).get("title", folder_name)
        existing   = get_existing_tracks(matching_card)
        chapters   = list(matching_card.get("content", {}).get("chapters") or matching_card.get("chapters") or [])

        missing = [f for f in audio_files if clean_track_name(f.name).lower() not in existing]

        if not missing:
            print(f"✅  {owner_label} '{card_title}' — up to date ({len(audio_files)} tracks)")
            continue

        total_missing += len(missing)
        print(f"⚠️   {owner_label} '{card_title}' — {len(missing)} track(s) missing:")
        for f in missing:
            size = format_size(f.stat().st_size)
            print(f"      + {clean_track_name(f.name)} ({size})")

        if args.apply:
            print(f"    Uploading…")
            for f in missing:
                track_name   = clean_track_name(f.name)
                content_type = get_content_type(f)
                try:
                    # Step 1: Get signed upload URL (Yoto requires SHA256)
                    upload_info = get_upload_url(creds, f, f.name)
                    upload_url  = upload_info.get("uploadUrl")
                    upload_id   = upload_info.get("uploadId")

                    # Step 2: Upload only if Yoto doesn't already have this file
                    if upload_url:
                        upload_file(upload_url, f, content_type)

                    # Step 3: Add chapter using uploadId as the key
                    chapters.append({"title": track_name, "key": upload_id})
                    total_uploaded += 1
                    print(f"      ✓ {track_name}")
                    time.sleep(0.5)  # Be polite to the API
                except Exception as e:
                    total_errors += 1
                    print(f"      ✗ {track_name}: {e}")

            if total_uploaded > 0:
                try:
                    update_card(creds, card_id, card_title, chapters)
                    print(f"    💾 Card updated.")
                except Exception as e:
                    print(f"    ✗ Failed to save card: {e}")

        print()

    print("─" * 60)
    if args.apply:
        print(f"✅  Done. {total_uploaded} track(s) uploaded, {total_errors} error(s).")
    else:
        print(f"📋  Summary: {total_missing} track(s) would be uploaded.")
        print(f"    Run with --apply to upload them.")


def print_status(cards):
    print(f"{'OWNER':<10} {'CARD TITLE':<40} {'TRACKS':>6}")
    print("─" * 60)
    for card in sorted(cards, key=lambda c: (
        assign_owner(c.get("title") or c.get("metadata", {}).get("title") or ""),
        c.get("title") or ""
    )):
        title  = card.get("title") or card.get("metadata", {}).get("title") or "Untitled"
        owner  = assign_owner(title)
        tracks = len(card.get("content", {}).get("chapters") or card.get("chapters") or [])
        print(f"{owner.upper():<10} {title[:40]:<40} {tracks:>6}")
    print("─" * 60)
    print(f"Total: {len(cards)} cards")


if __name__ == "__main__":
    main()
