# Yoto Manager

**Yoto Manager** is a Telegram bot (plus a small local dashboard) for adding music and audio to your [Yoto](https://yotoplay.com) cards. Search YouTube (and, optionally, your Mac's Apple Music library) from your phone, pick a result, choose a card, and the bot downloads, tags, and uploads it — no laptop, no dragging files around.

It runs as a small Python process on one machine — your Mac, or a headless Linux box — with a web dashboard at `http://localhost:8765` for logging in to Yoto and checking on things, and a Telegram bot that does the actual day-to-day work.

---

## How it works

**Who can use it:** one owner account. `bot_config.json` sets an `owner_chat_id` and an `allowed_user_ids` list; the bot only acts on messages from those IDs and silently ignores everything else. This is a direct-message bot, not a group bot — there's no group-chat mode.

**Commands**, sent as a DM to the bot:

| Command | What it does |
|---|---|
| `/find Song Title` | Searches (Apple Music first if that feature's enabled, otherwise YouTube), shows a list to pick from (tap to select, tap Done), then asks which Yoto card to add it to. |
| `/find Song Title \| Card Name` | Same search, but with the card name given up front — skips the card-picking step if it matches exactly. |
| `/findplay Some Playlist` | Searches YouTube for **playlists** matching the query; browse individual tracks or add the whole thing. |
| `/findplay Some Playlist \| Card Name` | Same, with the card pre-specified. |
| `/create Card Name` | Creates a new, empty Yoto card. |
| `/retry` | Repeats your last `/find`, `/findplay`, or `/create`. |
| `/help` | Lists the commands. |
| `/restart` | Restarts the server process, picking up any code changes. |

**The flow, end to end:** you pick tracks from the search results, the bot downloads the audio (from YouTube — see below for what "Apple Music" actually means here), uploads it to Yoto, and Yoto transcodes it server-side; you get a confirmation message per track. Selecting several tracks at once downloads them in parallel and uploads them as each one finishes, rather than one at a time.

**Setup:** run `python3 install.py`. It asks for your Telegram bot token and your own numeric chat ID (message something like `@userinfobot` to get it), walks you through the optional features below, writes `bot_config.json`, and then actually checks everything works — a real Telegram API call, a real Yoto API call, and so on — rather than just saving the file. It's safe to re-run any time to change settings or re-check things; see its own `--help` and in-script guidance for details, including how to complete Yoto's one-time browser login (a Yoto account and cards need to already exist — `install.py` doesn't create those).

---

## Optional features

Both are plain on/off flags in `bot_config.json`, and `install.py` will ask about each one.

### `apple_music_enabled`

When on, `/find` first searches your **local Apple Music library** (via AppleScript automating Music.app) before falling back to YouTube — useful for confirming you already own a track, or preferring a specific version. This only works when the bot is running **on a Mac** with Music.app; there's no equivalent on Linux, and enabling it on a non-Mac host will just fail on every search. `install.py` checks for this and warns you. Either way, the *audio itself* always comes from YouTube — this flag only affects whether the library is searched for a match first.

### `backup`

When enabled, every uploaded track is also copied somewhere else, organized into a folder per card. Two backends:

- **`local`** — copied to a folder on the same machine (`backup.path`).
- **`dropbox_api`** — pushed straight to a Dropbox folder over their API (`backup.dropbox_base_path`), using a token you generate yourself in Dropbox's developer console. Nothing is synced down to the machine running the bot — it's upload-only.

If a backup fails (bad token, unwritable path, etc.), the dashboard shows it and the bot sends you a Telegram message — it doesn't fail silently.

---

## What you'll need

- **Python 3.10+**
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)**, kept reasonably up to date — YouTube changes often enough that a stale copy will break search
- **A JS runtime** ([Deno](https://deno.com)) — current yt-dlp needs one to solve YouTube's extraction challenge
- A **Yoto account** with cards already set up
- A **Telegram bot token** from [@BotFather](https://t.me/BotFather)

`install.py` checks all of these for you and tells you what's missing.

---

## Troubleshooting

- **Nothing happens when I message the bot** — confirm the server's running (`systemctl status`, or the terminal window on a Mac) and that your chat ID is in `allowed_user_ids`.
- **Search returns nothing** — check `yt-dlp --version` isn't badly out of date, and that a JS runtime is installed; both are exactly the kind of thing that's broken this before.
- **A button tap does nothing** — search results expire after a while (`PENDING_TTL` in `telegram_bot.py`); the bot will tell you if a selection's expired — just search again.
- **Uploads aren't backing up** — check the backup indicator on the dashboard (`http://localhost:8765`); it shows the last successful backup or the current failure reason directly.
- **Yoto login problems** — log in again via the dashboard, then run `python3 install.py` to confirm it with a real API call.

---

## Files overview

| File | Purpose |
|---|---|
| `telegram_bot.py` | The bot itself — search, selection, upload, backup |
| `server.py` | Local HTTP server: the dashboard, Yoto login, health/status endpoints |
| `index.html` | The web dashboard |
| `install.py` | Interactive setup/reconfigure, with real validation |
| `bot_config.json.example` | Every config field `install.py` can write, documented |
| `setup.py`, `sync.py` | A separate, optional standalone bulk-sync tool with its own auth — not used by the bot above |

---

## Privacy & security

- Runs locally — no third-party service involved beyond Yoto, Telegram, and (if enabled) Dropbox
- `bot_config.json` and `yoto_token.json` hold your credentials and are excluded from version control
- The bot only ever acts on messages from the chat IDs you configure
