# Yoto Manager

**Yoto Manager** is a local web dashboard and Telegram bot for managing your children's [Yoto](https://yotoplay.com) audio player cards. It runs entirely on your Mac — no cloud account needed beyond Yoto and Telegram themselves.

With Yoto Manager you can:

- **Search Apple Music** for a song and add it straight to a Yoto card playlist — from your phone, via Telegram
- **Download from YouTube** — paste a URL or search by name; the audio is downloaded, converted, and uploaded automatically
- **Upload your own audio files** through the web dashboard (great for audiobooks, homemade recordings, or anything you already have)
- **Manage card playlists** — create new playlists, browse tracks, and organise your cards from a clean local web interface
- **Control everything from Telegram** — you (or anyone in your family group) can add songs in a few taps without opening a laptop

It works by running a small Python HTTP server on your Mac. That server powers the web dashboard at `http://localhost:8765` and keeps the Telegram bot running in the background.

> **Who is this for?** Parents who want an easy way to keep Yoto cards fresh without manually dragging files around. Once it's set up, adding a new song to a card is just a Telegram message.

---

> **How it works in practice:** Open Telegram, type `/find Hey Jude`, pick the result you want, choose which Yoto card it goes on, and you're done — the bot handles the download and upload while you get on with your day.

---

## What you'll need

- **Python 3.10 or later** — check by running `python3 --version` in Terminal
- **ffmpeg** — used to convert audio. Install via [Homebrew](https://brew.sh): `brew install ffmpeg`
- **A Yoto account** with cards already set up
- **A Telegram account** and a bot token (see below)

---

## Installation

### 1. Get the files

Either [download this repo as a ZIP](../../archive/refs/heads/main.zip) and unzip it somewhere convenient (like your Desktop or Documents folder), or clone it:

```
git clone https://github.com/YOUR_USERNAME/yoto-manager.git
cd yoto-manager
```

### 2. Authenticate with Yoto

Run the setup script once. It will walk you through authorising your Yoto account:

```
python3 setup.py
```

It will ask you for a **Yoto Client ID** and then open a URL in your browser. Follow the prompts to log in. Your credentials are saved locally to `yoto_token.json` (this file is never shared or uploaded).

### 3. Create your Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the steps — pick a name and a username
3. BotFather will give you a **bot token** that looks like `1234567890:ABCdef...`
4. Create or open a Telegram group that includes your new bot

You also need your **group's chat ID**. The easiest way: add `@userinfobot` to your group, it will reply with the group ID (a negative number like `-987654321`).

### 4. Configure the bot

Run the interactive installer:

```
python3 install.py
```

It collects your Telegram token and chat ID, walks you through each optional
feature (Apple Music search, track backup — local folder or Dropbox) asking
whether to turn it on and only then asking for that feature's own details,
writes `bot_config.json`, and then actually validates everything with real
calls (Telegram's `getMe`, a real Yoto API call, a real Dropbox write+delete
or local write+delete test, and checks that `yt-dlp`/a JS runtime/`ffmpeg`
are present) rather than just trusting the file looks right.

It's safe to re-run any time — against an existing install it shows your
current values (secrets masked) and pressing Enter keeps them; nothing is
written until you confirm a summary of exactly what will change, and your
previous config is backed up alongside the new one. Use `python3 install.py
--config /path/to/other/bot_config.json` to point it at a different file.

(If you'd rather edit the file by hand: copy `bot_config.json.example` to
`bot_config.json` and fill in the fields it documents — `install.py` writes
the same shape, it just validates as it goes.)

---

## Starting the server

Double-click **Start Yoto Manager.command** in Finder.

> On first run, macOS may warn you it can't verify the file. Right-click it, choose **Open**, and confirm. You only need to do this once.

A Terminal window will open and the dashboard will load in your browser at `http://localhost:8765`. Keep that window open while you're using the bot — closing it stops everything.

To restart after a crash or update, double-click **restart_server.command** instead.

---

## Daily use — Telegram commands

Send these in your Telegram group:

| Command | What it does |
|---|---|
| `/find The Beatles Hey Jude` | Searches Apple Music then YouTube. You'll get a list of results to pick from, then choose which Yoto playlist to add it to. |
| `/find Hey Jude \| Peppa Pig Mix` | Same search, but with the Yoto playlist already specified — skips the playlist-picking step. |
| `/findplay Party Favourites` | Searches YouTube for **playlists** matching that query. You can browse tracks or add all of them at once. |
| `/findplay Party Favourites \| Peppa Pig Mix` | Same, with a Yoto playlist pre-specified. |
| `/create Road Trip Songs` | Creates a new empty Yoto playlist with that name. |
| `/retry` | Repeats your last `/find`, `/findplay`, or `/create` command — handy if it timed out. |
| `/help` | Shows a summary of all commands. |

After picking a track, the bot downloads it, converts it to the right format, and uploads it to Yoto. The whole process usually takes 15–30 seconds. You'll get a confirmation message when it's done.

---

## Troubleshooting

**The server won't start**
- Make sure Python 3.10+ is installed: `python3 --version`
- Check that `ffmpeg` is installed: `ffmpeg -version`
- If the port is in use, another copy may already be running — use `restart_server.command` instead

**The bot isn't responding in Telegram**
- Make sure the server is still running (the Terminal window is open)
- Check that your `bot_config.json` has the correct token and group ID
- The group ID must be negative (e.g. `-987654321`) — if it's positive, it's a user ID, not a group
- Make sure the bot has been added to the group as a member

**Uploads are failing**
- Your Yoto token may have expired — log in again via the dashboard at `http://localhost:8765`, then run `python3 install.py` to confirm it with a real API call
- The dashboard shows recent activity and errors

**The dashboard says "Not authenticated"**
- Log in via the dashboard at `http://localhost:8765` (it writes `yoto_token.json` itself); `python3 install.py` will confirm it worked

---

## Files overview

| File | Purpose |
|---|---|
| `server.py` | The local web server and API proxy |
| `telegram_bot.py` | The Telegram bot logic |
| `index.html` | The web dashboard |
| `install.py` | Interactive install/reconfigure — collects config, walks each feature flag, validates everything with real calls |
| `setup.py` | Authenticates the separate standalone `sync.py` script (not the bot itself — see below) |
| `sync.py` | Standalone script for bulk syncing (optional) |
| `bot_config.json.example` | Template for your bot configuration, documenting every field `install.py` can write |
| `Start Yoto Manager.command` | Double-click to launch |
| `restart_server.command` | Double-click to restart after a crash |

---

## Privacy & security

- Everything runs **locally on your Mac** — no cloud server, no third-party service beyond Yoto and Telegram themselves
- Your Yoto credentials (`yoto_token.json`) and bot token (`bot_config.json`) are stored only on your machine and are excluded from version control
- The bot only responds to the Telegram group you specify — messages from anywhere else are silently ignored
