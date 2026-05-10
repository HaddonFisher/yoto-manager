# Yoto Manager

A local dashboard and Telegram bot for managing your Yoto card playlists — add songs and audio from YouTube without touching a laptop.

> **How it works:** You run a small Python server on your Mac. It opens a web dashboard in your browser and starts a Telegram bot. From Telegram, you (or anyone in your family group) can search for a song, pick a result, and it gets downloaded and uploaded to a Yoto playlist — all in a few taps.

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

Copy the example config file:

```
cp bot_config.json.example bot_config.json
```

Open `bot_config.json` in any text editor and fill in your values:

```json
{
  "telegram_bot_token": "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ",
  "allowed_group_id": -987654321
}
```

- `telegram_bot_token` — the token from BotFather
- `allowed_group_id` — the chat ID of your Telegram group (the bot ignores messages from anywhere else)

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
- Your Yoto token may have expired — run `python3 setup.py` again to re-authenticate
- Check that ffmpeg is installed and working: `ffmpeg -version`
- The dashboard at `http://localhost:8765` shows recent activity and errors

**The dashboard says "Not authenticated"**
- Run `python3 setup.py` to generate a fresh `yoto_token.json`

---

## Files overview

| File | Purpose |
|---|---|
| `server.py` | The local web server and API proxy |
| `telegram_bot.py` | The Telegram bot logic |
| `index.html` | The web dashboard |
| `setup.py` | Run once to authenticate with Yoto |
| `sync.py` | Standalone script for bulk syncing (optional) |
| `bot_config.json.example` | Template for your bot configuration |
| `Start Yoto Manager.command` | Double-click to launch |
| `restart_server.command` | Double-click to restart after a crash |

---

## Privacy & security

- Everything runs **locally on your Mac** — no cloud server, no third-party service beyond Yoto and Telegram themselves
- Your Yoto credentials (`yoto_token.json`) and bot token (`bot_config.json`) are stored only on your machine and are excluded from version control
- The bot only responds to the Telegram group you specify — messages from anywhere else are silently ignored
