#!/usr/bin/env python3
"""
Drop a Dropbox API access token into bot_config.json's backup section,
without it ever appearing in a chat log, shell history, or this repo.

Usage: run this ON the box (or wherever bot_config.json actually lives),
from the yoto-manager directory:

    python3 set_dropbox_token.py

It prompts for the token with input hidden (getpass), same as a password
prompt, and writes it into bot_config.json's backup.dropbox_api_token
field. Everything else in the file is left untouched.
"""
import getpass
import json
import sys
from pathlib import Path

CONFIG_PATH = Path("bot_config.json")


def main() -> int:
    if not CONFIG_PATH.exists():
        print(f"error: {CONFIG_PATH} not found in the current directory.", file=sys.stderr)
        print("Run this from the yoto-manager service directory.", file=sys.stderr)
        return 1

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    backup_cfg = cfg.setdefault("backup", {})

    token = getpass.getpass("Dropbox API access token (input hidden): ").strip()
    if not token:
        print("Empty token, nothing written.", file=sys.stderr)
        return 1

    backup_cfg["dropbox_api_token"] = token
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    print("Saved. backup.dropbox_api_token is now set.")
    print(f"backup.backend is currently: {backup_cfg.get('backend', 'local')!r}")
    if backup_cfg.get("backend") != "dropbox_api":
        print("Note: backend is not yet 'dropbox_api' -- the token alone doesn't")
        print("switch backups over. Flip backend to 'dropbox_api' when ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
