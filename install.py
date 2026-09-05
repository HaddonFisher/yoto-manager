#!/usr/bin/env python3
"""
Yoto Manager — interactive install / reconfigure

Collects the core bot config (Telegram token, owner chat ID, allowlists),
walks each feature flag (apple_music_enabled; backup.enabled and its
backend/destination) asking whether to turn it on and only then asking
for that feature's own inputs, writes bot_config.json, and then actually
validates everything with real calls -- not just "the file looks right".

Safe to re-run against an existing install: current values are shown
(secrets masked), pressing Enter keeps them, and nothing is written to
disk until you explicitly confirm a summary of what will change. The
previous config (if any) is backed up alongside the new one.

Usage:
    python3 install.py                  # operate on ./bot_config.json
    python3 install.py --config PATH    # operate on a different config
                                         # file (e.g. a copy, for testing
                                         # this script itself without
                                         # touching a live install)

Absorbs the old standalone set_dropbox_token.py -- that script is gone;
setting the Dropbox token is now one step of this same flow, same
hidden-input handling, one mechanism instead of two.
"""
from __future__ import annotations

import argparse
import getpass
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DROPBOX_TOKEN_PLACEHOLDER = "REPLACE_ME"

# ── Small IO helpers ─────────────────────────────────────────────────────

def hr(char: str = "─", width: int = 60) -> None:
    print(char * width)


def banner() -> None:
    hr("═")
    print("  Yoto Manager — install / reconfigure")
    hr("═")


def mask(value) -> str:
    """Display form for a secret in summaries -- never the value itself."""
    if not value or value == DROPBOX_TOKEN_PLACEHOLDER:
        return "<not set>"
    return f"<set, {len(str(value))} chars>"


def ask_yes_no(question: str, default: bool = True) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        ans = input(question + suffix).strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("  please answer y or n")


def ask_value(label: str, current=None, default=None, secret: bool = False) -> str:
    """Prompt for a plain value. Enter keeps `current` if there is one,
    otherwise falls back to `default` (pre-filled, not hidden)."""
    if secret:
        shown = mask(current)
        prompt = f"{label} (current: {shown}, Enter to keep): "
        entered = getpass.getpass(prompt)
        return entered.strip() if entered.strip() else (current or "")
    if current is not None:
        prompt = f"{label} (current: {current}, Enter to keep): "
    elif default is not None:
        prompt = f"{label} [{default}]: "
    else:
        prompt = f"{label}: "
    entered = input(prompt).strip()
    if entered:
        return entered
    if current is not None:
        return current
    return default if default is not None else ""


# ── Config load/save ─────────────────────────────────────────────────────

def load_config(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception as e:
            print(f"⚠️  {path} exists but isn't valid JSON ({e}) — starting fresh.")
    return {}


def save_config(path: Path, cfg: dict) -> None:
    if path.exists():
        backup_path = path.with_name(f"{path.name}.bak-{int(time.time())}")
        shutil.copy2(path, backup_path)
        print(f"  (previous config backed up to {backup_path.name})")
    path.write_text(json.dumps(cfg, indent=2) + "\n")


# ── Core config collection ───────────────────────────────────────────────

def collect_core(cfg: dict) -> dict:
    print("\n── Core configuration ──")
    cfg["telegram_bot_token"] = ask_value(
        "Telegram bot token", current=cfg.get("telegram_bot_token"), secret=True,
    )
    owner = ask_value("Your Telegram chat ID (owner)", current=cfg.get("owner_chat_id"))
    try:
        owner_int = int(owner)
    except ValueError:
        print(f"  ⚠️  {owner!r} doesn't look numeric — keeping as given, but this should be a number.")
        owner_int = owner
    cfg["owner_chat_id"] = owner_int

    existing_users = cfg.get("allowed_user_ids") or ([owner_int] if owner_int else [])
    users_str = ask_value(
        "Allowed user IDs (comma-separated)",
        current=",".join(str(x) for x in existing_users) if existing_users else None,
        default=str(owner_int),
    )
    cfg["allowed_user_ids"] = [int(x.strip()) for x in users_str.split(",") if x.strip()]

    existing_sends = cfg.get("allowed_send_chat_ids") or cfg["allowed_user_ids"]
    sends_str = ask_value(
        "Allowed outbound chat IDs (comma-separated, usually same as above)",
        current=",".join(str(x) for x in existing_sends) if existing_sends else None,
        default=",".join(str(x) for x in cfg["allowed_user_ids"]),
    )
    cfg["allowed_send_chat_ids"] = [int(x.strip()) for x in sends_str.split(",") if x.strip()]
    return cfg


def note_yoto_auth(config_dir: Path) -> None:
    """Yoto's own login is a browser OAuth (PKCE) flow against the local
    dashboard -- it can't be done from a terminal prompt. This just tells
    the user how, and validation later confirms whatever's already there
    actually works."""
    token_file = config_dir / "yoto_token.json"
    print("\n── Yoto login ──")
    if token_file.exists():
        print(f"  {token_file.name} already exists — will be checked with a real API call below.")
    else:
        print(f"  {token_file.name} not found. Yoto's login is a browser step, not a prompt:")
        print("    1. Make sure the dashboard is reachable (http://localhost:8765, or tunnel")
        print("       to it: ssh -L 8765:localhost:8765 <host>, then open localhost:8765).")
        print("    2. Log in with your Yoto account; the dashboard writes yoto_token.json itself.")
        print("    3. Re-run this script (or just its validation) afterward.")


# ── Feature flags ─────────────────────────────────────────────────────────

def collect_apple_music(cfg: dict) -> dict:
    print("\n── Feature: Apple Music search (am_search) ──")
    current = cfg.get("apple_music_enabled", True)
    default_note = " (default, not explicitly set)" if "apple_music_enabled" not in cfg else ""
    print(f"  currently: {'enabled' if current else 'disabled'}{default_note}")
    if not shutil.which("osascript"):
        print("  ⚠️  osascript not found on this host (not macOS) — am_search will fail every")
        print("     time it's called if enabled here. This is exactly the bug that was fixed")
        print("     by adding this flag in the first place; leaving it off is recommended")
        print("     on a Linux deployment.")
    enabled = ask_yes_no("Enable Apple Music search?", default=current)
    cfg["apple_music_enabled"] = enabled
    return cfg


def collect_backup(cfg: dict) -> dict:
    print("\n── Feature: track backup ──")
    backup = dict(cfg.get("backup", {}))
    current_enabled = backup.get("enabled", False)
    if current_enabled:
        print(f"  currently: enabled, backend={backup.get('backend', 'local')!r}")
    else:
        print("  currently: disabled")

    enabled = ask_yes_no("Enable track backup?", default=current_enabled)
    backup["enabled"] = enabled
    if not enabled:
        # Leave backend/path/token untouched -- turning this off shouldn't
        # discard a destination you might re-enable later.
        cfg["backup"] = backup
        return cfg

    print("  Backend: (1) local folder on this machine   (2) Dropbox (via their API)")
    default_choice = "2" if backup.get("backend") == "dropbox_api" else "1"
    choice = ask_value("Choose 1 or 2", default=default_choice)
    if choice.strip() == "2":
        backup["backend"] = "dropbox_api"
        backup["dropbox_api_token"] = ask_value(
            "Dropbox API access token", current=backup.get("dropbox_api_token"), secret=True,
        ) or DROPBOX_TOKEN_PLACEHOLDER
        backup["dropbox_base_path"] = ask_value(
            "Dropbox destination folder", current=backup.get("dropbox_base_path"),
            default="/Yoto Cards",
        )
    else:
        backup["backend"] = "local"
        default_path = backup.get("path") or str(SCRIPT_DIR / "backups")
        backup["path"] = ask_value("Local backup folder path", current=backup.get("path"), default=default_path)
        backup["mode"] = backup.get("mode", "organized")

    cfg["backup"] = backup
    return cfg


# ── Summary + confirm ─────────────────────────────────────────────────────

SECRET_KEYS = {"telegram_bot_token", "dropbox_api_token"}


def _flat_view(cfg: dict, prefix: str = "") -> list[tuple[str, str]]:
    rows = []
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            rows.extend(_flat_view(v, prefix=f"{key}."))
        elif key.split(".")[-1] in SECRET_KEYS:
            rows.append((key, mask(v)))
        else:
            rows.append((key, str(v)))
    return rows


def show_summary_and_confirm(old_cfg: dict, new_cfg: dict) -> bool:
    print("\n── Summary of what will be written ──")
    old_rows = dict(_flat_view(old_cfg))
    new_rows = dict(_flat_view(new_cfg))
    for key in sorted(set(old_rows) | set(new_rows)):
        old_v = old_rows.get(key, "<absent>")
        new_v = new_rows.get(key, "<absent>")
        marker = "  " if old_v == new_v else "* "
        print(f"  {marker}{key}: {old_v} -> {new_v}")
    print()
    return ask_yes_no("Write this config?", default=True)


# ── Validation ─────────────────────────────────────────────────────────────

class Result:
    def __init__(self, name: str, status: str, detail: str):
        self.name, self.status, self.detail = name, status, detail  # status: PASS/WARN/FAIL/SKIP


def validate_telegram(token: str) -> Result:
    if not token:
        return Result("Telegram", "FAIL", "no token configured")
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/getMe")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if not data.get("ok"):
            return Result("Telegram", "FAIL", data.get("description", "getMe returned ok=false"))
        info = data["result"]
        return Result("Telegram", "PASS", f"@{info.get('username')} ({info.get('first_name')})")
    except Exception as e:
        return Result("Telegram", "FAIL", str(e))


def validate_yoto(config_dir: Path) -> Result:
    token_file = config_dir / "yoto_token.json"
    if not token_file.exists():
        return Result("Yoto", "SKIP", "yoto_token.json not present -- complete the browser login first")
    try:
        sys.path.insert(0, str(config_dir))
        import telegram_bot as tb  # noqa: local import, same directory as bot_config.json
    except Exception as e:
        return Result("Yoto", "FAIL", f"could not import telegram_bot.py to validate: {e}")
    try:
        token = tb.load_token()
    except Exception as e:
        return Result("Yoto", "FAIL", f"yoto_token.json unreadable: {e}")
    try:
        tb._card_cache = None  # force a real call, not a stale cache from a prior run
        cards = tb.fetch_cards()
        return Result("Yoto", "PASS", f"{len(cards)} card(s) in your library")
    except Exception as e:
        return Result("Yoto", "FAIL", f"API call failed: {e}")


def _dropbox_api(method: str, endpoint: str, token: str, body: bytes = b"null",
                  content_type: str = "application/json", extra_headers: dict = None):
    req = urllib.request.Request(endpoint, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", content_type)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"{}")


def validate_dropbox(token: str, base_path: str) -> Result:
    if not token or token == DROPBOX_TOKEN_PLACEHOLDER:
        return Result("Dropbox", "FAIL", "no token configured (run this script's Dropbox step, or the old set_dropbox_token.py is gone -- this replaces it)")
    try:
        account = _dropbox_api(
            "POST", "https://api.dropboxapi.com/2/users/get_current_account", token,
        )
        who = account.get("name", {}).get("display_name", "unknown account")
    except Exception as e:
        return Result("Dropbox", "FAIL", f"token rejected: {e}")

    # Prove the target path is real and writable -- write, then remove, a
    # real test file. Same proof standard as the local-path check.
    test_path = f"{base_path.rstrip('/')}/.yoto-manager-install-test"
    try:
        req = urllib.request.Request(
            "https://content.dropboxapi.com/2/files/upload",
            data=b"yoto-manager install test", method="POST",
        )
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Dropbox-API-Arg", json.dumps({
            "path": test_path, "mode": "overwrite", "mute": True,
        }))
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        return Result("Dropbox", "FAIL", f"authenticated as {who}, but could not write to {base_path!r}: {e}")

    try:
        _dropbox_api("POST", "https://api.dropboxapi.com/2/files/delete_v2", token,
                      body=json.dumps({"path": test_path}).encode())
    except Exception as e:
        return Result("Dropbox", "WARN",
                       f"wrote to {base_path!r} OK, but couldn't remove the test file ({e}) -- "
                       f"harmless, just delete {test_path} yourself")

    return Result("Dropbox", "PASS", f"account {who}; wrote + removed a test file at {base_path}")


def validate_local_backup(path_str: str) -> Result:
    try:
        p = Path(path_str)
        p.mkdir(parents=True, exist_ok=True)
        test_file = p / ".yoto-manager-install-test"
        test_file.write_text("yoto-manager install test")
        ok = test_file.read_text() == "yoto-manager install test"
        test_file.unlink()
        if not ok:
            return Result("Local backup path", "FAIL", f"wrote to {p} but read-back didn't match")
        return Result("Local backup path", "PASS", f"wrote + removed a test file in {p}")
    except Exception as e:
        return Result("Local backup path", "FAIL", str(e))


def check_ytdlp(config_dir: Path) -> Result:
    candidates = [
        "/usr/local/bin/yt-dlp", "/opt/homebrew/bin/yt-dlp",
        str(Path.home() / ".local/bin/yt-dlp"),
    ]
    binary = next((c for c in candidates if Path(c).exists()), None) or shutil.which("yt-dlp")
    if not binary:
        return Result("yt-dlp", "FAIL", "not found on PATH or in the usual install locations")
    try:
        r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=15)
        version = r.stdout.strip()
    except Exception as e:
        return Result("yt-dlp", "FAIL", f"found at {binary} but --version failed: {e}")

    try:
        y, m, d = (int(x) for x in version.split(".")[:3])
        age_days = (date.today() - date(y, m, d)).days
        if age_days > 150:
            return Result("yt-dlp", "WARN",
                           f"{version} at {binary} -- {age_days} days old. YouTube breaks old "
                           f"yt-dlp versions often (this is the exact bug fixed earlier this "
                           f"project); consider updating.")
        return Result("yt-dlp", "PASS", f"{version} at {binary} ({age_days} days old)")
    except Exception:
        return Result("yt-dlp", "PASS", f"{version} at {binary} (couldn't parse date to check freshness)")


def check_js_runtime() -> Result:
    if not shutil.which("deno"):
        return Result("JS runtime (deno)", "FAIL",
                       "not found -- current yt-dlp needs one to solve YouTube's extraction "
                       "challenge; search will silently return no results without it")
    try:
        r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=10)
        first_line = r.stdout.splitlines()[0] if r.stdout else "present"
    except Exception:
        first_line = "present"
    return Result("JS runtime (deno)", "PASS", first_line)


def check_ffmpeg() -> Result:
    if not shutil.which("ffmpeg"):
        return Result("ffmpeg", "WARN",
                       "not found -- not required by any current code path (the local "
                       "pre-transcode step was removed after measuring it made uploads "
                       "slower, not faster), kept as a check in case that's revisited")
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        first_line = r.stdout.splitlines()[0] if r.stdout else "present"
    except Exception:
        first_line = "present"
    return Result("ffmpeg", "PASS", first_line)


def run_validation(cfg: dict, config_dir: Path) -> list[Result]:
    print("\n── Validating (real calls, not just checking the file) ──")
    results = []

    results.append(validate_telegram(cfg.get("telegram_bot_token", "")))
    results.append(validate_yoto(config_dir))

    backup = cfg.get("backup", {})
    if backup.get("enabled"):
        if backup.get("backend") == "dropbox_api":
            results.append(validate_dropbox(backup.get("dropbox_api_token", ""),
                                             backup.get("dropbox_base_path", "/Yoto Cards")))
        else:
            results.append(validate_local_backup(backup.get("path", "")))
    else:
        results.append(Result("Backup", "SKIP", "feature disabled"))

    if cfg.get("apple_music_enabled"):
        if shutil.which("osascript"):
            results.append(Result("Apple Music (osascript)", "PASS", "osascript available"))
        else:
            results.append(Result("Apple Music (osascript)", "FAIL",
                                   "enabled, but osascript not found on this host"))
    else:
        results.append(Result("Apple Music (osascript)", "SKIP", "feature disabled"))

    results.append(check_ytdlp(config_dir))
    results.append(check_js_runtime())
    results.append(check_ffmpeg())
    return results


def print_results(results: list[Result]) -> bool:
    print()
    hr()
    print("  VALIDATION SUMMARY")
    hr()
    icons = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌", "SKIP": "➖"}
    for r in results:
        print(f"  {icons.get(r.status, '?')} {r.status:<4}  {r.name:<26} {r.detail}")
    hr()
    failures = [r for r in results if r.status == "FAIL"]
    if failures:
        print(f"  {len(failures)} check(s) failed. Fix the above and re-run this script")
        print("  (it's safe to re-run -- your existing answers are shown as current values).")
    else:
        print("  All required checks passed.")
    hr()
    return not failures


# ── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "bot_config.json",
                         help="Path to bot_config.json (default: alongside this script). "
                              "Point this at a copy to test the installer without touching "
                              "a live config.")
    args = parser.parse_args()

    config_path = args.config.resolve()
    config_dir = config_path.parent

    banner()
    print(f"Config file: {config_path}")
    if config_path.exists():
        print("(existing config found -- current values will be shown, Enter keeps them)")

    old_cfg = load_config(config_path)
    cfg = json.loads(json.dumps(old_cfg))  # deep copy to diff against later

    cfg = collect_core(cfg)
    note_yoto_auth(config_dir)
    cfg = collect_apple_music(cfg)
    cfg = collect_backup(cfg)

    if not show_summary_and_confirm(old_cfg, cfg):
        print("\nNothing written. Re-run any time.")
        return 1

    save_config(config_path, cfg)
    print(f"  ✅  wrote {config_path}")

    results = run_validation(cfg, config_dir)
    ok = print_results(results)
    return 0 if ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EOFError, KeyboardInterrupt):
        print("\n\nCancelled -- nothing was written beyond what you'd already confirmed.")
        raise SystemExit(1)
