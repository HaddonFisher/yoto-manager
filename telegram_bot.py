"""
Telegram → Yoto upload bot.

Runs as a background daemon thread inside server.py.
Uses only Python stdlib — no third-party packages required.

Phases implemented:
  3 — Telegram long-polling + message routing
  4 — Audio upload pipeline (local file path → Yoto playlist)
  5 — Fuzzy card matching via difflib
"""

import base64
import difflib
import hashlib
import json
import logging
import mimetypes
import os
import queue as _queue_module
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# ── File paths (set by server.py before calling run_telegram_bot) ─────────
TOKEN_FILE              = Path('yoto_token.json')
QUEUE_FILE              = Path('queue.json')
OFFSET_FILE             = Path('bot_offset.json')
PENDING_FILE            = Path('bot_pending.json')
RESTART_ACK_FILE        = Path('bot_restart_ack.json')
RECENT_PLAYLISTS_FILE   = Path('recent_playlists.json')
LAST_COMMAND_FILE       = Path('bot_last_command.json')
ACTIVITY_LOG_FILE       = Path('yoto_activity.log')
ERROR_LOG_FILE          = Path('yoto_errors.log')

# ── Thread lock protecting all queue.json reads/writes ────────────────────
# Shared by the HTTP-server thread (delete via /local/queue) and the
# Telegram polling thread (save_to_queue), so every read-modify-write must
# hold this lock.
_QUEUE_LOCK = threading.Lock()

# ── Thread lock serialising the GET /content/{id} → POST /content RMW step.
# A single batch uploads sequentially in its own thread, but a single-track
# `do_upload` runs in the polling thread and a second batch may run in
# another thread.  Without this lock, two of those paths targeting the same
# card can fetch the chapter list in parallel and clobber each other on POST.
_CONTENT_LOCK = threading.Lock()

# ── Thread lock protecting recent_playlists.json reads/writes ─────────────
_RECENT_LOCK = threading.Lock()

# ── Background job queue ──────────────────────────────────────────────────
# Jobs are dicts:
#   { 'bot_token', 'chat_id', 'tracks': [{'url', 'title'}, ...],
#     'card', 'raw_message' }
# Apple-Music jobs additionally carry 'am_tracks': [{'file_path', 'title'}, ...]
# and an empty 'tracks' list (downloads are already done).
_JOB_QUEUE: _queue_module.Queue = _queue_module.Queue()
_JOB_WORKER_STARTED = False
_JOB_WORKER_LOCK = threading.Lock()


# ── Logging setup ─────────────────────────────────────────────────────────
# Two independent loggers:
#   activity  — one brief line per user action (5 × 500 KB rolling files)
#   error     — verbose entry per exception, including full traceback
#               and raw API response where available (5 × 2 MB rolling files)

def _setup_logger(name: str, filepath: Path,
                  level: int, max_bytes: int, backup_count: int = 5) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if not logger.handlers:
        handler = RotatingFileHandler(
            str(filepath), maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8',
        )
        handler.setFormatter(logging.Formatter(
            '%(asctime)s  %(levelname)-8s  %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(handler)
    return logger


_activity_logger: Optional[logging.Logger] = None
_error_logger:    Optional[logging.Logger] = None


def _init_loggers() -> None:
    """Call once from run_telegram_bot() after file paths are finalised."""
    global _activity_logger, _error_logger
    _activity_logger = _setup_logger(
        'yoto.activity', ACTIVITY_LOG_FILE,
        level=logging.INFO, max_bytes=500_000,
    )
    _error_logger = _setup_logger(
        'yoto.errors', ERROR_LOG_FILE,
        level=logging.DEBUG, max_bytes=2_000_000,
    )


def log_activity(msg: str) -> None:
    """Brief one-liner for every user-initiated action."""
    if _activity_logger:
        _activity_logger.info(msg)
    # Also print to stdout (existing behaviour)
    print(f'  📋  {msg}')


def log_error(msg: str, exc: BaseException = None, extra: str = '') -> None:
    """Verbose error entry — includes traceback and optional raw API response."""
    if _error_logger:
        parts = [msg]
        if exc is not None:
            parts.append(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        if extra:
            parts.append(f'[extra] {extra}')
        _error_logger.error('\n'.join(parts))
    # Also print to stdout so server console still shows errors
    print(f'  ❌  {msg}')
    if exc and not str(exc) in msg:
        print(f'       {exc}')


def _save_offset(offset: int) -> None:
    """Persist the Telegram update offset so restarts don't replay old messages."""
    try:
        OFFSET_FILE.write_text(json.dumps({'offset': offset}))
    except Exception:
        pass


def _load_offset() -> int:
    """Load the last saved offset, defaulting to 0."""
    if OFFSET_FILE.exists():
        try:
            return int(json.loads(OFFSET_FILE.read_text()).get('offset', 0))
        except Exception:
            pass
    return 0

# ── Yoto API base ─────────────────────────────────────────────────────────
YOTO_API = 'https://api.yotoplay.com'
YOTO_AUTH = 'https://login.yotoplay.com'

# YouTube URL pattern — matches video and playlist URLs
YT_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:youtube\.com/(?:watch|playlist)[^\s]*|youtu\.be/[^\s]+)',
    re.IGNORECASE,
)

# ── Local temp dir for downloaded tracks ──────────────────────────────────
TEMP_DIR = Path('/tmp/yoto_downloads')

# ── Pending fuzzy-match state ─────────────────────────────────────────────
# Keyed by "chat_id:user_id" in JSON, (chat_id, user_id) tuple in memory.
# Persisted to bot_pending.json so restarts don't lose context.
PENDING_TTL = 300  # seconds (5 minutes)


def _pending_key(chat_id: int, from_uid: int) -> str:
    return f'{chat_id}:{from_uid}'


def _load_pending() -> dict:
    """Load pending states from disk, keyed by (chat_id, from_uid) tuples."""
    if not PENDING_FILE.exists():
        return {}
    try:
        raw = json.loads(PENDING_FILE.read_text())
        result = {}
        now = time.time()
        for k, v in raw.items():
            if v.get('expires_at', 0) > now:   # drop already-expired entries
                chat_id, from_uid = (int(x) for x in k.split(':'))
                result[(chat_id, from_uid)] = v
        return result
    except Exception:
        return {}


def _save_pending() -> None:
    try:
        serialisable = {
            _pending_key(k[0], k[1]): v
            for k, v in pending_matches.items()
        }
        PENDING_FILE.write_text(json.dumps(serialisable, indent=2))
    except Exception as e:
        print(f'  ⚠️  Could not save pending state: {e}')


# Populated at startup from disk; kept in sync with _save_pending()
pending_matches: dict = {}


# ═══════════════════════════════════════════════════════════════════════════
#  PHASE 3 — Telegram polling thread
# ═══════════════════════════════════════════════════════════════════════════

def tg_request(bot_token: str, method: str, payload: dict = None,
               timeout: int = 35) -> dict:
    """Make a Telegram Bot API call. Returns parsed JSON response dict."""
    url = f'https://api.telegram.org/bot{bot_token}/{method}'
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def tg_send(bot_token: str, chat_id: int, text: str,
             reply_to: int = None, parse_mode: str = 'Markdown') -> None:
    """Send a Telegram message. Errors are printed but not raised."""
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode}
    if reply_to:
        payload['reply_to_message_id'] = reply_to
    try:
        tg_request(bot_token, 'sendMessage', payload)
    except Exception as e:
        print(f'  ⚠️  Telegram sendMessage failed: {e}')


def tg_react(bot_token: str, chat_id: int, msg_id: int, emoji: str = '👋') -> None:
    """Add an emoji reaction to a message. Silently ignores errors."""
    try:
        tg_request(bot_token, 'setMessageReaction', {
            'chat_id':   chat_id,
            'message_id': msg_id,
            'reaction':  [{'type': 'emoji', 'emoji': emoji}],
        })
    except Exception as e:
        print(f'  ⚠️  Telegram setMessageReaction failed: {e}')


def tg_send_keyboard(bot_token: str, chat_id: int, text: str,
                     buttons: list, reply_to: int = None,
                     parse_mode: str = 'Markdown') -> None:
    """Send a message with an inline keyboard. buttons is a list of rows,
    each row is a list of (label, callback_data) tuples."""
    keyboard = [
        [{'text': label, 'callback_data': data} for label, data in row]
        for row in buttons
    ]
    payload = {
        'chat_id':      chat_id,
        'text':         text,
        'parse_mode':   parse_mode,
        'reply_markup': {'inline_keyboard': keyboard},
    }
    if reply_to:
        payload['reply_to_message_id'] = reply_to
    try:
        tg_request(bot_token, 'sendMessage', payload)
    except Exception as e:
        print(f'  ⚠️  Telegram sendMessage (keyboard) failed: {e}')


def tg_edit_keyboard(bot_token: str, chat_id: int, message_id: int, buttons: list) -> None:
    """Edit the reply markup of an existing bot message in-place.
    buttons is the same format as tg_send_keyboard: list of rows,
    each row is a list of (label, callback_data) tuples."""
    keyboard = [
        [{'text': label, 'callback_data': data} for label, data in row]
        for row in buttons
    ]
    try:
        tg_request(bot_token, 'editMessageReplyMarkup', {
            'chat_id':      chat_id,
            'message_id':   message_id,
            'reply_markup': {'inline_keyboard': keyboard},
        })
    except Exception as e:
        # "message is not modified" is harmless (same keyboard re-sent); log everything else
        if 'not modified' not in str(e).lower():
            log_error(f'editMessageReplyMarkup failed  chat={chat_id}  msg={message_id}: {e}', exc=e)


def tg_edit_message(bot_token: str, chat_id: int, message_id: int,
                    text: str, buttons: list,
                    parse_mode: str = 'Markdown') -> None:
    """Edit the text and reply markup of an existing bot message in-place."""
    keyboard = [
        [{'text': label, 'callback_data': data} for label, data in row]
        for row in buttons
    ]
    try:
        tg_request(bot_token, 'editMessageText', {
            'chat_id':      chat_id,
            'message_id':   message_id,
            'text':         text,
            'parse_mode':   parse_mode,
            'reply_markup': {'inline_keyboard': keyboard},
        })
    except Exception as e:
        print(f'  ⚠️  editMessageText failed: {e}')


def tg_send_keyboard_ret(bot_token: str, chat_id: int, text: str,
                          buttons: list, reply_to: int = None,
                          parse_mode: str = 'Markdown') -> Optional[int]:
    """Like tg_send_keyboard but returns the message_id of the sent message, or None."""
    keyboard = [
        [{'text': label, 'callback_data': data} for label, data in row]
        for row in buttons
    ]
    payload = {
        'chat_id':      chat_id,
        'text':         text,
        'parse_mode':   parse_mode,
        'reply_markup': {'inline_keyboard': keyboard},
    }
    if reply_to:
        payload['reply_to_message_id'] = reply_to
    try:
        resp = tg_request(bot_token, 'sendMessage', payload)
        return resp.get('result', {}).get('message_id')
    except Exception as e:
        print(f'  ⚠️  Telegram sendMessage (keyboard, ret) failed: {e}')
        return None


def tg_answer_callback(bot_token: str, callback_query_id: str,
                       text: str = '') -> None:
    """Acknowledge a callback query (dismisses the loading spinner)."""
    try:
        tg_request(bot_token, 'answerCallbackQuery', {
            'callback_query_id': callback_query_id,
            'text': text,
        })
    except Exception as e:
        print(f'  ⚠️  Telegram answerCallbackQuery failed: {e}')


def _ensure_job_worker() -> None:
    """Start the background job worker thread once (idempotent)."""
    global _JOB_WORKER_STARTED
    with _JOB_WORKER_LOCK:
        if not _JOB_WORKER_STARTED:
            threading.Thread(target=_job_worker, daemon=True, name='job-worker').start()
            _JOB_WORKER_STARTED = True


def _job_worker() -> None:
    """Background thread: pulls jobs from _JOB_QUEUE and processes them."""
    while True:
        job = _JOB_QUEUE.get()
        try:
            _process_job(job)
        except Exception as e:
            log_error(f'job_worker error: {e}', exc=e)
        finally:
            _JOB_QUEUE.task_done()


def _process_job(job: dict) -> None:
    """Process a single job: download (if needed) + upload each track."""
    bot_token = job['bot_token']
    chat_id   = job['chat_id']
    card      = job['card']
    raw_msg   = job.get('raw_message', '')
    playlist  = card_title(card)

    try:
        token = load_token()
    except TokenMissingError:
        tg_send(bot_token, chat_id,
                '❌ No Yoto token — open the dashboard and log in first.')
        return

    # Apple Music tracks that still need to be downloaded first
    am_pending_tracks = job.get('am_pending_tracks', [])
    for t in am_pending_tracks:
        title        = t.get('title', '')
        _CLOUD_ONLY  = {'matched', 'purchased', 'uploaded', 'subscription'}
        cloud_status = t.get('cloud_status', '')
        if cloud_status in _CLOUD_ONLY:
            tg_send(bot_token, chat_id, f'☁️ Downloading *{title}* from iTunes Match…')
        else:
            tg_send(bot_token, chat_id, f'⬇️ Getting *{title}* from Music library…')
        path = am_download(t['id'], title=title, artist=t.get('artist', ''))
        if not path:
            tg_send(bot_token, chat_id,
                    f'⚠️ *{title}* — download timed out, skipping')
            continue
        try:
            local_path = am_copy_to_temp(path, title, t.get('artist', ''))
        except Exception as e:
            tg_send(bot_token, chat_id, f'❌ Could not copy *{title}*: `{e}`')
            continue
        tg_send(bot_token, chat_id, f'⏳ Uploading *{title}* → *{playlist}*…')
        ok, err = _upload_core(local_path, title, card, token)
        if ok:
            record_recent_playlist(card.get('cardId') or card.get('id', ''), playlist)
            backup_track(local_path, title, playlist)
            if err:
                tg_send(bot_token, chat_id, err)
            tg_send(bot_token, chat_id, f'✅ *{title}* → *{playlist}*')
            log_activity(f'job upload ok  track={title!r}  playlist={playlist!r}')
        else:
            log_error(f'job upload fail  track={title!r}  playlist={playlist!r}  err={err}')
            tg_send(bot_token, chat_id, f'❌ *{title}* failed: `{err[:200]}`')

    # Apple Music pre-downloaded tracks (file_path already available)
    am_tracks = job.get('am_tracks', [])
    for item in am_tracks:
        title     = item['title']
        file_path = item['file_path']
        tg_send(bot_token, chat_id, f'⏳ Uploading *{title}* → *{playlist}*…')
        ok, err = _upload_core(file_path, title, card, token)
        if ok:
            record_recent_playlist(card.get('cardId') or card.get('id', ''), playlist)
            backup_track(file_path, title, playlist)
            if err:
                tg_send(bot_token, chat_id, err)
            tg_send(bot_token, chat_id, f'✅ *{title}* → *{playlist}*')
            log_activity(f'job upload ok  track={title!r}  playlist={playlist!r}')
        else:
            log_error(f'job upload fail  track={title!r}  playlist={playlist!r}  err={err}')
            tg_send(bot_token, chat_id,
                    f'❌ *{title}* failed: `{err[:200]}`')

    # YouTube tracks (need to download first)
    yt_tracks = job.get('tracks', [])
    for item in yt_tracks:
        title = item['title']
        url   = item['url']
        tg_send(bot_token, chat_id,
                f'⬇️ Downloading *{title}*…')
        try:
            mp3_path = yt_download_mp3(url, title)
        except Exception as e:
            log_error(f'job yt_download fail  track={title!r}  err={e}', exc=e)
            tg_send(bot_token, chat_id,
                    f'❌ *{title}* download failed: `{str(e)[:200]}`')
            continue
        # Optional OGG transcode
        file_path = mp3_path
        try:
            ogg = transcode_to_ogg(mp3_path)
            if ogg:
                file_path = ogg
        except Exception as e:
            log_activity(f'job transcode skip  track={title!r}  reason={e}')

        tg_send(bot_token, chat_id, f'⏳ Uploading *{title}* → *{playlist}*…')
        ok, err = _upload_core(file_path, title, card, token)
        if not ok:
            time.sleep(5)
            ok, err = _upload_core(file_path, title, card, token)
        if ok:
            record_recent_playlist(card.get('cardId') or card.get('id', ''), playlist)
            backup_track(file_path, title, playlist)
            if err:
                tg_send(bot_token, chat_id, err)
            tg_send(bot_token, chat_id, f'✅ *{title}* → *{playlist}*')
            log_activity(f'job upload ok  track={title!r}  playlist={playlist!r}')
        else:
            log_error(f'job upload fail (2 attempts)  track={title!r}  err={err}')
            tg_send(bot_token, chat_id,
                    f'❌ *{title}* failed: `{err[:200]}`')


def run_telegram_bot(cfg: dict) -> None:
    """
    Entry point for the daemon thread.
    Long-polls getUpdates and dispatches each message.
    """
    bot_token       = cfg['telegram_bot_token']
    allowed_group   = cfg['allowed_group_id']
    offset          = _load_offset()

    _init_loggers()
    _ensure_job_worker()

    # Restore any pending states that survived the restart
    pending_matches.update(_load_pending())
    log_activity(f'Bot started. {len(pending_matches)} pending state(s) restored.')
    print(f'  🤖  Telegram bot polling started… ({len(pending_matches)} pending state(s) restored)')

    # If we were restarted via /restart, react to that message to signal we're back
    if RESTART_ACK_FILE.exists():
        try:
            ack = json.loads(RESTART_ACK_FILE.read_text())
            tg_send(bot_token, ack['chat_id'], '👋 Back online!',
                    reply_to=ack['msg_id'])
            # Also try a reaction — works if the group supports it
            tg_react(bot_token, ack['chat_id'], ack['msg_id'], '👋')
            RESTART_ACK_FILE.unlink()
            print('  ✅  Sent restart-ack')
        except Exception as e:
            print(f'  ⚠️  Could not send restart-ack: {e}')
            try:
                RESTART_ACK_FILE.unlink()
            except Exception:
                pass

    POLL_TIMEOUT = 30   # seconds Telegram holds the connection open
    SOCK_TIMEOUT = 65   # socket timeout — must be well above POLL_TIMEOUT

    while True:
        try:
            resp = tg_request(bot_token, 'getUpdates', {
                'offset':          offset,
                'timeout':         POLL_TIMEOUT,
                'allowed_updates': ['message', 'callback_query'],
            }, timeout=SOCK_TIMEOUT)

            for update in resp.get('result', []):
                offset = update['update_id'] + 1

                # ── Inline keyboard button tap ───────────────────────────
                cq = update.get('callback_query')
                if cq:
                    cq_id    = cq['id']
                    from_uid = cq.get('from', {}).get('id')
                    chat_id  = cq.get('message', {}).get('chat', {}).get('id')
                    msg_id   = cq.get('message', {}).get('message_id')
                    data     = (cq.get('data') or '').strip()

                    if chat_id != allowed_group:
                        tg_answer_callback(bot_token, cq_id)
                        continue

                    tg_answer_callback(bot_token, cq_id)
                    handle_selection_reply(bot_token, chat_id, from_uid, msg_id, data)
                    continue

                # ── Regular message ──────────────────────────────────────
                msg = update.get('message')
                if not msg:
                    continue

                chat_id  = msg['chat']['id']
                from_uid = msg.get('from', {}).get('id')
                text     = (msg.get('text') or '').strip()
                msg_id   = msg['message_id']

                # ── Security: only allow the configured group ────────────
                if chat_id != allowed_group:
                    continue  # silently ignore

                # ── Normalise text ───────────────────────────────────────
                # Strip @BotName mention from start or end so that
                # "@DomoYotoBot help" and "help @DomoYotoBot" both route
                # the same as plain "help" or "/help".
                text = re.sub(r'^@\w+\s+', '', text)   # "@Bot cmd …" → "cmd …"
                text = re.sub(r'\s+@\w+$', '', text)   # "cmd … @Bot" → "cmd …"

                # ── Route message ────────────────────────────────────────
                text_lower = text.lower().strip().lstrip('/')
                if text_lower.startswith('retry'):
                    log_activity(f'cmd /retry  uid={from_uid}  chat={chat_id}')
                    last = _get_last_command(chat_id, from_uid)
                    if not last:
                        tg_send(bot_token, chat_id,
                                '❓ No previous command to retry.', reply_to=msg_id)
                    else:
                        tg_send(bot_token, chat_id,
                                f'🔁 Retrying: `{last[:120]}`', reply_to=msg_id)
                        last_lower = last.lower().strip().lstrip('/')
                        if last_lower.startswith('findplay'):
                            handle_findplay_command(bot_token, chat_id, from_uid, msg_id, last)
                        elif last_lower.startswith('find'):
                            handle_find_command(bot_token, chat_id, from_uid, msg_id, last)
                        elif last_lower.startswith('create'):
                            handle_create_command(bot_token, chat_id, from_uid, msg_id, last)
                elif text_lower.startswith('findplay'):
                    log_activity(f'cmd /findplay  uid={from_uid}  chat={chat_id}  text={text[:120]!r}')
                    handle_findplay_command(bot_token, chat_id, from_uid, msg_id, text)
                    _save_last_command(chat_id, from_uid, text)
                elif text_lower.startswith('find'):
                    log_activity(f'cmd /find  uid={from_uid}  chat={chat_id}  text={text[:120]!r}')
                    handle_find_command(bot_token, chat_id, from_uid, msg_id, text)
                    _save_last_command(chat_id, from_uid, text)
                elif text_lower.startswith('create'):
                    log_activity(f'cmd /create  uid={from_uid}  chat={chat_id}  text={text[:120]!r}')
                    handle_create_command(bot_token, chat_id, from_uid, msg_id, text)
                    _save_last_command(chat_id, from_uid, text)
                elif text_lower == 'help' or text_lower.startswith('help@'):
                    log_activity(f'cmd /help  uid={from_uid}  chat={chat_id}')
                    handle_help(bot_token, chat_id, msg_id)
                elif text_lower.startswith('restart'):
                    log_activity(f'cmd /restart  uid={from_uid}  chat={chat_id}')
                    handle_restart(bot_token, chat_id, msg_id, offset)
                else:
                    handle_selection_reply(bot_token, chat_id, from_uid, msg_id, text)

            # Persist offset after every successful batch so a crash doesn't replay messages
            _save_offset(offset)

        except TimeoutError:
            # Socket timed out during long-poll — normal under intermittent network; retry immediately
            pass
        except urllib.error.URLError as e:
            log_error(f'Telegram poll network error — retrying in 5s: {e}', e)
            time.sleep(5)
        except Exception as e:
            log_error(f'Telegram poll error — retrying in 5s: {e}', e)
            time.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════
#  PHASE 3 — Message handlers
# ═══════════════════════════════════════════════════════════════════════════

def handle_help(bot_token: str, chat_id: int, msg_id: int) -> None:
    """Reply with usage instructions."""
    tg_send(bot_token, chat_id, (
        '🎧 *Yoto Manager Bot — Help*\n'
        '\n'
        '*Commands:*\n'
        '• `/find Song Title` — search Apple Music, then YouTube; pick a result to add to a playlist\n'
        '• `/find Song Title | Playlist` — same, with playlist pre-specified\n'
        '• `/findplay Query` — search YouTube for playlists; pick one to browse its tracks or add all\n'
        '• `/findplay Query | Playlist` — same, with Yoto playlist pre-specified\n'
        '• `/create Playlist Name` — create a new empty Yoto playlist\n'
        '• `/retry` — repeat your last /find, /findplay, or /create command\n'
        '• `help` or `/help` — show this message\n'
        '• `restart` or `/restart` — restart the server (reloads all code)\n'
        '\n'
        '*Queue:*\n'
        'Failed uploads can be saved to the queue and retried from the dashboard under ⏳ Queue.\n'
    ), reply_to=msg_id)


def handle_restart(bot_token: str, chat_id: int, msg_id: int, offset: int) -> None:
    """Restart the server process, reloading all code."""
    tg_send(bot_token, chat_id, '🔄 Restarting server… back in a few seconds.', reply_to=msg_id)
    _save_offset(offset)  # persist offset so the new process doesn't replay this message
    # Save the restart message details so the new process can react to it on startup
    try:
        RESTART_ACK_FILE.write_text(json.dumps({'chat_id': chat_id, 'msg_id': msg_id}))
    except Exception:
        pass
    time.sleep(0.8)  # give the message time to send before replacing the process
    os.execv(sys.executable, [sys.executable] + sys.argv)


def handle_create_command(bot_token: str, chat_id: int, from_uid: int,
                          msg_id: int, text: str) -> None:
    """/create [Playlist Name] — create a new Yoto playlist."""
    rest = re.sub(r'^/?create(@\S+)?\s*', '', text, flags=re.IGNORECASE).strip()
    if rest:
        # Name provided inline — create immediately
        _do_create_playlist(bot_token, chat_id, msg_id, rest,
                            file_path=None, track_name=None, raw_message=text,
                            from_uid=from_uid)
    else:
        # No name — ask for one
        _prompt_create_playlist(bot_token, chat_id, from_uid, msg_id,
                                suggested='', file_path=None, track_name=None,
                                raw_message=text)


def _prompt_create_playlist(bot_token: str, chat_id: int, from_uid: int,
                              msg_id: int, suggested: str,
                              file_path: Optional[str], track_name: Optional[str],
                              raw_message: str, yt_tracks: list = None,
                              am_tracks: list = None,
                              am_pending_tracks: list = None) -> None:
    """Ask the user to type a playlist name."""
    prompt = (
        f'What should the new playlist be called?\n'
        f'_(Suggested: *{suggested}*)_' if suggested
        else 'What should the new playlist be called? Just type the name.'
    )
    tg_send(bot_token, chat_id, prompt, reply_to=msg_id)
    store_pending(chat_id, from_uid, file_path or '', track_name or '', [],
                  raw_message=raw_message,
                  type='create_name',
                  suggested=suggested,
                  yt_tracks=yt_tracks or [],
                  am_tracks=am_tracks or [],
                  am_pending_tracks=am_pending_tracks or [])


def _handle_create_name_reply(bot_token: str, chat_id: int, from_uid: int,
                               msg_id: int, text: str, pending: dict) -> None:
    """User typed the name for the new playlist."""
    key = (chat_id, from_uid)
    name = text.strip()
    if not name:
        return  # stay silent
    file_path         = pending.get('file_path') or None
    track_name        = pending.get('track_name') or None
    raw_msg           = pending.get('raw_message', '')
    yt_tracks         = pending.get('yt_tracks') or []
    am_tracks         = pending.get('am_tracks') or []
    am_pending_tracks = pending.get('am_pending_tracks') or []
    del pending_matches[key]
    _save_pending()
    _do_create_playlist(bot_token, chat_id, msg_id, name,
                        file_path=file_path, track_name=track_name,
                        raw_message=raw_msg, yt_tracks=yt_tracks,
                        am_tracks=am_tracks, am_pending_tracks=am_pending_tracks,
                        from_uid=from_uid)


def _do_create_playlist(bot_token: str, chat_id: int, msg_id: int,
                         name: str, file_path: Optional[str],
                         track_name: Optional[str], raw_message: str,
                         yt_tracks: list = None, am_tracks: list = None,
                         am_pending_tracks: list = None,
                         from_uid: int = 0) -> None:
    """Call the Yoto API to create the playlist, then upload if a file/batch is queued."""
    log_activity(f'create_playlist  name={name!r}')
    tg_send(bot_token, chat_id, f'➕ Creating playlist *{name}*…', reply_to=msg_id)
    try:
        token = load_token()
        card  = create_playlist(token, name)
        global _card_cache
        _card_cache = None
        log_activity(f'create_playlist success  name={name!r}  id={card.get("cardId") or card.get("id", "?")}')
        tg_send(bot_token, chat_id, f'✅ Created playlist *{name}*!')
        if yt_tracks:
            n = len(yt_tracks)
            _JOB_QUEUE.put({
                'bot_token': bot_token, 'chat_id': chat_id,
                'tracks': yt_tracks, 'card': card, 'raw_message': raw_message,
            })
            tg_send(bot_token, chat_id,
                    f'✅ Queued {n} track{"s" if n > 1 else ""} → *{name}*\n'
                    f'_(Download & upload running in background)_')
        elif am_pending_tracks:
            n = len(am_pending_tracks)
            _JOB_QUEUE.put({
                'bot_token': bot_token, 'chat_id': chat_id,
                'am_pending_tracks': am_pending_tracks, 'card': card, 'raw_message': raw_message,
            })
            tg_send(bot_token, chat_id,
                    f'✅ Queued {n} track{"s" if n > 1 else ""} → *{name}*\n'
                    f'_(Download & upload running in background)_')
        elif am_tracks:
            n = len(am_tracks)
            _JOB_QUEUE.put({
                'bot_token': bot_token, 'chat_id': chat_id,
                'tracks': [], 'am_tracks': am_tracks, 'card': card, 'raw_message': raw_message,
            })
            tg_send(bot_token, chat_id,
                    f'✅ Queued {n} track{"s" if n > 1 else ""} → *{name}*\n'
                    f'_(Upload running in background)_')
        elif file_path and track_name:
            do_upload(bot_token, chat_id, msg_id, file_path, track_name, card,
                      raw_message=raw_message, from_uid=from_uid)
    except TokenMissingError:
        log_error(f'create_playlist failed — no token  name={name!r}')
        tg_send(bot_token, chat_id,
                '❌ No Yoto token — open the dashboard and log in first.',
                reply_to=msg_id)
    except Exception as e:
        log_error(f'create_playlist failed  name={name!r}  err={e}', exc=e)
        tg_send(bot_token, chat_id,
                f'❌ Could not create playlist: `{e}`', reply_to=msg_id)


def create_playlist(token: dict, name: str) -> dict:
    """Create a new empty Yoto playlist and return the card dict."""
    result = yoto_post(token, '/content', {
        'title':    name,
        'metadata': {'title': name},
        'content':  {'chapters': []},
        'deleted':  False,
    })
    # API returns the card directly; cardId is auto-generated
    card = result.get('card') or result
    if not (card.get('cardId') or card.get('id')):
        raise RuntimeError(f'Unexpected API response: {str(result)[:200]}')
    return card


def handle_upload_command(bot_token: str, chat_id: int, from_uid: int,
                           msg_id: int, text: str) -> None:
    """
    Parse /upload <file_path> [| <playlist name>]
    and kick off the upload or fuzzy-match flow.
    """
    # Strip the /upload command prefix (handles /upload@botname too)
    rest = re.sub(r'^/upload(@\S+)?\s*', '', text, flags=re.IGNORECASE).strip()

    if not rest:
        tg_send(bot_token, chat_id,
                '❓ Usage:\n`/upload /path/to/file.mp3 | Playlist Name`\n'
                'Or omit the playlist name to search.',
                reply_to=msg_id)
        return

    # Split on '|' — optional playlist name
    parts = [p.strip() for p in rest.split('|', 1)]
    file_path    = parts[0]
    playlist_name = parts[1] if len(parts) > 1 else None

    # Validate file exists
    path = Path(file_path)
    if not path.exists():
        tg_send(bot_token, chat_id,
                f'❌ File not found:\n`{file_path}`', reply_to=msg_id)
        return
    if not path.is_file():
        tg_send(bot_token, chat_id,
                f'❌ Path is not a file:\n`{file_path}`', reply_to=msg_id)
        return

    # Derive a nice track name from the filename
    track_name = path.stem
    # Strip leading numbers / underscores: "01 - Song Name" → "Song Name"
    track_name = re.sub(r'^[\d\s\-_.]+', '', track_name).strip() or path.stem

    # Load cards from Yoto
    try:
        cards = fetch_cards()
    except Exception as e:
        tg_send(bot_token, chat_id, f'❌ Could not load your Yoto library:\n`{e}`',
                reply_to=msg_id)
        return

    if playlist_name:
        # Exact match first
        card = find_card_exact(playlist_name, cards)
        if card:
            do_upload(bot_token, chat_id, msg_id, file_path, track_name, card,
                      raw_message=text, from_uid=from_uid)
        else:
            # Exact failed — fall back to fuzzy with their query pre-filled
            offer_fuzzy_matches(bot_token, chat_id, from_uid, msg_id,
                                file_path, track_name, playlist_name, cards,
                                raw_message=text)
    else:
        # No playlist given — go straight to fuzzy
        offer_fuzzy_matches(bot_token, chat_id, from_uid, msg_id,
                            file_path, track_name, '', cards,
                            raw_message=text)


def offer_fuzzy_matches(bot_token: str, chat_id: int, from_uid: int, msg_id: int,
                         file_path: str, track_name: str, query: str,
                         cards: list, raw_message: str = '') -> None:
    """Show playlist options.

    If query is empty (no playlist specified), show Pick / Create buttons.
    If query has text but no exact match, show fuzzy suggestions.
    """
    if not query:
        # No playlist specified — let the user choose how to proceed
        _offer_pick_or_create(bot_token, chat_id, from_uid, msg_id,
                              file_path, track_name, cards, raw_message)
        return

    matches = fuzzy_match_cards(query, cards, n=3)

    if not matches:
        # Fuzzy search for the given name found nothing
        create_label = f'➕ Create "{query}"'
        buttons = [
            [(create_label, 'create')],
            [('🔍 Search playlists', 'pick')],
            [('📥 Save for later', 'save')],
        ]
        tg_send_keyboard(
            bot_token, chat_id,
            f'❓ No playlists found matching *{query}*.',
            buttons, reply_to=msg_id,
        )
        store_pending(chat_id, from_uid, file_path, track_name, cards,
                      raw_message=raw_message, create_name=query,
                      type='playlist_choose', all_cards=cards)
        return

    lines = ['Which playlist?\n']
    for i, card in enumerate(matches, 1):
        lines.append(f'  {i}. {card_title(card)}')
    buttons = (
        [[(f'{i}. {card_title(card)}', str(i))] for i, card in enumerate(matches, 1)]
        + [[('🔍 Search playlists', 'pick')]]
        + [[('➕ Create new playlist', 'create')]]
        + [[('📥 Save for later', 'save')]]
    )
    tg_send_keyboard(bot_token, chat_id, '\n'.join(lines), buttons, reply_to=msg_id)
    store_pending(chat_id, from_uid, file_path, track_name, cards,
                  raw_message=raw_message, create_name=query,
                  type='playlist_choose', all_cards=cards)


def _offer_pick_or_create(bot_token: str, chat_id: int, from_uid: int, msg_id: int,
                           file_path: str, track_name: str,
                           cards: list, raw_message: str) -> None:
    """No playlist specified — ask the user to pick one or create a new one."""
    recents = load_recent_playlists()
    buttons = [
        [(f'🕐 {r["title"]}', f'recent:{i}')] for i, r in enumerate(recents)
    ] + [
        [('🔍 Search playlists', 'pick')],
        [('➕ Create a playlist', 'create')],
        [('❌ Cancel',           'cancel')],
    ]
    tg_send_keyboard(
        bot_token, chat_id,
        f'🎵 *{track_name}*\nWhich playlist should this go into?',
        buttons, reply_to=msg_id,
    )
    store_pending(chat_id, from_uid, file_path, track_name, cards,
                  raw_message=raw_message, type='playlist_choose', all_cards=cards)


def handle_selection_reply(bot_token: str, chat_id: int, from_uid: int,
                            msg_id: int, text: str) -> None:
    """Dispatch pending-state replies based on their type."""
    key = (chat_id, from_uid)
    pending = pending_matches.get(key)

    if not pending:
        return  # not waiting for a reply from this user — stay silent

    # Expire old pending state
    if time.time() > pending['expires_at']:
        del pending_matches[key]
        _save_pending()
        return

    ptype = pending.get('type', 'upload')

    if ptype == 'am_confirm':
        _handle_am_confirm_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'youtube_multiselect':
        _handle_youtube_multiselect_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'youtube_pick':
        # Legacy handler — redirect to multiselect (shouldn't normally be hit after restart)
        _handle_youtube_multiselect_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'yt_playlist_tracks':
        _handle_yt_playlist_tracks_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'create_name':
        _handle_create_name_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'yt_batch_confirm':
        _handle_yt_batch_confirm_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'yt_batch_select':
        _handle_yt_batch_select_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'playlist_choose':
        _handle_playlist_choose_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'playlist_search':
        _handle_playlist_search_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'upload_failed':
        _handle_upload_failed_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    elif ptype == 'yt_playlist_pick':
        _handle_yt_playlist_pick_reply(bot_token, chat_id, from_uid, msg_id, text, pending)
    else:
        _handle_upload_selection(bot_token, chat_id, from_uid, msg_id, text, pending)


def _handle_upload_selection(bot_token: str, chat_id: int, from_uid: int,
                              msg_id: int, text: str, pending: dict) -> None:
    """Original upload playlist-selection reply handler."""
    key = (chat_id, from_uid)
    cleaned = text.strip().lower()

    if cleaned == 'save':
        save_to_queue(pending['file_path'], pending['track_name'],
                      raw_message=pending.get('raw_message', ''),
                      reason='user_saved',
                      rejected_candidates=pending.get('candidates', []))
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id,
                f'📥 Saved *{pending["track_name"]}* to queue.\n'
                'Assign it a playlist from the dashboard.',
                reply_to=msg_id)
        return

    if cleaned == 'create':
        suggested = pending.get('create_name', '')
        file_path  = pending['file_path']
        track_name = pending['track_name']
        raw_msg    = pending.get('raw_message', '')
        del pending_matches[key]
        _save_pending()
        _prompt_create_playlist(bot_token, chat_id, from_uid, msg_id,
                                suggested, file_path, track_name, raw_msg)
        return

    if cleaned.isdigit():
        idx = int(cleaned) - 1
        candidates = pending['candidates']
        if idx < 0 or idx >= len(candidates):
            tg_send(bot_token, chat_id,
                    f'❌ Invalid choice. Reply 1–{len(candidates)}.',
                    reply_to=msg_id)
            return
        card = candidates[idx]
        file_path  = pending['file_path']
        track_name = pending['track_name']
        raw_msg    = pending.get('raw_message', '')
        del pending_matches[key]
        _save_pending()
        do_upload(bot_token, chat_id, msg_id, file_path, track_name, card,
                  raw_message=raw_msg, from_uid=from_uid)
        return

    # Not a recognised reply — stay silent (don't spam the group)


def _handle_playlist_choose_reply(bot_token: str, chat_id: int, from_uid: int,
                                   msg_id: int, text: str, pending: dict) -> None:
    """Handle the Pick / Create / Save / recent:N buttons when no playlist was specified."""
    key     = (chat_id, from_uid)
    cleaned = text.strip().lower()

    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return

    if cleaned.startswith('recent:'):
        try:
            idx = int(cleaned[7:])
        except ValueError:
            return
        recents   = load_recent_playlists()
        all_cards = pending.get('all_cards') or pending.get('candidates', [])
        if idx >= len(recents):
            tg_send(bot_token, chat_id, '❌ That recent playlist is no longer available.',
                    reply_to=msg_id)
            return
        recent  = recents[idx]
        card    = next(
            (c for c in all_cards
             if (c.get('cardId') or c.get('id')) == recent['cardId']),
            None,
        )
        if not card:
            tg_send(bot_token, chat_id,
                    f'❌ Could not find playlist *{recent["title"]}* — '
                    'it may have been deleted. Try searching.',
                    reply_to=msg_id)
            return
        file_path         = pending['file_path']
        track_name        = pending['track_name']
        raw_msg           = pending.get('raw_message', '')
        yt_tracks         = pending.get('yt_tracks')
        am_tracks         = pending.get('am_tracks')
        am_pending_tracks = pending.get('am_pending_tracks')
        del pending_matches[key]
        _save_pending()
        if yt_tracks:
            n = len(yt_tracks)
            _JOB_QUEUE.put({
                'bot_token': bot_token, 'chat_id': chat_id,
                'tracks': yt_tracks, 'card': card, 'raw_message': raw_msg,
            })
            tg_send(bot_token, chat_id,
                    f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                    f'_(Download & upload running in background)_')
        elif am_pending_tracks:
            n = len(am_pending_tracks)
            _JOB_QUEUE.put({
                'bot_token': bot_token, 'chat_id': chat_id,
                'am_pending_tracks': am_pending_tracks, 'card': card, 'raw_message': raw_msg,
            })
            tg_send(bot_token, chat_id,
                    f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                    f'_(Download & upload running in background)_')
        elif am_tracks:
            n = len(am_tracks)
            _JOB_QUEUE.put({
                'bot_token': bot_token, 'chat_id': chat_id,
                'tracks': [], 'am_tracks': am_tracks, 'card': card, 'raw_message': raw_msg,
            })
            tg_send(bot_token, chat_id,
                    f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                    f'_(Upload running in background)_')
        else:
            do_upload(bot_token, chat_id, msg_id, file_path, track_name, card,
                      raw_message=raw_msg, from_uid=from_uid)
        return

    if cleaned == 'save':
        save_to_queue(pending['file_path'], pending['track_name'],
                      raw_message=pending.get('raw_message', ''),
                      reason='user_saved', rejected_candidates=[])
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id,
                f'📥 Saved *{pending["track_name"]}* to queue.',
                reply_to=msg_id)
        return

    if cleaned == 'create':
        file_path         = pending['file_path']
        track_name        = pending['track_name']
        raw_msg           = pending.get('raw_message', '')
        yt_tracks         = pending.get('yt_tracks')
        am_tracks         = pending.get('am_tracks')
        am_pending_tracks = pending.get('am_pending_tracks')
        del pending_matches[key]
        _save_pending()
        _prompt_create_playlist(bot_token, chat_id, from_uid, msg_id,
                                '', file_path, track_name, raw_msg,
                                yt_tracks=yt_tracks or [],
                                am_tracks=am_tracks or [],
                                am_pending_tracks=am_pending_tracks or [])
        return

    if cleaned == 'pick':
        # Switch to search state — bot sends a prompt the user must reply to
        pending['type']       = 'playlist_search'
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        tg_send(bot_token, chat_id,
                '🔍 *Reply to this message* with part of the playlist name to search:',
                reply_to=msg_id)
        return

    # Numbered pick from a fuzzy-match fallback list
    if cleaned.isdigit():
        idx       = int(cleaned) - 1
        all_cards = pending.get('candidates', [])
        if 0 <= idx < len(all_cards):
            card              = all_cards[idx]
            file_path         = pending['file_path']
            track_name        = pending['track_name']
            raw_msg           = pending.get('raw_message', '')
            yt_tracks         = pending.get('yt_tracks')
            am_tracks         = pending.get('am_tracks')
            am_pending_tracks = pending.get('am_pending_tracks')
            del pending_matches[key]
            _save_pending()
            if yt_tracks:
                n = len(yt_tracks)
                _JOB_QUEUE.put({
                    'bot_token': bot_token, 'chat_id': chat_id,
                    'tracks': yt_tracks, 'card': card, 'raw_message': raw_msg,
                })
                tg_send(bot_token, chat_id,
                        f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                        f'_(Download & upload running in background)_')
            elif am_pending_tracks:
                n = len(am_pending_tracks)
                _JOB_QUEUE.put({
                    'bot_token': bot_token, 'chat_id': chat_id,
                    'am_pending_tracks': am_pending_tracks, 'card': card, 'raw_message': raw_msg,
                })
                tg_send(bot_token, chat_id,
                        f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                        f'_(Download & upload running in background)_')
            elif am_tracks:
                n = len(am_tracks)
                _JOB_QUEUE.put({
                    'bot_token': bot_token, 'chat_id': chat_id,
                    'tracks': [], 'am_tracks': am_tracks, 'card': card, 'raw_message': raw_msg,
                })
                tg_send(bot_token, chat_id,
                        f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                        f'_(Upload running in background)_')
            else:
                do_upload(bot_token, chat_id, msg_id, file_path, track_name, card,
                          raw_message=raw_msg, from_uid=from_uid)
        return

    # Not a recognised reply — stay silent


def _handle_playlist_search_reply(bot_token: str, chat_id: int, from_uid: int,
                                   msg_id: int, text: str, pending: dict) -> None:
    """User replied with a search term — show fuzzy-matched playlists as buttons."""
    key        = (chat_id, from_uid)
    query      = text.strip()
    # 'all_cards' is the full library — always search over it.
    # 'candidates' may have been narrowed to a previous result set; fall back to it.
    all_cards  = pending.get('all_cards') or pending.get('candidates', [])
    file_path  = pending['file_path']
    track_name = pending['track_name']
    raw_msg    = pending.get('raw_message', '')

    matches = fuzzy_match_cards(query, all_cards, n=6)

    if not matches:
        buttons = [
            [('🔍 Search again', 'pick')],
            [('➕ Create new playlist', 'create')],
            [('📥 Save for later', 'save')],
        ]
        tg_send_keyboard(
            bot_token, chat_id,
            f'❓ No playlists matched *{query}*.',
            buttons, reply_to=msg_id,
        )
        pending['type']       = 'playlist_choose'
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        return

    lines = [f'🔍 Results for *{query}*:\n']
    for i, card in enumerate(matches, 1):
        lines.append(f'  {i}. {card_title(card)}')
    buttons = (
        [[(f'{i}. {card_title(card)}', str(i))] for i, card in enumerate(matches, 1)]
        + [[('🔍 Search again', 'pick')]]
        + [[('➕ Create new playlist', 'create')]]
        + [[('📥 Save for later', 'save')]]
    )
    tg_send_keyboard(bot_token, chat_id, '\n'.join(lines), buttons, reply_to=msg_id)
    # Keep all_cards for further searches; update candidates to these matches for numbered picks
    pending['type']        = 'playlist_choose'
    pending['candidates']  = matches
    pending['all_cards']   = all_cards
    pending['create_name'] = query
    pending['expires_at']  = time.time() + PENDING_TTL
    _save_pending()


def _handle_upload_failed_reply(bot_token: str, chat_id: int, from_uid: int,
                                 msg_id: int, text: str, pending: dict) -> None:
    """User tapped '📥 Add to queue' after an upload failure."""
    key = (chat_id, from_uid)
    if text.strip().lower() != 'add_to_queue':
        return  # unrecognised — stay silent
    card = pending.get('card', {})
    save_to_queue(pending['file_path'], pending['track_name'],
                  raw_message=pending.get('raw_message', ''),
                  reason='upload_failed',
                  rejected_candidates=[card] if card else [])
    del pending_matches[key]
    _save_pending()
    tg_send(bot_token, chat_id,
            f'📥 Saved *{pending["track_name"]}* to queue.\n'
            'Assign it a playlist from the dashboard.',
            reply_to=msg_id)


def store_pending(chat_id: int, from_uid: int, file_path: str,
                  track_name: str, candidates: list,
                  raw_message: str = '', **extra) -> None:
    pending_matches[(chat_id, from_uid)] = {
        'type':        'upload',   # override via extra if needed
        'file_path':   file_path,
        'track_name':  track_name,
        'candidates':  candidates,
        'raw_message': raw_message,
        'expires_at':  time.time() + PENDING_TTL,
        **extra,
    }
    _save_pending()


# ═══════════════════════════════════════════════════════════════════════════
#  PHASE 4 — Upload pipeline
# ═══════════════════════════════════════════════════════════════════════════

def _upload_core(file_path: str, track_name: str, card: dict,
                  token: dict = None) -> tuple:
    """
    Core upload logic — no Telegram messaging.
    Returns (True, '') on success or (False, error_message) on failure.

    The GET /content → POST /content step is always wrapped in _CONTENT_LOCK
    so concurrent callers (single-track upload from the polling thread,
    parallel batches in worker threads) can't race when writing the same card.
    """
    try:
        if token is None:
            token = load_token()
        card_id = card.get('cardId') or card.get('id', '')
        if not card_id:
            raise RuntimeError(f'Card has no cardId/id: {str(card)[:200]}')
        if not file_path:
            raise RuntimeError('Empty file_path')
        if not Path(file_path).exists():
            raise RuntimeError(f'File not found: {file_path}')

        file_bytes, filename = get_file_bytes(file_path)
        if not file_bytes:
            raise RuntimeError(f'File is empty (0 bytes): {file_path}')
        encoded_filename = urllib.parse.quote(filename)

        # Step 1: Get a fresh upload slot (no sha256 param per Yoto API docs)
        upload_info = yoto_get(
            token,
            f'/media/transcode/audio/uploadUrl?filename={encoded_filename}',
        )
        upload_data = upload_info.get('upload', upload_info)
        upload_id   = upload_data.get('uploadId') or upload_data.get('id', '')
        upload_url  = upload_data.get('uploadUrl')
        if not upload_url:
            raise RuntimeError(f'Yoto API returned no uploadUrl for {filename!r}')
        if not upload_id:
            raise RuntimeError(f'Yoto API returned no uploadId for {filename!r}')

        # Step 2: PUT file to S3
        s3_req = urllib.request.Request(upload_url, data=file_bytes, method='PUT')
        mime_type = mimetypes.guess_type(filename)[0] or 'audio/mpeg'
        s3_req.add_header('Content-Type', mime_type)
        s3_req.add_header('Content-Disposition', f'attachment; filename="{filename}"')
        # Generous timeout — large tracks over a slow link can take a while,
        # but a stuck S3 socket must eventually unblock so we don't wedge.
        with urllib.request.urlopen(s3_req, timeout=300) as resp:
            print(f'  ☁️   S3 PUT {resp.status} for {filename}')
        log_activity(f'_upload_core: S3 PUT OK  upload_id={upload_id!r}')

        # Step 3: Poll until Yoto finishes transcoding the uploaded file.
        # Log every distinct response so we can see the real field names/structure.
        # Check transcodedSha256 at both the top level and one wrapper level deep
        # (the API may return { transcodedSha256, transcodedInfo } or wrap it under
        # a key like 'upload' or 'media').
        transcoded = None
        last_raw   = None
        for attempt in range(600):
            if attempt > 0:
                time.sleep(1.0)
            result   = yoto_get(
                token,
                f'/media/upload/{upload_id}/transcoded?loudnorm=false',
            )
            raw_repr = json.dumps(result, sort_keys=True)[:600]
            if raw_repr != last_raw:
                log_activity(f'_upload_core: poll [{attempt + 1}] {raw_repr}')
                print(f'  🔄  poll [{attempt + 1}] {raw_repr}')
                last_raw = raw_repr

            # Accept the hash at the top level or one dict level down
            wrapper      = next((result[k] for k in result if isinstance(result.get(k), dict)), {})
            sha256_val   = result.get('transcodedSha256') or wrapper.get('transcodedSha256')
            info_val     = result.get('transcodedInfo')   or wrapper.get('transcodedInfo') or {}

            if sha256_val:
                transcoded = {'transcodedSha256': sha256_val, 'transcodedInfo': info_val}
                log_activity(
                    f'_upload_core: transcoded after {attempt + 1} poll(s)  '
                    f'sha256={sha256_val[:12]}…'
                )
                break

        if not transcoded:
            raise RuntimeError(
                f'Transcoding did not complete after 600 polls for {filename!r}'
            )

        # Step 4: Build trackUrl from transcodedSha256 and collect transcodedInfo
        transcoded_sha256 = transcoded['transcodedSha256']
        track_url = f'yoto:#{transcoded_sha256}'
        info = transcoded['transcodedInfo']

        # ── Serialised read-modify-write: prevents concurrent callers from
        #    interleaving GET /content/{id} → POST /content and clobbering
        #    each other's chapter lists.
        with _CONTENT_LOCK:
            card_data = yoto_get(token, f'/content/{card_id}')
            card_obj  = card_data.get('card', card_data)
            chapters  = card_obj.get('content', {}).get('chapters', []) or []

            # Preserve the full existing metadata (artwork, description, etc.)
            # Only set title if the card doesn't already have one.
            card_metadata = dict(card_obj.get('metadata', {}))
            if not card_metadata.get('title'):
                card_metadata['title'] = card_obj.get('title') or ''

            chapter_index = len(chapters)
            chapter_key   = str(chapter_index).zfill(2)
            track_obj = {
                'key':      chapter_key + '1',
                'title':    track_name,
                'type':     'audio',
                'trackUrl': track_url,
            }
            for field in ('duration', 'fileSize', 'channels', 'format'):
                if info.get(field) is not None:
                    track_obj[field] = info[field]
            new_chapter = {
                'key':          chapter_key,
                'title':        track_name,
                'overlayLabel': str(chapter_index + 1),
                'tracks':       [track_obj],
            }
            chapters.append(new_chapter)

            # POST with automatic retry: if an existing chapter references a media file
            # Yoto can't find (stale upload from before the S3 bug fix), strip it and
            # retry once rather than failing the whole upload.
            auto_stripped = []
            for attempt in range(2):
                try:
                    yoto_post(token, '/content', {
                        'cardId':   card_id,
                        'content':  {'chapters': chapters},
                        'metadata': card_metadata,
                    })
                    break   # success
                except RuntimeError as post_err:
                    err_str = str(post_err)
                    if attempt == 0 and 'Media file not found' in err_str:
                        # Parse the JSON body to get the clean track title
                        bad_title = None
                        json_start = err_str.find('{')
                        if json_start >= 0:
                            try:
                                parsed    = json.loads(err_str[json_start:])
                                api_msg   = parsed.get('error', {}).get('message', '')
                                title_hit = re.search(r'The track "([^"]+)"', api_msg)
                                if title_hit:
                                    bad_title = title_hit.group(1)
                            except Exception:
                                pass
                        if bad_title:
                            log_error(
                                f'_upload_core: stripping unplayable chapter '
                                f'{bad_title!r} from card {card_id} and retrying',
                            )
                            auto_stripped.append(bad_title)
                            chapters = [
                                ch for ch in chapters
                                if not any(t.get('title') == bad_title
                                           for t in ch.get('tracks', []))
                                and ch.get('title') != bad_title
                            ]
                            continue  # retry with bad chapter removed
                    raise   # non-recoverable — propagate

        warning = (f'⚠️ Removed unplayable track(s) from playlist: '
                   + ', '.join(f'"{t}"' for t in auto_stripped)) if auto_stripped else ''
        log_activity(
            f'_upload_core success  card_id={card_id}  track={track_name!r}'
            + (f'  stripped={auto_stripped}' if auto_stripped else '')
        )
        return True, warning
    except Exception as e:
        log_error(
            f'_upload_core error  card_id={card.get("cardId") or card.get("id", "?")}  '
            f'track={track_name!r}  file={file_path!r}  err={e}',
            exc=e,
        )
        return False, str(e)


def _safe_dirname(name: str) -> str:
    """Strip characters that are invalid in directory names on common OS."""
    return re.sub(r'[<>:"/\\|?*]', '-', name).strip()


def backup_track(file_path: str, track_name: str, playlist_title: str) -> None:
    """Copy the source audio file to a local backup directory (if configured).

    Reads bot_config.json fresh each call so runtime config changes are
    respected without a bot restart.  Never raises — backup failures are
    logged but do not affect the upload result.
    """
    try:
        raw = Path('bot_config.json').read_text(encoding='utf-8')
        cfg  = json.loads(raw)
        backup_cfg = cfg.get('backup', {})
        if not backup_cfg.get('enabled'):
            return

        backup_path = backup_cfg.get('path', '')
        mode        = backup_cfg.get('mode', 'organized')

        if not backup_path:
            log_error(f'backup failed — no path configured  track={track_name!r}')
            return

        if mode == 'organized':
            dest_dir = Path(backup_path) / _safe_dirname(playlist_title)
        else:
            dest_dir = Path(backup_path)

        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_file = dest_dir / Path(file_path).name

        if dest_file.exists():
            return  # already backed up — skip silently

        shutil.copy2(file_path, dest_file)
        log_activity(f'backup  track={track_name!r}  dest={str(dest_file)!r}')

    except Exception as e:
        log_error(f'backup failed  track={track_name!r}', exc=e)


def do_upload(bot_token: str, chat_id: int, msg_id: int,
               file_path: str, track_name: str, card: dict,
               raw_message: str = '', from_uid: int = 0) -> None:
    """Upload a single file to a Yoto playlist, sending Telegram status messages."""
    playlist_title = card_title(card)
    log_activity(f'upload start  track={track_name!r}  playlist={playlist_title!r}  file={file_path!r}')
    tg_send(bot_token, chat_id,
            f'⏳ Uploading *{track_name}* to *{playlist_title}*…',
            reply_to=msg_id)

    def _offer_queue_button(err_text: str) -> None:
        tg_send_keyboard(
            bot_token, chat_id,
            err_text,
            [[('📥 Add to queue', 'add_to_queue')]],
            reply_to=msg_id,
        )
        store_pending(chat_id, from_uid, file_path, track_name, [],
                      raw_message=raw_message,
                      type='upload_failed',
                      card=card)

    try:
        token = load_token()
    except TokenMissingError:
        log_error(f'upload failed — no token  track={track_name!r}  playlist={playlist_title!r}')
        _offer_queue_button(
            '❌ No Yoto token stored. Open the dashboard and log in first.'
        )
        return

    ok, err = _upload_core(file_path, track_name, card, token)
    if ok:
        log_activity(f'upload success  track={track_name!r}  playlist={playlist_title!r}')
        record_recent_playlist(card.get('cardId') or card.get('id', ''), playlist_title)
        backup_track(file_path, track_name, playlist_title)
        if err:   # non-empty = auto-strip warning
            tg_send(bot_token, chat_id, err)
        tg_send(bot_token, chat_id,
                f'✅ Added *{track_name}* to *{playlist_title}*!',
                reply_to=msg_id)
    else:
        log_error(f'upload failed  track={track_name!r}  playlist={playlist_title!r}  err={err}')
        _offer_queue_button(f'❌ Upload failed: `{err}`')


# ═══════════════════════════════════════════════════════════════════════════
#  YouTube URL handling — video and playlist pastes
# ═══════════════════════════════════════════════════════════════════════════

def _parse_yt_message(text: str) -> tuple:
    """Extract (youtube_url, playlist_name_or_None) from a message."""
    m = YT_URL_RE.search(text)
    url = m.group(0) if m else ''
    after = text[m.end():].strip() if m else ''
    playlist_name = after.lstrip('|').strip() or None
    return url, playlist_name


def _is_yt_playlist_url(url: str) -> bool:
    """True if URL is a playlist (not just a video that happens to be in a list)."""
    return 'list=' in url and 'watch?v=' not in url


def yt_get_playlist_info(url: str) -> list:
    """Return [{title, url}, ...] for all tracks in a YouTube playlist."""
    r = subprocess.run(
        _yt_dlp_cmd() + [
            '--flat-playlist', '--no-warnings',
            '--print', '%(title)s|||%(webpage_url)s',
            url,
        ],
        capture_output=True, text=True, timeout=60,
    )
    results = []
    for line in r.stdout.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split('|||', 1)
        if len(parts) == 2:
            results.append({'title': parts[0].strip(), 'url': parts[1].strip()})
    return results



def handle_youtube_url_message(bot_token: str, chat_id: int, from_uid: int,
                                msg_id: int, text: str) -> None:
    """Entry point when a message contains a YouTube URL (kept for direct messages)."""
    url, playlist_name = _parse_yt_message(text)
    if _is_yt_playlist_url(url):
        _handle_yt_playlist_url(bot_token, chat_id, from_uid, msg_id,
                                 url, playlist_name, text)
    else:
        _handle_yt_video_url(bot_token, chat_id, from_uid, msg_id,
                              url, playlist_name, text)


def _handle_yt_video_url(bot_token: str, chat_id: int, from_uid: int,
                          msg_id: int, url: str,
                          playlist_name: Optional[str], raw_message: str) -> None:
    """Download a single YouTube video and feed it into the normal upload flow."""
    tg_send(bot_token, chat_id, '🔍 Getting video info…', reply_to=msg_id)
    try:
        r = subprocess.run(
            _yt_dlp_cmd() + ['--no-playlist', '--print', '%(title)s', url],
            capture_output=True, text=True, timeout=30,
        )
        title = r.stdout.strip().split('\n')[0] or 'Unknown Track'
    except Exception as e:
        tg_send(bot_token, chat_id, f'❌ Could not get video info: `{e}`', reply_to=msg_id)
        return

    tg_send(bot_token, chat_id,
            f'⬇️ Downloading *{title}* as MP3…\n_(This may take a minute)_')
    try:
        mp3_path = yt_download_mp3(url, title)
    except Exception as e:
        tg_send(bot_token, chat_id,
                f'❌ Download failed: `{str(e)[:300]}`', reply_to=msg_id)
        return

    tg_send(bot_token, chat_id, f'✅ Downloaded *{title}*')
    _finish_find(bot_token, chat_id, from_uid, msg_id,
                 mp3_path, title, playlist_name, raw_message)


def _handle_yt_playlist_url(bot_token: str, chat_id: int, from_uid: int,
                              msg_id: int, url: str,
                              playlist_name: Optional[str], raw_message: str) -> None:
    """Fetch YouTube playlist info, then ask which Yoto playlist to add to."""
    tg_send(bot_token, chat_id, '🔍 Fetching playlist…', reply_to=msg_id)
    try:
        tracks = yt_get_playlist_info(url)
    except Exception as e:
        tg_send(bot_token, chat_id,
                f'❌ Could not fetch playlist: `{str(e)[:300]}`', reply_to=msg_id)
        return

    if not tracks:
        tg_send(bot_token, chat_id, '❌ No tracks found in that playlist.', reply_to=msg_id)
        return

    # Build preview
    lines = [f'📋 Found *{len(tracks)} track{"s" if len(tracks) != 1 else ""}*:\n']
    for i, t in enumerate(tracks[:5], 1):
        lines.append(f'  {i}. {t["title"]}')
    if len(tracks) > 5:
        lines.append(f'  _…and {len(tracks) - 5} more_')

    try:
        cards = fetch_cards()
    except Exception as e:
        tg_send(bot_token, chat_id,
                f'❌ Could not load Yoto library: `{e}`', reply_to=msg_id)
        return

    # If playlist name given and exact match found — go straight to confirm
    if playlist_name:
        card = find_card_exact(playlist_name, cards)
        if card:
            lines.append(f'\nAdd all to *{card_title(card)}*?')
            tg_send_keyboard(bot_token, chat_id, '\n'.join(lines),
                             [[('✅ Add all', 'confirm'), ('❌ Cancel', 'cancel')]],
                             reply_to=msg_id)
            store_pending(chat_id, from_uid, '', '', [],
                          type='yt_batch_confirm',
                          yt_url=url, yt_tracks=tracks, card=card,
                          raw_message=raw_message)
            return

    # Otherwise show Yoto playlist picker
    matches = fuzzy_match_cards(playlist_name or '', cards, n=3)
    lines.append('\nWhich Yoto playlist should these go into?')
    buttons = [
        [(f'{i}. {card_title(c)}', str(i))] for i, c in enumerate(matches, 1)
    ] + [[('➕ Create new playlist', 'create')], [('❌ Cancel', 'cancel')]]
    tg_send_keyboard(bot_token, chat_id, '\n'.join(lines), buttons, reply_to=msg_id)
    store_pending(chat_id, from_uid, '', '', matches,
                  type='yt_batch_select',
                  yt_url=url, yt_tracks=tracks,
                  create_name=playlist_name or '',
                  raw_message=raw_message)


def _handle_yt_batch_confirm_reply(bot_token: str, chat_id: int, from_uid: int,
                                    msg_id: int, text: str, pending: dict) -> None:
    """User confirmed (or cancelled) adding a YouTube playlist to a Yoto playlist."""
    key     = (chat_id, from_uid)
    cleaned = text.strip().lower()
    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return
    if cleaned != 'confirm':
        return  # stay silent
    card      = pending['card']
    yt_tracks = pending['yt_tracks']
    raw_msg   = pending.get('raw_message', '')
    del pending_matches[key]
    _save_pending()
    n = len(yt_tracks)
    _JOB_QUEUE.put({
        'bot_token': bot_token, 'chat_id': chat_id,
        'tracks': yt_tracks, 'card': card, 'raw_message': raw_msg,
    })
    tg_send(bot_token, chat_id,
            f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
            f'_(Download & upload running in background)_')


def _handle_yt_batch_select_reply(bot_token: str, chat_id: int, from_uid: int,
                                   msg_id: int, text: str, pending: dict) -> None:
    """User picked a Yoto playlist for a YouTube batch."""
    key     = (chat_id, from_uid)
    cleaned = text.strip().lower()
    yt_tracks = pending['yt_tracks']
    raw_msg   = pending.get('raw_message', '')

    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return

    if cleaned == 'create':
        suggested = pending.get('create_name', '')
        del pending_matches[key]
        _save_pending()
        # Store yt_tracks in a new create_name pending so we can kick off batch after creation
        _prompt_create_playlist(bot_token, chat_id, from_uid, msg_id,
                                suggested, file_path=None, track_name=None,
                                raw_message=raw_msg,
                                yt_tracks=yt_tracks)
        return

    if cleaned.isdigit():
        idx = int(cleaned) - 1
        candidates = pending['candidates']
        if idx < 0 or idx >= len(candidates):
            return
        card = candidates[idx]
        del pending_matches[key]
        _save_pending()
        n = len(yt_tracks)
        _JOB_QUEUE.put({
            'bot_token': bot_token, 'chat_id': chat_id,
            'tracks': yt_tracks, 'card': card, 'raw_message': raw_msg,
        })
        tg_send(bot_token, chat_id,
                f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                f'_(Download & upload running in background)_')


def _do_yt_batch_upload(bot_token: str, chat_id: int, card: dict,
                         tracks: list, raw_message: str) -> None:
    """Download and upload each track in a YouTube playlist. Runs in its own thread.

    Parallelism strategy:
      • Downloads (yt-dlp + optional ffmpeg transcode) run in parallel — up to
        MAX_WORKERS concurrent threads via ThreadPoolExecutor.
      • Uploads (S3 PUT + transcode poll + POST /content) run sequentially in
        the main thread, driven by concurrent.futures.as_completed() so each
        upload starts as soon as its download finishes rather than waiting for
        the whole batch.  Cross-thread serialisation of the chapter-list RMW
        (e.g. another batch or a single /find upload to the same card) is
        handled inside _upload_core via _CONTENT_LOCK.

    Notifications: for playlists with more than 10 tracks progress messages are
    throttled to one every 5 minutes.  Errors always surface immediately.  A
    final summary is always sent.
    """
    import concurrent.futures

    THROTTLE_THRESHOLD = 10          # only throttle for batches larger than this
    THROTTLE_INTERVAL  = 5 * 60      # seconds between progress messages
    MAX_WORKERS        = 3

    total       = len(tracks)
    failed      = []                 # [{title, url, error}]
    failed_lock = threading.Lock()
    throttle    = total > THROTTLE_THRESHOLD

    # Progress counters — only touched in the main (upload) thread, but keep
    # the lock for safety in case the structure ever changes.
    done_count       = [0]
    last_notify_time = [time.time()]
    progress_lock    = threading.Lock()

    log_activity(f'yt_batch start  playlist={card_title(card)!r}  tracks={total}  workers={MAX_WORKERS}')
    try:
        token = load_token()
    except TokenMissingError:
        log_error(f'yt_batch failed — no token  playlist={card_title(card)!r}')
        tg_send(bot_token, chat_id,
                '❌ No Yoto token — open the dashboard and log in first.')
        return

    # ── Download phase (runs in parallel worker threads) ──────────────────────
    def download_phase(i: int, track: dict) -> tuple:
        """Download + optional OGG transcode.  Returns (i, track, file_path).
        Raises on unrecoverable download failure (caller records the error)."""
        url   = track['url']
        title = track['title']

        mp3_path   = None
        last_error = None
        for attempt in range(2):
            try:
                mp3_path = yt_download_mp3(url, title)
                break
            except Exception as e:
                last_error = e
                if attempt == 0:
                    time.sleep(5)

        if not mp3_path:
            raise last_error  # caught in the main loop below

        # Pre-transcode to OGG/Opus so server-side transcoding is near-instant.
        # Falls back silently to the raw MP3 if ffmpeg is unavailable or fails.
        file_path = mp3_path
        try:
            ogg = transcode_to_ogg(mp3_path)
            if ogg:
                file_path = ogg
        except Exception as e:
            log_activity(f'yt_batch transcode skip  track={title!r}  reason={e}')

        return (i, track, file_path)

    # ── Submit all downloads; process each upload as soon as its download done ─
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        download_futures = {
            pool.submit(download_phase, i, track): (i, track)
            for i, track in enumerate(tracks, 1)
        }

        for fut in concurrent.futures.as_completed(download_futures):
            i, track = download_futures[fut]
            title = track['title']
            url   = track['url']

            # ── Collect download result ────────────────────────────────────
            try:
                _, _, file_path = fut.result()
            except Exception as e:
                err_msg = str(e)
                log_error(f'yt_batch download fail  track={title!r}  url={url!r}  err={err_msg}',
                          exc=e)
                tg_send(bot_token, chat_id,
                        f'⚠️ Skipping *{title}*\n`{err_msg[:200]}`')
                with failed_lock:
                    failed.append({'title': title, 'url': url, 'error': err_msg})
                continue

            # ── Upload — sequential within this batch; cross-thread same-card
            #    safety is provided by _CONTENT_LOCK inside _upload_core.
            ok, msg = _upload_core(file_path, title, card, token)
            if not ok:
                time.sleep(5)
                ok, msg = _upload_core(file_path, title, card, token)

            if not ok:
                log_error(f'yt_batch upload fail (2 attempts)  track={title!r}  err={msg}')
                tg_send(bot_token, chat_id,
                        f'⚠️ Upload failed for *{title}*\n`{msg[:200]}`')
                with failed_lock:
                    failed.append({'title': title, 'url': url, 'error': msg})
                continue

            if msg:  # ok=True but carries a strip warning
                tg_send(bot_token, chat_id, msg)

            backup_track(file_path, title, card_title(card))
            log_activity(f'yt_batch track ok  {i}/{total}  {title!r}  playlist={card_title(card)!r}')

            # ── Progress notification (throttled for large batches) ────────
            with progress_lock:
                done_count[0] += 1
                done = done_count[0]
                now  = time.time()
                if throttle:
                    should_notify = (now - last_notify_time[0]) >= THROTTLE_INTERVAL
                else:
                    should_notify = True
                if should_notify:
                    last_notify_time[0] = now

            if should_notify:
                if throttle:
                    tg_send(bot_token, chat_id,
                            f'⏳ {done}/{total} tracks done — *{card_title(card)}*…')
                else:
                    tg_send(bot_token, chat_id,
                            f'✔️ {i}/{total} — *{title}*')

    # ── Final summary ──────────────────────────────────────────────────────────
    success = total - len(failed)
    if success > 0:
        # At least one track landed — bump this playlist to the top of recents
        record_recent_playlist(card.get('cardId') or card.get('id', ''), card_title(card))
    if not failed:
        log_activity(f'yt_batch complete  playlist={card_title(card)!r}  all {total} ok')
        tg_send(bot_token, chat_id,
                f'✅ Done! All {total} tracks added to *{card_title(card)}*.')
    else:
        log_activity(
            f'yt_batch complete  playlist={card_title(card)!r}  '
            f'{success}/{total} ok  {len(failed)} failed'
        )
        lines = [f'✅ {success}/{total} tracks added to *{card_title(card)}*.']
        lines.append('\n⚠️ Failed:')
        for f in failed:
            lines.append(f'  • *{f["title"]}*: `{f["error"][:120]}`')
        tg_send(bot_token, chat_id, '\n'.join(lines))


def get_file_bytes(source: str) -> tuple[bytes, str]:
    """
    Get file bytes and filename from a source string.
    Currently: local file path only.
    Future extension points:
      - If source starts with 'https://' → download via urllib
      - If source is a Telegram file_id   → call getFile bot API
    """
    path = Path(source)
    return path.read_bytes(), path.name


# ═══════════════════════════════════════════════════════════════════════════
#  PHASE 5 — Fuzzy card matching
# ═══════════════════════════════════════════════════════════════════════════

_card_cache: Optional[tuple[float, list]] = None
CARD_CACHE_TTL = 60  # seconds


def fetch_cards() -> list:
    """Fetch the user's Yoto card library, cached for 60 seconds."""
    global _card_cache
    # Snapshot the global because another thread may set it to None
    # (cache invalidation in _do_create_playlist) between checks.
    cache = _card_cache
    now = time.time()
    if cache and (now - cache[0]) < CARD_CACHE_TTL:
        return cache[1]
    token = load_token()
    data  = yoto_get(token, '/content/mine')
    cards = data.get('cards') or (data if isinstance(data, list) else [])
    _card_cache = (now, cards)
    return cards


def card_title(card: dict) -> str:
    return (card.get('title')
            or card.get('metadata', {}).get('title', '')
            or card.get('cardId', 'Unknown'))


def find_card_exact(name: str, cards: list) -> Optional[dict]:
    """Case-insensitive exact match."""
    name_lower = name.lower()
    for card in cards:
        if card_title(card).lower() == name_lower:
            return card
    return None


def fuzzy_match_cards(query: str, cards: list, n: int = 3) -> list:
    """Return up to n cards whose titles are closest to query, best match first."""
    titles = [card_title(c) for c in cards]
    if query:
        close = difflib.get_close_matches(query, titles, n=n, cutoff=0.3)
        # Build a title → card map (first occurrence wins on duplicate titles).
        # Then return in difflib order so the best match comes first.
        title_to_card: dict = {}
        for c in cards:
            t = card_title(c)
            if t not in title_to_card:
                title_to_card[t] = c
        return [title_to_card[t] for t in close if t in title_to_card]
    else:
        # No query — just return the first n cards alphabetically
        sorted_cards = sorted(cards, key=lambda c: card_title(c).lower())
        return sorted_cards[:n]


# ═══════════════════════════════════════════════════════════════════════════
#  MUSIC FIND — Apple Music search + YouTube fallback
# ═══════════════════════════════════════════════════════════════════════════

# ── Apple Music helpers (macOS only, via osascript) ────────────────────────

def am_search(query: str, offset: int = 0) -> list:
    """
    Search the local Music library for tracks matching query.
    Returns list of {id, title, artist, album}.  Requires macOS + Music app.
    offset skips the first N results, allowing batched pagination beyond 40.
    """
    safe = query.replace('"', "'").replace('\\', '').replace('\n', ' ')[:200]
    script = (
        'tell application "Music"\n'
        '    set sr to search library playlist 1 for "' + safe + '"\n'
        '    set out to ""\n'
        '    set skip to 0\n'
        '    set n to 0\n'
        '    repeat with t in sr\n'
        '        if skip < ' + str(offset) + ' then\n'
        '            set skip to skip + 1\n'
        '        else\n'
        '            if n ≥ 40 then exit repeat\n'
        '            set cs to "unknown"\n'
        '            try\n'
        '                set cs to cloud status of t as string\n'
        '            end try\n'
        '            set out to out & (database id of t as string) & "|||"'
        ' & (name of t) & "|||" & (artist of t) & "|||" & (album of t) & "|||" & cs & "\n"\n'
        '            set n to n + 1\n'
        '        end if\n'
        '    end repeat\n'
        '    return out\n'
        'end tell'
    )
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            print(f'  am_search stderr: {r.stderr.strip()[:200]}')
            return []
        tracks = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('|||')
            if len(parts) >= 4:
                tracks.append({
                    'id':           parts[0].strip(),
                    'title':        parts[1].strip(),
                    'artist':       parts[2].strip(),
                    'album':        parts[3].strip(),
                    'cloud_status': parts[4].strip() if len(parts) > 4 else 'unknown',
                })
        return tracks
    except Exception as e:
        print(f'  am_search error: {e}')
        return []


def _am_get_location(track_id: str, verbose: bool = False) -> Optional[str]:
    """Return the local POSIX path of a Music track, or None if not downloaded."""
    try:
        tid = int(track_id)
    except (TypeError, ValueError):
        _activity_logger.info(f'am_location: invalid track_id {track_id!r}')
        return None
    # Ask for both location and iCloud status in one call so we can log both
    script = (
        'tell application "Music"\n'
        '    set t to (first track of library playlist 1 whose database id is '
        + str(tid) + ')\n'
        '    set cloudStatus to "unknown"\n'
        '    try\n'
        '        set cloudStatus to cloud status of t as string\n'
        '    end try\n'
        '    set locPath to ""\n'
        '    try\n'
        '        if location of t is not missing value then\n'
        '            set locPath to POSIX path of (location of t)\n'
        '        end if\n'
        '    end try\n'
        '    return cloudStatus & "|||" & locPath\n'
        'end tell'
    )
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=15)
        raw = r.stdout.strip()
        stderr = r.stderr.strip()
        parts = raw.split('|||', 1)
        cloud_status = parts[0].strip() if parts else 'unknown'
        path = parts[1].strip() if len(parts) > 1 else ''
        if verbose:
            exists = Path(path).exists() if path else False
            _activity_logger.info(f'am_location [{track_id}]: cloud={cloud_status!r} path={path!r} exists={exists} stderr={stderr!r}')
        if path and Path(path).exists():
            return path
        return None
    except Exception as e:
        _activity_logger.info(f'am_location error: {e}')
        return None


def _am_stop_and_unmute() -> None:
    """Stop Music playback and restore its volume to 100."""
    script = (
        'tell application "Music"\n'
        '    try\n'
        '        stop\n'
        '    end try\n'
        '    try\n'
        '        set sound volume to 100\n'
        '    end try\n'
        'end tell'
    )
    try:
        subprocess.run(['osascript', '-e', script],
                       capture_output=True, text=True, timeout=10)
        _activity_logger.info('am_download: stopped Music and restored volume')
    except Exception as e:
        _activity_logger.info(f'am_stop_and_unmute error: {e}')


def _am_cloud_status(track_id: str) -> str:
    """Return the current cloud status for a Music track, or 'unknown' on error."""
    try:
        tid = int(track_id)
    except (TypeError, ValueError):
        return 'unknown'
    script = (
        'tell application "Music"\n'
        '    set t to (first track of library playlist 1 whose database id is '
        + str(tid) + ')\n'
        '    set cs to "unknown"\n'
        '    try\n'
        '        set cs to cloud status of t as string\n'
        '    end try\n'
        '    return cs\n'
        'end tell'
    )
    try:
        r = subprocess.run(['osascript', '-e', script],
                           capture_output=True, text=True, timeout=15)
        return r.stdout.strip() or 'unknown'
    except Exception as e:
        print(f'  am_cloud_status error: {e}')
        return 'unknown'


def _am_glob_file(title: str, artist: str = '') -> Optional[str]:
    """Search ~/Music/ recursively for a downloaded file matching title+artist."""
    if not title:
        return None
    # Search all of ~/Music/ so we catch moved/relocated libraries too
    search_root = Path.home() / 'Music'
    if not search_root.exists():
        return None
    safe_title = re.sub(r'[^\w\s]', '', title).strip()[:50]
    if not safe_title:
        return None
    safe_artist = re.sub(r'[^\w\s]', '', artist).strip()[:40] if artist else ''

    # Build progressively shorter title variants: full name → separator prefix → first word.
    # Needed because Music.app track names often include extra words not in the filename
    # (e.g. "Dela I Think I Know Why…" → filename is just "Dela.m4a").
    title_variants: list[str] = [safe_title]
    for sep in (' - ', ' (', ': '):
        if sep in safe_title:
            shorter = safe_title.split(sep)[0].strip()
            if shorter and shorter not in title_variants:
                title_variants.append(shorter)
    first_word = safe_title.split()[0] if safe_title.split() else ''
    if first_word and first_word not in title_variants:
        title_variants.append(first_word)

    _activity_logger.info(f'am_glob_file: searching {search_root} for {title_variants!r} ({safe_artist!r})')

    def _best_hit(hits: list) -> Optional[str]:
        if safe_artist:
            artist_hits = [h for h in hits if safe_artist.lower() in str(h).lower()]
            if artist_hits:
                return str(artist_hits[0])
        return str(hits[0]) if hits else None

    for variant in title_variants:
        for ext in ('m4a', 'm4p', 'mp3', 'aac'):
            hits = list(search_root.glob(f'**/*{variant}*.{ext}'))
            hit = _best_hit(hits)
            if hit:
                _activity_logger.info(f'am_glob_file: match via variant {variant!r} → {hit}')
                return hit

    _activity_logger.info(f'am_glob_file: no match found for {title_variants!r}')
    return None


def am_download(track_id: str, title: str = '', artist: str = '') -> Optional[str]:
    """
    Trigger a Music.app download and return the local POSIX path once the file appears.

    Primary trigger: AppleScript `startdownload t`.
    Fallback trigger (if startdownload errors): muted play+stop, which forces buffering.
    Polls both Music.app's location property and the filesystem for up to 10 minutes.
    Returns None if the download fails or times out.
    """
    try:
        tid = int(track_id)
    except (TypeError, ValueError):
        _activity_logger.info(f'am_download: invalid track_id {track_id!r}')
        return None

    # Step 1 — check if already local, collect metadata, trigger download.
    # Uses startdownload (correct Music.app command); falls back to muted play+stop.
    trigger_script = (
        'tell application "Music"\n'
        '    set t to (first track of library playlist 1 whose database id is '
        + str(tid) + ')\n'
        '    set tName to name of t\n'
        '    set tArtist to ""\n'
        '    try\n'
        '        set tArtist to artist of t\n'
        '    end try\n'
        '    set cs to "unknown"\n'
        '    try\n'
        '        set cs to cloud status of t as string\n'
        '    end try\n'
        '    try\n'
        '        if location of t is not missing value then\n'
        '            return "local|||" & POSIX path of (location of t) & "|||" & cs & "|||" & tName & "|||" & tArtist\n'
        '        end if\n'
        '    end try\n'
        '    set triggerMethod to "none"\n'
        '    try\n'
        '        startdownload t\n'
        '        set triggerMethod to "startdownload"\n'
        '    end try\n'
        '    if triggerMethod is "none" then\n'
        '        try\n'
        '            set sound volume to 0\n'
        '            set rating of t to rating of t\n'
        '            play t\n'
        '            delay 3\n'
        '            stop\n'
        '            set sound volume to 100\n'
        '            set triggerMethod to "playStop"\n'
        '        end try\n'
        '    end if\n'
        '    return "downloading|||" & cs & "|||" & tName & "|||" & tArtist & "|||" & triggerMethod\n'
        'end tell'
    )
    try:
        r = subprocess.run(['osascript', '-e', trigger_script],
                           capture_output=True, text=True, timeout=30)
        raw = r.stdout.strip()
        stderr = r.stderr.strip()
        _activity_logger.info(f'am_download trigger [{track_id}]: raw={raw!r} stderr={stderr!r}')
        parts = raw.split('|||')
        status_token = parts[0] if parts else ''
        if status_token == 'local':
            loc = parts[1] if len(parts) > 1 else ''
            if loc and Path(loc).exists():
                _activity_logger.info(f'am_download [{track_id}]: already local → {loc}')
                return loc
            _activity_logger.info(f'am_download [{track_id}]: location returned but file absent, polling…')
        elif status_token == 'downloading':
            initial_cloud = parts[1] if len(parts) > 1 else 'unknown'
            if not title and len(parts) > 2:
                title = parts[2]
            if not artist and len(parts) > 3:
                artist = parts[3]
            trigger_method = parts[4] if len(parts) > 4 else 'unknown'
            _activity_logger.info(f'am_download [{track_id}]: triggered via {trigger_method!r}, '
                                  f'cloud={initial_cloud!r} title={title!r} artist={artist!r}')
        else:
            _activity_logger.info(f'am_download [{track_id}]: unexpected trigger response {raw!r}, proceeding to poll')
    except Exception as e:
        _activity_logger.info(f'am_download trigger error: {e}')
        return None

    # Step 2 — poll for up to 10 minutes.
    # Log before each sleep so the activity log shows trigger results immediately.
    _activity_logger.info(f'am_download [{track_id}]: starting poll loop '
                          f'(title={title!r} artist={artist!r})…')
    deadline = time.time() + 600
    poll = 0
    found_path = None
    last_loc_result = None
    last_fs_result = None
    while time.time() < deadline:
        poll += 1
        elapsed = (poll - 1) * 3
        _activity_logger.info(f'am_download [{track_id}]: poll {poll} ({elapsed}s) — checking…')

        loc_path = _am_get_location(track_id, verbose=True)
        last_loc_result = loc_path
        if loc_path:
            _activity_logger.info(f'am_download [{track_id}]: Music.app location ready '
                                  f'after {elapsed}s → {loc_path}')
            found_path = loc_path
            break

        fs_path = _am_glob_file(title, artist)
        last_fs_result = fs_path
        if fs_path:
            _activity_logger.info(f'am_download [{track_id}]: filesystem hit after {elapsed}s → {fs_path}')
            found_path = fs_path
            break

        # Check iTunes Match temp folder so we know a download IS in flight even
        # before Music.app moves the file to its final library location.
        # Active downloads land at: ~/Music/Music/Media.localized/Downloads-Music/<Name>.tmp/download.m4a
        if title:
            _fw_parts = re.sub(r'[^\w\s]', '', title).strip().split()
            _fw = _fw_parts[0] if _fw_parts else ''
            if _fw:
                _dl_dir = Path.home() / 'Music/Music/Media.localized/Downloads-Music'
                if _dl_dir.exists():
                    _tmp_dirs = [d for d in _dl_dir.iterdir()
                                 if d.is_dir() and d.name.endswith('.tmp')
                                 and _fw.lower() in d.name.lower()]
                    if _tmp_dirs:
                        _activity_logger.info(
                            f'am_download [{track_id}]: temp folder detected '
                            f'{_tmp_dirs[0].name!r} — download in progress, continuing to poll…')

        _activity_logger.info(f'am_download [{track_id}]: poll {poll} ({elapsed}s): no file yet — sleeping 3s')
        if time.time() < deadline:
            time.sleep(3)

    # Always stop Music and restore volume before returning
    _am_stop_and_unmute()

    if found_path:
        return found_path

    # Timeout — emit diagnostic info so we can see exactly what Music.app and the
    # filesystem reported on the last poll
    _activity_logger.info(f'am_download [{track_id}]: timed out after {poll} polls (~{(poll - 1) * 3}s)')
    _activity_logger.info(f'am_download [{track_id}]: last _am_get_location={last_loc_result!r}')
    _activity_logger.info(f'am_download [{track_id}]: last _am_glob_file={last_fs_result!r}')
    sample = list((Path.home() / 'Music').rglob('*.m4a'))[:5]
    _activity_logger.info(f'am_download [{track_id}]: ~/Music *.m4a sample: {[str(p) for p in sample]}')
    return None


def am_copy_to_temp(source_path: str, title: str, artist: str = '') -> str:
    """Copy a Music-app file into TEMP_DIR and return the new path."""
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    src  = Path(source_path)
    name = f'{artist} - {title}' if artist else title
    safe = re.sub(r'[^\w\s\-]', '', name)[:80].strip()
    dest = TEMP_DIR / f'{safe}{src.suffix}'
    shutil.copy2(str(src), str(dest))
    return str(dest)


# ── YouTube helpers (yt-dlp required) ─────────────────────────────────────

def _yt_dlp_cmd() -> list:
    """Return the command prefix for yt-dlp.
    Prefers a standalone yt-dlp binary (e.g. from Homebrew, which bundles its
    own Python 3.12+) so we're not limited by the server's Python version.
    Falls back to the module form if no binary is found."""
    candidates = [
        '/opt/homebrew/bin/yt-dlp',      # Homebrew — Apple Silicon
        '/usr/local/bin/yt-dlp',          # Homebrew — Intel
        os.path.expanduser('~/.local/bin/yt-dlp'),  # pip user install
        shutil.which('yt-dlp'),           # anything on PATH
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            print(f'  yt-dlp binary: {candidate}')
            return [candidate]
    # Try asking the shell — catches Homebrew paths not in Python's PATH
    try:
        r = subprocess.run(
            ['sh', '-c', 'command -v yt-dlp'],
            capture_output=True, text=True, timeout=5,
        )
        path = r.stdout.strip()
        if path and Path(path).exists():
            print(f'  yt-dlp binary (via shell): {path}')
            return [path]
    except Exception:
        pass
    # Fall back to module form using current Python
    print(f'  yt-dlp: no binary found anywhere, falling back to {sys.executable} -m yt_dlp')
    print(f'  yt-dlp: checked: {[c for c in candidates if c]}')
    return [sys.executable, '-m', 'yt_dlp']


def yt_search(query: str, n: int = 9) -> list:
    """Return top n YouTube results for query. Raises RuntimeError if yt-dlp missing."""
    try:
        r = subprocess.run(
            _yt_dlp_cmd() + ['--no-playlist',
             '--print', '%(title)s|||%(webpage_url)s|||%(channel)s|||%(duration_string)s',
             f'ytsearch{n}:{query}'],
            capture_output=True, text=True, timeout=30,
        )
        _yt_combined = r.stdout + r.stderr
        print(f'  yt_search returncode={r.returncode}')
        print(f'  yt_search stdout={r.stdout[:500]!r}')
        print(f'  yt_search stderr={r.stderr[:500]!r}')
        if r.returncode != 0 and 'No module named' in _yt_combined:
            raise RuntimeError('yt-dlp not installed — run: pip3 install yt-dlp')
        if 'Python version' in _yt_combined and 'eprecated' in _yt_combined:
            raise RuntimeError('yt-dlp requires Python 3.10+. Fix: pip3 install "yt-dlp==2024.10.7"')
        results = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('|||')
            if len(parts) >= 2:
                dur = parts[3].strip() if len(parts) > 3 else ''
                results.append({
                    'title':    parts[0].strip(),
                    'url':      parts[1].strip(),
                    'channel':  parts[2].strip() if len(parts) > 2 else '',
                    'duration': dur if dur and dur != 'NA' else '',
                })
        print(f'  yt_search parsed {len(results)} results')
        return results[:n]
    except RuntimeError:
        raise
    except Exception as e:
        print(f'  yt_search error: {e}')
        return []


def yt_download_mp3(url: str, title: str) -> str:
    """Download YouTube audio as MP3 to TEMP_DIR. Raises on failure.

    Uses a uuid-suffixed base filename so concurrent batch downloads can't
    collide (yt-dlp would otherwise overwrite, and the 'newest mp3' fallback
    could pick up a sibling worker's file).
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    safe   = re.sub(r'[^\w\s\-]', '', title)[:60].strip() or 'track'
    base   = f'{safe}_{uuid.uuid4().hex[:8]}'
    out_tmpl = str(TEMP_DIR / f'{base}.%(ext)s')
    try:
        r = subprocess.run(
            _yt_dlp_cmd() + ['-x', '--audio-format', 'mp3', '--audio-quality', '0',
             '-o', out_tmpl, '--no-playlist', url],
            capture_output=True, text=True, timeout=300,
        )
        _yt_combined = r.stdout + r.stderr
        if 'Python version' in _yt_combined and 'eprecated' in _yt_combined:
            raise RuntimeError('yt-dlp requires Python 3.10+. Fix: pip3 install "yt-dlp==2024.10.7"')
        if r.returncode != 0:
            if 'No module named' in _yt_combined:
                raise RuntimeError('yt-dlp not installed — run: pip3 install yt-dlp')
            raise RuntimeError(r.stderr[-400:] or 'yt-dlp returned non-zero')
        mp3 = TEMP_DIR / f'{base}.mp3'
        if mp3.exists() and mp3.stat().st_size > 0:
            return str(mp3)
        # yt-dlp may have sanitised the filename — match against our unique base
        mp3s = sorted(
            (p for p in TEMP_DIR.glob(f'*{base}*.mp3') if p.stat().st_size > 0),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if mp3s:
            return str(mp3s[0])
        raise RuntimeError('yt-dlp finished but no MP3 found in temp dir')
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(str(e))


def transcode_to_ogg(mp3_path: str) -> str | None:
    """
    Transcode an MP3 to OGG/Opus using Yoto's target loudnorm settings so that
    server-side transcoding completes almost instantly.

    Uses a two-pass loudnorm approach (measure first, then encode) for accurate
    normalisation.  Falls back to a single-pass if the measurement step fails.
    Returns the path of the new .ogg file, or None if ffmpeg is not installed.
    Raises RuntimeError on encode failure.

    Yoto target settings (from observed transcode responses):
      codec: libopus  format: ogg  bitrate: 64k
      loudnorm: I=-16  LRA=7  TP=-2  linear=true  dual_mono=true
    """
    if not shutil.which('ffmpeg'):
        return None

    # Reject empty/missing input early so we don't spend 5 minutes uploading
    # a 0-byte file the server can never transcode.
    in_path = Path(mp3_path)
    if not in_path.exists():
        raise RuntimeError(f'transcode_to_ogg: input missing: {mp3_path}')
    if in_path.stat().st_size == 0:
        raise RuntimeError(f'transcode_to_ogg: input is empty: {mp3_path}')

    ogg_path = mp3_path.rsplit('.', 1)[0] + '.ogg'

    # ── Pass 1: measure input loudness ────────────────────────────────────
    af_final = 'loudnorm=I=-16:LRA=7:TP=-2:linear=true:dual_mono=true'  # single-pass fallback
    try:
        r1 = subprocess.run(
            ['ffmpeg', '-y', '-i', mp3_path,
             '-af', 'loudnorm=I=-16:LRA=7:TP=-2:print_format=json',
             '-f', 'null', '-'],
            capture_output=True, text=True, timeout=120,
        )
        # ffmpeg writes the loudnorm JSON block to stderr
        m = re.search(r'\{[^{}]+\}', r1.stderr, re.DOTALL)
        if m:
            stats = json.loads(m.group())
            required = ('input_i', 'input_lra', 'input_tp',
                        'input_thresh', 'target_offset')
            # Reject -inf / nan readings (silent input, audio-less file) which
            # would make the second-pass filter fail.
            if all(k in stats for k in required) and not any(
                str(stats[k]).lower() in ('-inf', 'inf', 'nan')
                for k in required
            ):
                af_final = (
                    'loudnorm=I=-16:LRA=7:TP=-2'
                    f':measured_I={stats["input_i"]}'
                    f':measured_LRA={stats["input_lra"]}'
                    f':measured_TP={stats["input_tp"]}'
                    f':measured_thresh={stats["input_thresh"]}'
                    f':offset={stats["target_offset"]}'
                    ':linear=true:dual_mono=true'
                )
    except Exception as e:
        log_activity(f'transcode_to_ogg: loudnorm pass-1 skipped ({e}), using single-pass')

    # ── Pass 2: encode to OGG/Opus ────────────────────────────────────────
    r2 = subprocess.run(
        ['ffmpeg', '-y', '-i', mp3_path,
         '-c:a', 'libopus', '-b:a', '64k', '-vbr', 'on',
         '-af', af_final,
         ogg_path],
        capture_output=True, text=True, timeout=300,
    )
    if r2.returncode != 0:
        raise RuntimeError(f'ffmpeg OGG encode failed: {r2.stderr[-400:]}')

    # Sanity-check the output: ffmpeg can return 0 yet leave a tiny/empty file
    # when the input has no audio stream.  Anything under ~1 KB is too small
    # to be real Opus audio.
    out = Path(ogg_path)
    if not out.exists() or out.stat().st_size < 1024:
        raise RuntimeError(
            f'ffmpeg produced empty/tiny OGG '
            f'({out.stat().st_size if out.exists() else "missing"} bytes): {ogg_path}'
        )

    log_activity(f'transcode_to_ogg: ok  {Path(mp3_path).name} → {Path(ogg_path).name}')
    return ogg_path


# ── /find command entry point ──────────────────────────────────────────────

def handle_find_command(bot_token: str, chat_id: int, from_uid: int,
                        msg_id: int, text: str) -> None:
    """
    /find Song Title [| Playlist Name]
    Searches Apple Music first; falls back to YouTube.
    """
    rest = re.sub(r'^/find(@\S+)?\s*', '', text, flags=re.IGNORECASE).strip()
    if not rest:
        rest = re.sub(r'^find\s+', '', text, flags=re.IGNORECASE).strip()
    if not rest:
        tg_send(bot_token, chat_id,
                '❓ Usage:\n`/find Song Title`\nor\n`/find Song Title | Playlist Name`',
                reply_to=msg_id)
        return

    parts         = [p.strip() for p in rest.split('|', 1)]
    query         = parts[0]
    playlist_name = parts[1] if len(parts) > 1 else None

    tg_send(bot_token, chat_id,
            f'🔍 Searching Apple Music for *{query}*…', reply_to=msg_id)

    tracks = am_search(query)

    _CLOUD_DISPLAY_STATUSES = {'matched', 'purchased', 'uploaded', 'subscription'}
    if tracks:
        def _cloud_badge(t: dict) -> str:
            return ' ☁️' if t.get('cloud_status', '') in _CLOUD_DISPLAY_STATUSES else ''

        total      = len(tracks)
        end        = min(_AM_PAGE_SIZE, total)
        fetch_more = total > 0 and total % 40 == 0
        total_str  = f'{total}+' if fetch_more else str(total)
        if total == 1:
            header = '🎵 Found 1 match in Apple Music:\n'
        else:
            header = f'🎵 Found {total_str} matches in Apple Music (1–{end} of {total_str}):\n'
        lines = [header, '_Tap tracks to select, then tap Done._']
        for i, t in enumerate(tracks[:end], 1):
            lines.append(f'  {i}. *{t["title"]}* — {t["artist"]}  _{t["album"]}_{_cloud_badge(t)}')

        buttons = _am_multiselect_buttons(tracks, selected=[], page=0, fetch_more=fetch_more)
        kbd_mid = tg_send_keyboard_ret(bot_token, chat_id, '\n'.join(lines), buttons)

        store_pending(chat_id, from_uid, '', query, tracks,
                      raw_message=text,
                      type='am_confirm',
                      playlist_name=playlist_name,
                      query=query,
                      selected=[],
                      am_page=0,
                      keyboard_msg_id=kbd_mid)
    else:
        tg_send(bot_token, chat_id,
                f'🔍 Not in your Apple Music library — checking YouTube…')
        _do_youtube_search(bot_token, chat_id, from_uid, msg_id,
                           query, playlist_name, text)


# ── /findplay command — search YouTube for playlists ──────────────────────

def handle_findplay_command(bot_token: str, chat_id: int, from_uid: int,
                             msg_id: int, text: str) -> None:
    """
    /findplay Query [| Playlist Name]
    Searches YouTube for playlists; user picks one, then picks tracks or adds all.
    """
    rest = re.sub(r'^/findplay(@\S+)?\s*', '', text, flags=re.IGNORECASE).strip()
    if not rest:
        rest = re.sub(r'^findplay\s+', '', text, flags=re.IGNORECASE).strip()
    if not rest:
        tg_send(bot_token, chat_id,
                '❓ Usage:\n`/findplay Playlist Query`\nor\n`/findplay Playlist Query | Yoto Playlist Name`',
                reply_to=msg_id)
        return

    parts         = [p.strip() for p in rest.split('|', 1)]
    query         = parts[0]
    playlist_name = parts[1] if len(parts) > 1 else None

    _do_youtube_playlist_search(bot_token, chat_id, from_uid, msg_id,
                                query, playlist_name, text)


def yt_search_playlists(query: str, n: int = 9) -> list:
    """Search YouTube specifically for playlists using the playlist filter URL.

    Uses --flat-playlist so each result is a single playlist entry (not expanded).
    Returns [{title, url, channel, duration}, ...].
    """
    encoded = urllib.parse.quote(query)
    # sp=EgIQAw%3D%3D is YouTube's built-in "Playlists" search filter
    search_url = f'https://www.youtube.com/results?search_query={encoded}&sp=EgIQAw%3D%3D'
    try:
        r = subprocess.run(
            _yt_dlp_cmd() + [
                '--flat-playlist', '--no-warnings',
                '--print', '%(title)s|||%(webpage_url)s|||%(uploader)s',
                search_url,
            ],
            capture_output=True, text=True, timeout=30,
        )
        _yt_combined = r.stdout + r.stderr
        print(f'  yt_search_playlists returncode={r.returncode}')
        print(f'  yt_search_playlists stdout={r.stdout[:500]!r}')
        print(f'  yt_search_playlists stderr={r.stderr[:300]!r}')
        if r.returncode != 0 and 'No module named' in _yt_combined:
            raise RuntimeError('yt-dlp not installed — run: pip3 install yt-dlp')
        if 'Python version' in _yt_combined and 'eprecated' in _yt_combined:
            raise RuntimeError('yt-dlp requires Python 3.10+. Fix: pip3 install "yt-dlp==2024.10.7"')
        results = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('|||')
            if len(parts) >= 2:
                url = parts[1].strip()
                # Only keep actual playlist URLs (contain /playlist? or list=)
                if 'playlist' not in url and 'list=' not in url:
                    continue
                results.append({
                    'title':    parts[0].strip(),
                    'url':      url,
                    'channel':  parts[2].strip() if len(parts) > 2 else '',
                    'duration': '',  # playlists don't have a single duration
                })
            if len(results) >= n:
                break
        print(f'  yt_search_playlists parsed {len(results)} playlist results')
        return results
    except RuntimeError:
        raise
    except Exception as e:
        print(f'  yt_search_playlists error: {e}')
        return []


def _do_youtube_playlist_search(bot_token: str, chat_id: int, from_uid: int,
                                 msg_id: int, query: str,
                                 playlist_name: Optional[str], raw_message: str) -> None:
    """Search YouTube for playlists and present paginated results."""
    log_activity(f'yt_playlist_search  query={query!r}  uid={from_uid}')
    tg_send(bot_token, chat_id,
            f'🔍 Searching YouTube playlists for *{query}*…', reply_to=msg_id)
    try:
        results = yt_search_playlists(query, n=9)
    except RuntimeError as e:
        log_error(f'yt_playlist_search failed  query={query!r}  err={e}', exc=e)
        tg_send(bot_token, chat_id, f'❌ {e}', reply_to=msg_id)
        return

    if not results:
        tg_send(bot_token, chat_id,
                f'❌ No playlists found for *{query}*.', reply_to=msg_id)
        return

    log_activity(f'yt_playlist_search returned {len(results)} results  query={query!r}')
    store_pending(chat_id, from_uid, '', query, results,
                  raw_message=raw_message,
                  type='yt_playlist_pick',
                  playlist_name=playlist_name,
                  query=query,
                  yt_page=0,
                  yt_fetch_total=len(results),
                  yt_has_more=True)
    _show_yt_playlist_results_page(bot_token, chat_id, results, page=0,
                                   query=query, fetch_more_btn=True)


def _show_yt_playlist_results_page(bot_token: str, chat_id: int,
                                    results: list, page: int, query: str,
                                    fetch_more_btn: bool = False) -> None:
    """Render one page of YouTube playlist search results as an inline keyboard."""
    start      = page * _YT_PAGE_SIZE
    page_items = results[start: start + _YT_PAGE_SIZE]
    total      = len(results)
    has_more   = (start + _YT_PAGE_SIZE) < total

    lines = [f'📋 YouTube playlist results for *{query}*'
             + (f' (results {start + 1}–{start + len(page_items)}):\n' if page > 0 else ':\n')]
    for i, r in enumerate(page_items):
        abs_i = start + i + 1
        ch  = f' — _{r["channel"]}_' if r.get('channel') else ''
        dur = f' `{r["duration"]}`' if r.get('duration') else ''
        lines.append(f'  {abs_i}. {r["title"]}{ch}{dur}')

    buttons = [
        [(f'{start + i + 1}. {r["title"][:40]}', f'ytp:{start + i}')]
        for i, r in enumerate(page_items)
    ]
    if has_more:
        buttons.append([('🔍 More results', f'ytpmore:{page + 1}')])
    elif fetch_more_btn:
        buttons.append([('Show more results →', 'ytpfetch')])
    buttons.append([('❌ Cancel', 'cancel')])

    tg_send_keyboard(bot_token, chat_id, '\n'.join(lines), buttons)


def _handle_yt_playlist_pick_reply(bot_token: str, chat_id: int, from_uid: int,
                                    msg_id: int, text: str, pending: dict) -> None:
    """Handle selections from the /findplay YouTube playlist search results."""
    key     = (chat_id, from_uid)
    cleaned = text.strip().lower()
    results = pending['candidates']
    pl      = pending.get('playlist_name')
    raw     = pending.get('raw_message', '')
    query   = pending.get('query', '')

    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return

    # Page within current batch
    if cleaned.startswith('ytpmore:'):
        try:
            next_page = int(cleaned[8:])
        except ValueError:
            return
        pending['yt_page']    = next_page
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        _show_yt_playlist_results_page(bot_token, chat_id, results, next_page, query,
                                       fetch_more_btn=pending.get('yt_has_more', False))
        return

    # Fetch more results from YouTube
    if cleaned == 'ytpfetch':
        yt_fetch_total = pending.get('yt_fetch_total', len(results))
        new_total      = yt_fetch_total + 9
        try:
            all_fetched = yt_search_playlists(query, n=new_total)
        except RuntimeError as e:
            tg_send(bot_token, chat_id, f'❌ {e}', reply_to=msg_id)
            return
        new_items = all_fetched[yt_fetch_total:]
        if not new_items:
            tg_send(bot_token, chat_id,
                    '❌ No more results found.', reply_to=msg_id)
            pending['yt_has_more']  = False
            pending['expires_at']   = time.time() + PENDING_TTL
            _save_pending()
            return
        updated   = results + new_items
        has_more  = len(new_items) == 9
        new_page  = yt_fetch_total // _YT_PAGE_SIZE
        pending['candidates']     = updated
        pending['yt_fetch_total'] = len(updated)
        pending['yt_has_more']    = has_more
        pending['yt_page']        = new_page
        pending['expires_at']     = time.time() + PENDING_TTL
        _save_pending()
        _show_yt_playlist_results_page(bot_token, chat_id, updated, new_page, query,
                                       fetch_more_btn=has_more)
        return

    # User picked a result — ytp:<absolute-index>
    if cleaned.startswith('ytp:'):
        try:
            idx = int(cleaned[4:])
        except ValueError:
            return
        if not (0 <= idx < len(results)):
            return
        result = results[idx]
        del pending_matches[key]
        _save_pending()
        _fetch_and_show_playlist_tracks(bot_token, chat_id, from_uid, msg_id,
                                        result['url'], result['title'], pl, raw)
        return


def _fetch_and_show_playlist_tracks(bot_token: str, chat_id: int, from_uid: int,
                                     msg_id: int, url: str, playlist_title: str,
                                     playlist_name: Optional[str], raw_message: str) -> None:
    """Fetch a YouTube playlist's tracks and show them with an Add all button."""
    tg_send(bot_token, chat_id,
            f'🔍 Fetching tracks from *{playlist_title}*…')
    try:
        tracks = yt_get_playlist_info(url)
    except Exception as e:
        tg_send(bot_token, chat_id,
                f'❌ Could not fetch playlist: `{str(e)[:300]}`', reply_to=msg_id)
        return

    if not tracks:
        tg_send(bot_token, chat_id,
                '❌ No tracks found in that playlist.', reply_to=msg_id)
        return

    try:
        cards = fetch_cards()
    except Exception as e:
        tg_send(bot_token, chat_id,
                f'❌ Could not load Yoto library: `{e}`', reply_to=msg_id)
        return

    shown = tracks[:20]
    lines = [f'📋 *{playlist_title}* — {len(tracks)} track{"s" if len(tracks) != 1 else ""}:\n']
    lines.append('_Tap tracks to select, then tap Done. Or use Add all._')
    if len(tracks) > 20:
        lines.append(f'_(Showing first 20 of {len(tracks)} tracks)_\n')
    for i, t in enumerate(shown, 1):
        lines.append(f'  {i}. {t["title"]}')

    buttons = _ytpt_multiselect_buttons(shown, selected=[])
    kbd_mid = tg_send_keyboard_ret(bot_token, chat_id, '\n'.join(lines), buttons)
    store_pending(chat_id, from_uid, '', playlist_title, cards,
                  raw_message=raw_message,
                  type='yt_playlist_tracks',
                  playlist_name=playlist_name,
                  yt_tracks=tracks,
                  yt_shown=shown,
                  all_cards=cards,
                  selected=[],
                  keyboard_msg_id=kbd_mid)


def _ytpt_multiselect_buttons(shown: list, selected: list) -> list:
    """Build toggle buttons for the /findplay track list multi-select."""
    buttons = []
    for i, t in enumerate(shown):
        check = '✅' if i in selected else '☐'
        label = f'{check} {i + 1}. {t["title"][:45]}'
        buttons.append([(label, f'ytpt:{i}')])

    # "Add all" selects all shown tracks
    buttons.append([('➕ Add all', 'ytpt_all')])
    n = len(selected)
    if n > 0:
        buttons.append([(f'✅ Done ({n} selected)', 'done')])
    else:
        buttons.append([('Done (select tracks above)', 'done_empty')])
    buttons.append([('❌ Cancel', 'cancel')])
    return buttons


def _handle_yt_playlist_tracks_reply(bot_token: str, chat_id: int, from_uid: int,
                                      msg_id: int, text: str, pending: dict) -> None:
    """Handle track toggles, Add all, and Done from the /findplay track list."""
    key      = (chat_id, from_uid)
    cleaned  = text.strip().lower()
    tracks   = pending.get('yt_tracks', [])
    shown    = pending.get('yt_shown') or tracks[:20]
    pl       = pending.get('playlist_name')
    raw      = pending.get('raw_message', '')
    cards    = pending.get('all_cards') or pending.get('candidates', [])
    selected = pending.get('selected', [])
    kbd_mid  = pending.get('keyboard_msg_id')

    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return

    # No-op when 0 selected
    if cleaned == 'done_empty':
        return

    def _submit_tracks(chosen_tracks: list) -> None:
        """Emit job and confirm, or route through playlist picker."""
        if pl:
            card = find_card_exact(pl, cards)
            if card:
                n = len(chosen_tracks)
                _JOB_QUEUE.put({
                    'bot_token': bot_token, 'chat_id': chat_id,
                    'tracks': chosen_tracks, 'card': card, 'raw_message': raw,
                })
                tg_send(bot_token, chat_id,
                        f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                        f'_(Download & upload running in background)_')
                return
        offer_fuzzy_matches(bot_token, chat_id, from_uid, msg_id,
                            '', f'batch of {len(chosen_tracks)} tracks', pl or '', cards,
                            raw_message=raw)
        key2 = (chat_id, from_uid)
        if key2 in pending_matches:
            pending_matches[key2]['yt_tracks'] = chosen_tracks
            _save_pending()

    # "Done" — submit selected tracks
    if cleaned == 'done':
        if not selected:
            return
        chosen = [shown[i] for i in selected if 0 <= i < len(shown)]
        if not chosen:
            return
        del pending_matches[key]
        _save_pending()
        _submit_tracks(chosen)
        return

    # "Add all" — select every shown track, update keyboard to show all checked, then Done
    if cleaned == 'ytpt_all':
        del pending_matches[key]
        _save_pending()
        _submit_tracks(list(tracks))
        return

    # Toggle a track: ytpt:<shown-index>
    if cleaned.startswith('ytpt:'):
        try:
            idx = int(cleaned[5:])
        except ValueError:
            return
        if not (0 <= idx < len(shown)):
            return
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        pending['selected']   = selected
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        if kbd_mid:
            buttons = _ytpt_multiselect_buttons(shown, selected)
            tg_edit_keyboard(bot_token, chat_id, kbd_mid, buttons)
        return


_YT_PAGE_SIZE = 3
_AM_PAGE_SIZE = 5


def _do_youtube_search(bot_token: str, chat_id: int, from_uid: int,
                        msg_id: int, query: str,
                        playlist_name: Optional[str], raw_message: str) -> None:
    """Search YouTube and present results as a multi-select toggle keyboard."""
    log_activity(f'yt_search  query={query!r}  uid={from_uid}')
    try:
        results = yt_search(query, n=9)
    except RuntimeError as e:
        log_error(f'yt_search failed  query={query!r}  err={e}', exc=e)
        tg_send(bot_token, chat_id, f'❌ {e}', reply_to=msg_id)
        return

    if not results:
        log_activity(f'yt_search no results  query={query!r}')
        tg_send(bot_token, chat_id,
                f'❌ Nothing found on YouTube either for *{query}*.',
                reply_to=msg_id)
        return

    log_activity(f'yt_search returned {len(results)} results  query={query!r}')
    keyboard_msg_id = _send_yt_multiselect_keyboard(bot_token, chat_id, results,
                                                     page=0, selected=[],
                                                     query=query, fetch_more_btn=True)
    store_pending(chat_id, from_uid, '', query, results,
                  raw_message=raw_message,
                  type='youtube_multiselect',
                  playlist_name=playlist_name,
                  query=query,
                  yt_page=0,
                  yt_fetch_total=len(results),
                  yt_has_more=True,
                  selected=[],
                  keyboard_msg_id=keyboard_msg_id)


def _yt_multiselect_buttons(results: list, page: int, selected: list,
                              fetch_more_btn: bool = False) -> list:
    """Build the inline keyboard buttons for YouTube multi-select.

    Each track shows ✅ if its absolute index is in `selected`, else ☐.
    Returns a buttons list suitable for tg_send_keyboard / tg_edit_keyboard.
    """
    start      = page * _YT_PAGE_SIZE
    page_items = results[start: start + _YT_PAGE_SIZE]
    has_more   = (start + _YT_PAGE_SIZE) < len(results)

    buttons = []
    for i, r in enumerate(page_items):
        abs_i = start + i          # 0-based absolute index stored in selected
        check = '✅' if abs_i in selected else '☐'
        label = f'{check} {abs_i + 1}. {r["title"][:45]}'
        buttons.append([(label, f'yms:{abs_i}')])

    if has_more:
        buttons.append([('🔍 More results', f'ytmore:{page + 1}')])
    elif fetch_more_btn:
        buttons.append([('Show more results →', 'ytfetch')])

    n = len(selected)
    if n > 0:
        buttons.append([(f'✅ Done ({n} selected)', 'done')])
    else:
        buttons.append([('Done (select tracks above)', 'done_empty')])
    buttons.append([('❌ Cancel', 'cancel')])
    return buttons


def _send_yt_multiselect_keyboard(bot_token: str, chat_id: int,
                                   results: list, page: int,
                                   selected: list, query: str,
                                   fetch_more_btn: bool = False) -> Optional[int]:
    """Send the multi-select YouTube keyboard and return its message_id."""
    start      = page * _YT_PAGE_SIZE
    page_items = results[start: start + _YT_PAGE_SIZE]

    lines = [f'📺 YouTube results for *{query}*'
             + (f' (results {start + 1}–{start + len(page_items)}):\n' if page > 0 else ':\n')]
    lines.append('_Tap tracks to select, then tap Done._')
    for i, r in enumerate(page_items):
        abs_i = start + i + 1
        ch  = f' — _{r["channel"]}_' if r.get('channel') else ''
        dur = f' `{r["duration"]}`' if r.get('duration') else ''
        lines.append(f'  {abs_i}. {r["title"]}{ch}{dur}')

    buttons = _yt_multiselect_buttons(results, page, selected, fetch_more_btn)
    return tg_send_keyboard_ret(bot_token, chat_id, '\n'.join(lines), buttons)


# Keep a thin wrapper for legacy callers that don't need the return value
def _show_yt_results_page(bot_token: str, chat_id: int,
                           results: list, page: int, query: str,
                           fetch_more_btn: bool = False) -> None:
    """Legacy wrapper — sends the multi-select keyboard (return value discarded)."""
    _send_yt_multiselect_keyboard(bot_token, chat_id, results, page, [], query,
                                   fetch_more_btn)


# ── Reply handlers for am_confirm and youtube_pick ─────────────────────────

def _am_multiselect_buttons(tracks: list, selected: list, page: int = 0,
                             fetch_more: bool = False) -> list:
    """Build toggle buttons for Apple Music multi-select (paginated)."""
    start      = page * _AM_PAGE_SIZE
    page_items = tracks[start: start + _AM_PAGE_SIZE]
    has_more   = (start + _AM_PAGE_SIZE) < len(tracks)

    buttons = []
    for i, t in enumerate(page_items):
        abs_i = start + i
        check = '✅' if abs_i in selected else '☐'
        label = f'{check} {abs_i + 1}. {t["title"][:45]}'
        buttons.append([(label, f'ams:{abs_i}')])

    if has_more:
        next_start = start + _AM_PAGE_SIZE
        next_end   = min(next_start + _AM_PAGE_SIZE, len(tracks))
        buttons.append([(f'➡️ More ({next_start + 1}–{next_end})', f'amore:{page + 1}')])
    elif fetch_more:
        next_from = len(tracks) + 1
        next_to   = len(tracks) + 40
        buttons.append([(f'➡️ More results ({next_from}–{next_to})', 'amore:fetch')])

    n = len(selected)
    if n > 0:
        buttons.append([(f'✅ Done ({n} selected)', 'done')])
    else:
        buttons.append([('Done (select tracks above)', 'done_empty')])
    buttons.append([('🎬 Search YouTube instead', 'youtube')])
    buttons.append([('❌ Cancel', 'cancel')])
    return buttons


def _handle_am_confirm_reply(bot_token: str, chat_id: int, from_uid: int,
                              msg_id: int, text: str, pending: dict) -> None:
    key      = (chat_id, from_uid)
    cleaned  = text.strip().lower()
    tracks   = pending['candidates']
    pl       = pending.get('playlist_name')
    raw      = pending.get('raw_message', '')
    selected = pending.get('selected', [])
    kbd_mid  = pending.get('keyboard_msg_id')

    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return

    if cleaned == 'youtube':
        del pending_matches[key]
        _save_pending()
        _do_youtube_search(bot_token, chat_id, from_uid, msg_id,
                           pending.get('query', ''), pl, raw)
        return

    # No-op when 0 selected
    if cleaned == 'done_empty':
        return

    # "Done" — ask which card/playlist to add to (download deferred to job worker)
    if cleaned == 'done':
        if not selected:
            return
        chosen = [tracks[i] for i in selected if 0 <= i < len(tracks)]
        if not chosen:
            return
        del pending_matches[key]
        _save_pending()

        try:
            cards = fetch_cards()
        except Exception as e:
            tg_send(bot_token, chat_id,
                    f'❌ Could not load Yoto library: `{e}`', reply_to=msg_id)
            return

        if pl:
            card = find_card_exact(pl, cards)
            if card:
                n = len(chosen)
                _JOB_QUEUE.put({
                    'bot_token': bot_token, 'chat_id': chat_id,
                    'am_pending_tracks': chosen, 'card': card, 'raw_message': raw,
                })
                tg_send(bot_token, chat_id,
                        f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                        f'_(Download & upload running in background)_')
                return

        # No playlist pre-specified — run through picker
        offer_fuzzy_matches(bot_token, chat_id, from_uid, msg_id,
                            '', f'{len(chosen)} track(s)', pl or '', cards,
                            raw_message=raw)
        key2 = (chat_id, from_uid)
        if key2 in pending_matches:
            pending_matches[key2]['am_pending_tracks'] = chosen
            _save_pending()
        return

    # Fetch next 40 results from Apple Music
    if cleaned == 'amore:fetch':
        query      = pending.get('query', '')
        cur_offset = len(tracks)
        new_tracks = am_search(query, offset=cur_offset)
        if not new_tracks:
            tg_send(bot_token, chat_id, '🔍 No more results in your Apple Music library.')
            return
        all_tracks = tracks + new_tracks
        pending['candidates'] = all_tracks
        new_page = cur_offset // _AM_PAGE_SIZE
        pending['am_page']    = new_page
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        if kbd_mid:
            _CLOUD_DISPLAY_STATUSES = {'matched', 'purchased', 'uploaded', 'subscription'}
            def _cloud_badge(t: dict) -> str:
                return ' ☁️' if t.get('cloud_status', '') in _CLOUD_DISPLAY_STATUSES else ''
            total      = len(all_tracks)
            start      = new_page * _AM_PAGE_SIZE
            end        = min(start + _AM_PAGE_SIZE, total)
            fetch_more = total % 40 == 0
            total_str  = f'{total}+' if fetch_more else str(total)
            header = (f'🎵 Found {total_str} matches in Apple Music'
                      f' ({start + 1}–{end} of {total_str}):\n')
            lines  = [header, '_Tap tracks to select, then tap Done._']
            for i, t in enumerate(all_tracks[start:end], start + 1):
                lines.append(f'  {i}. *{t["title"]}* — {t["artist"]}  _{t["album"]}_{_cloud_badge(t)}')
            buttons = _am_multiselect_buttons(all_tracks, selected, new_page, fetch_more=fetch_more)
            tg_edit_message(bot_token, chat_id, kbd_mid, '\n'.join(lines), buttons)
        return

    # Page navigation — next page within already-fetched results
    if cleaned.startswith('amore:'):
        try:
            next_page = int(cleaned[6:])
        except ValueError:
            return
        total      = len(tracks)
        start      = next_page * _AM_PAGE_SIZE
        end        = min(start + _AM_PAGE_SIZE, total)
        fetch_more = total % 40 == 0
        total_str  = f'{total}+' if fetch_more else str(total)
        pending['am_page']    = next_page
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        if kbd_mid:
            _CLOUD_DISPLAY_STATUSES = {'matched', 'purchased', 'uploaded', 'subscription'}
            def _cloud_badge(t: dict) -> str:
                return ' ☁️' if t.get('cloud_status', '') in _CLOUD_DISPLAY_STATUSES else ''
            header = (f'🎵 Found {total_str} match{"es" if total != 1 else ""} in Apple Music'
                      f' ({start + 1}–{end} of {total_str}):\n')
            lines  = [header, '_Tap tracks to select, then tap Done._']
            for i, t in enumerate(tracks[start:end], start + 1):
                lines.append(f'  {i}. *{t["title"]}* — {t["artist"]}  _{t["album"]}_{_cloud_badge(t)}')
            buttons = _am_multiselect_buttons(tracks, selected, next_page, fetch_more=fetch_more)
            tg_edit_message(bot_token, chat_id, kbd_mid, '\n'.join(lines), buttons)
        return

    # Toggle a track: ams:<0-based-absolute-index>
    if cleaned.startswith('ams:'):
        try:
            idx = int(cleaned[4:])
        except ValueError:
            return
        if not (0 <= idx < len(tracks)):
            return
        if idx in selected:
            selected.remove(idx)
        else:
            selected.append(idx)
        pending['selected']   = selected
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        effective_mid = kbd_mid or msg_id  # msg_id from callback IS the keyboard message
        if effective_mid:
            cur_page   = pending.get('am_page', 0)
            fetch_more = len(tracks) % 40 == 0
            buttons    = _am_multiselect_buttons(tracks, selected, cur_page, fetch_more=fetch_more)
            tg_edit_keyboard(bot_token, chat_id, effective_mid, buttons)
        return

    # Catch old text-based input (typing "1", "yes", etc.) — guide the user to the buttons
    if cleaned in ('yes', 'no') or cleaned.isdigit():
        tg_send(bot_token, chat_id,
                '👆 Tap a track in the message above to select it, then tap *Done*.',
                reply_to=msg_id)
        return

    del pending_matches[key]
    _save_pending()
    track = tracks[idx]
    _CLOUD_ONLY_STATUSES = {'matched', 'purchased', 'uploaded', 'subscription'}
    cloud_status = track.get('cloud_status', '')
    if cloud_status in _CLOUD_ONLY_STATUSES:
        tg_send(bot_token, chat_id,
                '☁️ Track is in your iTunes Match library — downloading now (may take a minute)…')
    else:
        tg_send(bot_token, chat_id,
                f'⬇️ Downloading *{track["title"]}* — {track["artist"]} from iTunes Match…\n'
                f'_(May take up to 10 minutes for large files)_')

    path = am_download(track['id'], title=track.get('title', ''), artist=track.get('artist', ''))
    if not path:
        tg_send(bot_token, chat_id,
                '⚠️ Download timed out for this track. Falling back to YouTube…')
        _do_youtube_search(bot_token, chat_id, from_uid, msg_id,
                           f'{track["title"]} {track["artist"]}', pl, raw)
        return

    try:
        local_path = am_copy_to_temp(path, track['title'], track['artist'])
    except Exception as e:
        tg_send(bot_token, chat_id, f'❌ Could not copy file: `{e}`', reply_to=msg_id)
        return

    tg_send(bot_token, chat_id,
            f'✅ Got *{track["title"]}* → `{local_path}`')
    _finish_find(bot_token, chat_id, from_uid, msg_id,
                 local_path, track['title'], pl, raw)


def _handle_youtube_pick_reply(bot_token: str, chat_id: int, from_uid: int,
                                msg_id: int, text: str, pending: dict) -> None:
    """Legacy alias — delegates to the new multi-select handler."""
    _handle_youtube_multiselect_reply(bot_token, chat_id, from_uid, msg_id, text, pending)


def _handle_youtube_multiselect_reply(bot_token: str, chat_id: int, from_uid: int,
                                       msg_id: int, text: str, pending: dict) -> None:
    """Handle all interactions with the YouTube multi-select keyboard."""
    key      = (chat_id, from_uid)
    cleaned  = text.strip().lower()
    results  = pending['candidates']
    pl       = pending.get('playlist_name')
    raw      = pending.get('raw_message', '')
    query    = pending.get('query', '')
    selected = pending.get('selected', [])   # list of 0-based absolute indices
    kbd_mid  = pending.get('keyboard_msg_id')  # message_id of the keyboard message

    if cleaned == 'cancel':
        del pending_matches[key]
        _save_pending()
        tg_send(bot_token, chat_id, '👍 Cancelled.', reply_to=msg_id)
        return

    # No-op when 0 tracks are selected
    if cleaned == 'done_empty':
        return

    # "Done" — transition to playlist picker with selected tracks queued
    if cleaned == 'done':
        if not selected:
            return  # safety: should have been done_empty
        selected_results = [results[i] for i in selected if 0 <= i < len(results)]
        if not selected_results:
            return
        del pending_matches[key]
        _save_pending()
        try:
            cards = fetch_cards()
        except Exception as e:
            tg_send(bot_token, chat_id,
                    f'❌ Could not load Yoto library: `{e}`', reply_to=msg_id)
            return
        yt_tracks = [{'url': r['url'], 'title': r['title']} for r in selected_results]
        log_activity(
            f'yt_multiselect done  query={query!r}  '
            f'selected={len(yt_tracks)}  pl={pl!r}'
        )
        if pl:
            card = find_card_exact(pl, cards)
            if card:
                n = len(yt_tracks)
                _JOB_QUEUE.put({
                    'bot_token': bot_token, 'chat_id': chat_id,
                    'tracks': yt_tracks, 'card': card, 'raw_message': raw,
                })
                tg_send(bot_token, chat_id,
                        f'✅ Queued {n} track{"s" if n > 1 else ""} → *{card_title(card)}*\n'
                        f'_(Download & upload running in background)_')
                return
        # Ask for playlist (stash yt_tracks in the new pending via offer_fuzzy_matches)
        offer_fuzzy_matches(bot_token, chat_id, from_uid, msg_id,
                            '', f'{len(yt_tracks)} track(s)', pl or '', cards,
                            raw_message=raw)
        key2 = (chat_id, from_uid)
        if key2 in pending_matches:
            pending_matches[key2]['yt_tracks'] = yt_tracks
            _save_pending()
        return

    # Toggle a track: yms:<0-based-absolute-index>
    if cleaned.startswith('yms:'):
        try:
            abs_idx = int(cleaned[4:])
        except ValueError:
            return
        if not (0 <= abs_idx < len(results)):
            return
        if abs_idx in selected:
            selected.remove(abs_idx)
        else:
            selected.append(abs_idx)
        pending['selected']    = selected
        pending['expires_at']  = time.time() + PENDING_TTL
        _save_pending()
        # Edit the keyboard in-place — use msg_id as fallback if kbd_mid wasn't stored
        effective_mid = kbd_mid or msg_id
        if effective_mid:
            cur_page = pending.get('yt_page', 0)
            buttons  = _yt_multiselect_buttons(
                results, cur_page, selected,
                fetch_more_btn=pending.get('yt_has_more', False),
            )
            tg_edit_keyboard(bot_token, chat_id, effective_mid, buttons)
        return

    # Page navigation — next page within already-fetched batch
    if cleaned.startswith('ytmore:'):
        try:
            next_page = int(cleaned[7:])
        except ValueError:
            return
        pending['yt_page']    = next_page
        pending['expires_at'] = time.time() + PENDING_TTL
        _save_pending()
        # Edit keyboard to show the new page (preserving selections)
        if kbd_mid:
            buttons = _yt_multiselect_buttons(
                results, next_page, selected,
                fetch_more_btn=pending.get('yt_has_more', False),
            )
            tg_edit_keyboard(bot_token, chat_id, kbd_mid, buttons)
        return

    # "Show more results →" — fetch more from YouTube
    if cleaned == 'ytfetch':
        yt_fetch_total = pending.get('yt_fetch_total', len(results))
        new_total      = yt_fetch_total + 9
        try:
            all_fetched = yt_search(query, n=new_total)
        except RuntimeError as e:
            tg_send(bot_token, chat_id, f'❌ {e}', reply_to=msg_id)
            return
        new_items = all_fetched[yt_fetch_total:]
        if not new_items:
            tg_send(bot_token, chat_id,
                    '❌ No more YouTube results found.', reply_to=msg_id)
            pending['yt_has_more']    = False
            pending['expires_at']     = time.time() + PENDING_TTL
            _save_pending()
            return
        updated_results = results + new_items
        has_more_next   = len(new_items) == 9
        first_new_page  = yt_fetch_total // _YT_PAGE_SIZE
        pending['candidates']      = updated_results
        pending['yt_fetch_total']  = len(updated_results)
        pending['yt_has_more']     = has_more_next
        pending['yt_page']         = first_new_page
        pending['expires_at']      = time.time() + PENDING_TTL
        _save_pending()
        log_activity(
            f'yt_search fetch more  query={query!r}  '
            f'new={len(new_items)}  total={len(updated_results)}'
        )
        if kbd_mid:
            buttons = _yt_multiselect_buttons(
                updated_results, first_new_page, selected,
                fetch_more_btn=has_more_next,
            )
            tg_edit_keyboard(bot_token, chat_id, kbd_mid, buttons)
        return


def _finish_find(bot_token: str, chat_id: int, from_uid: int, msg_id: int,
                 file_path: str, track_name: str,
                 playlist_name: Optional[str], raw_message: str) -> None:
    """File is ready — proceed to playlist selection and upload."""
    try:
        cards = fetch_cards()
    except Exception as e:
        tg_send(bot_token, chat_id,
                f'❌ Could not load Yoto library: `{e}`', reply_to=msg_id)
        return

    if playlist_name:
        card = find_card_exact(playlist_name, cards)
        if card:
            do_upload(bot_token, chat_id, msg_id,
                      file_path, track_name, card, raw_message=raw_message,
                      from_uid=from_uid)
            return
    offer_fuzzy_matches(bot_token, chat_id, from_uid, msg_id,
                        file_path, track_name,
                        playlist_name or '', cards,
                        raw_message=raw_message)


# ═══════════════════════════════════════════════════════════════════════════
#  RECENT PLAYLISTS — last 3 playlists used for uploads
# ═══════════════════════════════════════════════════════════════════════════

def load_recent_playlists() -> list:
    """Return [{cardId, title}, ...] most-recent-first, max 3. Empty list on any error."""
    try:
        if RECENT_PLAYLISTS_FILE.exists():
            return json.loads(RECENT_PLAYLISTS_FILE.read_text())
    except Exception:
        pass
    return []


def record_recent_playlist(card_id: str, title: str) -> None:
    """Move card_id to the top of recent_playlists.json (or prepend), trim to 3.

    Holds _RECENT_LOCK so concurrent callers (batch worker thread + polling
    thread) can't lose updates by interleaving read/write.
    """
    if not card_id:
        return
    try:
        with _RECENT_LOCK:
            recents = [r for r in load_recent_playlists() if r.get('cardId') != card_id]
            recents.insert(0, {'cardId': card_id, 'title': title})
            RECENT_PLAYLISTS_FILE.write_text(json.dumps(recents[:3], indent=2))
    except Exception as e:
        print(f'  ⚠️  record_recent_playlist failed: {e}')


# ═══════════════════════════════════════════════════════════════════════════
#  LAST COMMAND — per-user retry support
# ═══════════════════════════════════════════════════════════════════════════

def _load_last_commands() -> dict:
    """Return {'{chat_id}:{from_uid}': '<command text>', ...}."""
    try:
        if LAST_COMMAND_FILE.exists():
            return json.loads(LAST_COMMAND_FILE.read_text())
    except Exception:
        pass
    return {}


def _save_last_command(chat_id: int, from_uid: int, text: str) -> None:
    """Persist the last top-level command for this user."""
    try:
        data = _load_last_commands()
        data[f'{chat_id}:{from_uid}'] = text
        LAST_COMMAND_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f'  ⚠️  _save_last_command failed: {e}')


def _get_last_command(chat_id: int, from_uid: int) -> Optional[str]:
    """Return the last stored command for this user, or None."""
    return _load_last_commands().get(f'{chat_id}:{from_uid}')


# ═══════════════════════════════════════════════════════════════════════════
#  QUEUE — "Save for Later" persistence
# ═══════════════════════════════════════════════════════════════════════════

def _load_queue_unlocked() -> list:
    """Read queue.json without acquiring _QUEUE_LOCK (caller must hold it)."""
    if QUEUE_FILE.exists():
        try:
            return json.loads(QUEUE_FILE.read_text())
        except Exception:
            pass
    return []


def _write_queue_atomic(data: list) -> None:
    """Write queue atomically: write to .tmp then rename, holding _QUEUE_LOCK."""
    tmp = QUEUE_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(QUEUE_FILE)


def save_to_queue(file_path: str, track_name: str,
                  raw_message: str = '',
                  reason: str = 'user_saved',
                  rejected_candidates: list = None) -> dict:
    """
    Append an item to queue.json. Returns the new item.

    reason values:
      'user_saved'     — user explicitly said 'save'
      'no_match'       — no fuzzy matches found for the playlist
      'upload_failed'  — upload pipeline raised an exception
    """
    item = {
        'id':                  str(uuid.uuid4()),
        'file_path':           file_path,
        'track_name':          track_name,
        'raw_message':         raw_message,
        'reason':              reason,
        'rejected_candidates': [card_title(c) for c in (rejected_candidates or [])],
        'added_at':            time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'status':              'pending',
    }
    with _QUEUE_LOCK:
        queue = _load_queue_unlocked()
        queue.append(item)
        _write_queue_atomic(queue)
    return item


def load_queue() -> list:
    with _QUEUE_LOCK:
        return _load_queue_unlocked()


def delete_queue_item(item_id: str) -> bool:
    with _QUEUE_LOCK:
        queue = _load_queue_unlocked()
        new_queue = [q for q in queue if q.get('id') != item_id]
        if len(new_queue) == len(queue):
            return False  # not found
        _write_queue_atomic(new_queue)
    return True


# ═══════════════════════════════════════════════════════════════════════════
#  Yoto API helpers
# ═══════════════════════════════════════════════════════════════════════════

class TokenMissingError(Exception):
    pass


def load_token() -> dict:
    """Load token from yoto_token.json. Raises TokenMissingError if not found."""
    if not TOKEN_FILE.exists():
        raise TokenMissingError('yoto_token.json not found — open the dashboard first')
    try:
        data = json.loads(TOKEN_FILE.read_text())
    except json.JSONDecodeError as e:
        raise TokenMissingError(f'yoto_token.json is corrupted ({e}) — log in again via the dashboard')
    if not data.get('access_token'):
        raise TokenMissingError('yoto_token.json has no access_token')
    return data


def refresh_yoto_token(token: dict) -> dict:
    """
    Use the stored refresh_token to get a new access_token from Auth0.
    Updates yoto_token.json in place and returns the updated token dict.
    Raises RuntimeError if refresh fails or credentials are missing.
    """
    refresh_token = token.get('refresh_token')
    client_id     = token.get('client_id')
    if not refresh_token or not client_id:
        raise RuntimeError(
            'Cannot refresh Yoto token — no refresh_token/client_id in yoto_token.json. '
            'Please log in again via the dashboard.'
        )
    body = urllib.parse.urlencode({
        'grant_type':    'refresh_token',
        'refresh_token': refresh_token,
        'client_id':     client_id,
    }).encode()
    req = urllib.request.Request(
        YOTO_AUTH + '/oauth/token',
        data=body,
        method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err_body = e.read()
        raise RuntimeError(
            f'Token refresh failed ({e.code}): {err_body[:200].decode(errors="replace")}'
        ) from e

    if not data.get('access_token'):
        raise RuntimeError(f'Token refresh returned no access_token: {str(data)[:200]}')

    token['access_token'] = data['access_token']
    if data.get('refresh_token'):
        token['refresh_token'] = data['refresh_token']

    # Persist updated token to disk
    try:
        TOKEN_FILE.write_text(json.dumps(token, indent=2))
        log_activity('token refreshed and saved OK')
    except Exception as e:
        log_error(f'token refresh: could not save updated token: {e}', exc=e)

    return token


def yoto_request(method: str, token: dict, path: str,
                  body: dict = None) -> dict:
    """
    Make an authenticated Yoto API call.
    On 401/403, automatically attempts a token refresh and retries once.
    """
    url  = YOTO_API + path
    data = json.dumps(body).encode() if body is not None else None

    def _do_request(tok: dict):
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                'Authorization': f'Bearer {tok["access_token"]}',
                'Content-Type':  'application/json',
                'Accept':        'application/json',
            },
        )
        # Timeout matters: a hung Yoto call would otherwise hold _CONTENT_LOCK
        # forever and block every other upload.
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    try:
        return _do_request(token)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403):
            err_body = e.read()
            raise RuntimeError(
                f'Yoto API {method} {path} → {e.code}: {err_body[:200].decode(errors="replace")}'
            ) from e
        # Token likely expired — try to refresh
        log_activity(f'token expired ({e.code}) on {method} {path} — refreshing…')
        try:
            refreshed = refresh_yoto_token(token)
            # Update the caller's token dict in place so future calls use new token
            token.update(refreshed)
        except RuntimeError as refresh_err:
            raise RuntimeError(
                f'Yoto API {method} {path} → {e.code} and token refresh failed: {refresh_err}'
            ) from e
        # Retry with fresh token
        try:
            return _do_request(token)
        except urllib.error.HTTPError as e2:
            err_body = e2.read()
            raise RuntimeError(
                f'Yoto API {method} {path} → {e2.code} (after token refresh): '
                f'{err_body[:200].decode(errors="replace")}'
            ) from e2


def yoto_get(token: dict, path: str) -> dict:
    return yoto_request('GET', token, path)


def yoto_post(token: dict, path: str, body: dict) -> dict:
    return yoto_request('POST', token, path, body)
