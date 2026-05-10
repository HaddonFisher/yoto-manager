#!/usr/bin/env python3
"""
Yoto Manager - Local server with API proxy.
Serves the dashboard and proxies Yoto API/auth calls to avoid CORS issues.

Routes:
  /yoto-auth/*  →  https://login.yotoplay.com/*
  /yoto-api/*   →  https://api.yotoplay.com/*
  everything else → static files in this directory
"""

import http.server
import urllib.request
import urllib.error
import urllib.parse
import os
import socket
import sys
import subprocess
import shutil
import threading
import webbrowser
import json
import re
import time
from pathlib import Path

# ── API proxy log (all requests + error bodies) ───────────────────────────
API_LOG_FILE = Path('yoto_api.log')

# Keys whose values must never appear in the log file.
_REDACT_KEYS = frozenset({'access_token', 'refresh_token', 'id_token', 'token'})
_REDACT_RE   = re.compile(
    r'("(?:' + '|'.join(_REDACT_KEYS) + r')")\s*:\s*"([^"]{8,})"'
)

def _redact_for_log(text: str) -> str:
    """Replace long token values with a placeholder before writing to the log."""
    return _REDACT_RE.sub(r'\1: "…[redacted]"', text)

def _api_log(line: str) -> None:
    try:
        with API_LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except Exception:
        pass

def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to a file atomically (temp file + rename)."""
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)

# Telegram bot — imported lazily so missing file doesn't crash the server
try:
    from telegram_bot import (
        run_telegram_bot,
        load_queue, delete_queue_item, save_to_queue,
        TOKEN_FILE as _TG_TOKEN_FILE,
        QUEUE_FILE as _TG_QUEUE_FILE,
        yt_download_mp3 as _tg_yt_download_mp3,
        transcode_to_ogg as _tg_transcode_to_ogg,
        _upload_core as _tg_upload_core,
        load_token as _tg_load_token,
    )
    TELEGRAM_BOT_AVAILABLE = True
except ImportError as _e:
    print(f'⚠️  telegram_bot.py not found: {_e} — bot features disabled')
    TELEGRAM_BOT_AVAILABLE = False
    def run_telegram_bot(cfg): pass
    def load_queue(): return []
    def delete_queue_item(i): return False
    def save_to_queue(fp, tn): return {}
    def _tg_yt_download_mp3(url, title): raise RuntimeError('telegram_bot.py not available')
    def _tg_transcode_to_ogg(path): raise RuntimeError('telegram_bot.py not available')
    def _tg_upload_core(fp, tn, card, token=None): return (False, 'telegram_bot.py not available')
    def _tg_load_token(): raise RuntimeError('telegram_bot.py not available')

PORT = 8765

TOKEN_FILE      = Path('yoto_token.json')
BOT_CONFIG_FILE = Path('bot_config.json')
QUEUE_FILE      = Path('queue.json')
TAB_ALIVE_FILE  = Path('tab_alive.json')

PROXY_ROUTES = {
    '/yoto-auth/': 'https://login.yotoplay.com/',
    '/yoto-api/':  'https://api.yotoplay.com/',
}

# Cap POST body size at 25 MB. Generous enough for icon uploads (largest payload
# that flows through the proxy) but small enough to prevent OOM from a malicious
# Content-Length value.
MAX_BODY_BYTES = 25 * 1024 * 1024


class YotoHandler(http.server.SimpleHTTPRequestHandler):

    def do_GET(self):
        if self._try_local(): return
        if self._try_proxy(): return
        super().do_GET()

    def do_POST(self):
        if self._try_local(): return
        if self._try_proxy(): return
        self.send_error(404)

    def do_PUT(self):
        if self._try_proxy(): return
        self.send_error(404)

    def do_DELETE(self):
        if self._try_local(): return
        if self._try_proxy(): return
        self.send_error(404)

    def do_PATCH(self):
        if self._try_proxy(): return
        self.send_error(404)

    def do_OPTIONS(self):
        # CORS preflight
        self.send_response(200)
        self._cors()
        self.end_headers()

    # ── local endpoints (localhost only) ───────────────────────
    def _local_auth_ok(self, *, require_origin: bool) -> bool:
        """Reject non-localhost clients and (for state-changing endpoints)
        cross-origin browser requests that would otherwise bypass CORS for
        side-effect-only POSTs (CSRF defence)."""
        if self.client_address[0] not in ('127.0.0.1', '::1'):
            self.send_error(403, 'Local endpoints are localhost-only')
            return False
        if require_origin:
            origin = self.headers.get('Origin')
            if origin is not None:
                expected = f'http://localhost:{self.server.server_address[1]}'
                # Allow either localhost or 127.0.0.1 form
                expected_alt = f'http://127.0.0.1:{self.server.server_address[1]}'
                if origin not in (expected, expected_alt):
                    self.send_error(403, 'Cross-origin request rejected')
                    return False
        return True

    def _read_body_capped(self):
        """Read the request body, refusing payloads above MAX_BODY_BYTES.
        Returns the body bytes or None (response already sent on error)."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except (ValueError, TypeError):
            length = 0
        if length < 0 or length > MAX_BODY_BYTES:
            self.send_error(413, 'Request body too large')
            return None
        return self.rfile.read(length) if length else b''

    def _try_local(self):
        """Handle /local/* endpoints. Only accepts connections from 127.0.0.1."""
        path = self.path.split('?')[0]  # strip query string

        # ── /local/token ──────────────────────────────────────
        if path == '/local/token':
            if self.command == 'GET':
                if not self._local_auth_ok(require_origin=False): return True
                if TOKEN_FILE.exists():
                    data = TOKEN_FILE.read_bytes()
                    self._send_json_response(200, data)
                else:
                    self._send_json_response(404, b'{"error":"no token stored"}')
                return True

            if self.command == 'POST':
                if not self._local_auth_ok(require_origin=True): return True
                body = self._read_body_capped()
                if body is None: return True
                try:
                    payload = json.loads(body)
                    # Validate required fields
                    if not isinstance(payload, dict) or not isinstance(payload.get('access_token'), str) or not payload['access_token']:
                        self._send_json_response(400, b'{"error":"access_token required"}')
                        return True
                    _atomic_write_text(TOKEN_FILE, json.dumps(payload, indent=2))
                    print(f'  💾 yoto_token.json updated')
                    self._send_json_response(200, b'{"ok":true}')
                except Exception as e:
                    self._send_json_response(400, json.dumps({'error': str(e)}).encode())
                return True

            if self.command == 'DELETE':
                if not self._local_auth_ok(require_origin=True): return True
                if TOKEN_FILE.exists():
                    TOKEN_FILE.unlink()
                self._send_json_response(200, b'{"ok":true}')
                return True

        # ── /local/queue ──────────────────────────────────────
        if path == '/local/queue':
            if self.command == 'GET':
                if not self._local_auth_ok(require_origin=False): return True
                queue = load_queue()
                body = json.dumps(queue).encode()
                self._send_json_response(200, body)
                return True

        # ── /local/queue/{id} ─────────────────────────────────
        m = re.match(r'^/local/queue/([^/]+)$', path)
        if m:
            item_id = m.group(1)
            if self.command == 'DELETE':
                if not self._local_auth_ok(require_origin=True): return True
                found = delete_queue_item(item_id)
                body = json.dumps({'ok': found}).encode()
                self._send_json_response(200 if found else 404, body)
                return True

        # ── /local/config ─────────────────────────────────────
        if path == '/local/config':
            if self.command == 'GET':
                if not self._local_auth_ok(require_origin=False): return True
                if not BOT_CONFIG_FILE.exists():
                    self._send_json_response(200, b'{}')
                    return True
                try:
                    cfg = json.loads(BOT_CONFIG_FILE.read_text())
                    # Mask the token: keep only the last 8 chars
                    raw_token = cfg.get('telegram_bot_token', '')
                    if raw_token and len(raw_token) > 8:
                        cfg['masked_token'] = '•' * (len(raw_token) - 8) + raw_token[-8:]
                    elif raw_token:
                        cfg['masked_token'] = raw_token
                    else:
                        cfg['masked_token'] = ''
                    # Never send the real token to the browser
                    cfg.pop('telegram_bot_token', None)
                    self._send_json_response(200, json.dumps(cfg).encode())
                except Exception as e:
                    self._send_json_response(500, json.dumps({'error': str(e)}).encode())
                return True

            if self.command == 'POST':
                if not self._local_auth_ok(require_origin=True): return True
                body = self._read_body_capped()
                if body is None: return True
                try:
                    payload = json.loads(body)
                    if not isinstance(payload, dict) or 'backup' not in payload:
                        self._send_json_response(400, b'{"error":"backup key required"}')
                        return True
                    # Read existing config (preserve token and other fields)
                    existing = {}
                    if BOT_CONFIG_FILE.exists():
                        try:
                            existing = json.loads(BOT_CONFIG_FILE.read_text())
                        except Exception:
                            pass
                    # Only allow merging the backup section
                    existing['backup'] = payload['backup']
                    _atomic_write_text(BOT_CONFIG_FILE, json.dumps(existing, indent=2))
                    print('  💾 bot_config.json backup settings updated')
                    self._send_json_response(200, b'{"ok":true}')
                except Exception as e:
                    self._send_json_response(400, json.dumps({'error': str(e)}).encode())
                return True

        # ── /local/restart ────────────────────────────────────
        if path == '/local/restart' and self.command == 'POST':
            if not self._local_auth_ok(require_origin=True): return True
            self._send_json_response(200, b'{"ok":true,"message":"Restarting..."}')
            threading.Timer(0.5, lambda: os.execv(sys.executable, [sys.executable] + sys.argv)).start()
            return True

        # ── /local/ping ───────────────────────────────────────
        if path == '/local/ping' and self.command == 'GET':
            if not self._local_auth_ok(require_origin=False): return True
            self._send_json_response(200, b'{"status":"ok"}')
            return True

        # ── /local/tab-heartbeat ──────────────────────────────
        if path == '/local/tab-heartbeat' and self.command == 'POST':
            if not self._local_auth_ok(require_origin=True): return True
            try:
                _atomic_write_text(TAB_ALIVE_FILE, json.dumps({'last_seen': time.time()}))
            except Exception:
                pass  # best-effort; reader treats missing/stale file as "no tab"
            self._send_json_response(200, b'{"ok":true}')
            return True

        # ── /local/yt-info ────────────────────────────────────
        if path == '/local/yt-info' and self.command == 'GET':
            if not self._local_auth_ok(require_origin=False): return True
            qs = urllib.parse.parse_qs(self.path.split('?', 1)[1] if '?' in self.path else '')
            yt_url = qs.get('url', [''])[0]
            if not yt_url:
                self._send_json_response(400, b'{"error":"url parameter required"}')
                return True
            if not re.search(r'(youtube\.com/watch|youtu\.be/)', yt_url):
                self._send_json_response(400, b'{"error":"not a YouTube URL"}')
                return True
            try:
                # Resolve yt-dlp binary the same way telegram_bot does
                candidates = [
                    '/opt/homebrew/bin/yt-dlp',
                    '/usr/local/bin/yt-dlp',
                    os.path.expanduser('~/.local/bin/yt-dlp'),
                    shutil.which('yt-dlp'),
                ]
                ytdlp_bin = next((c for c in candidates if c and Path(c).exists()), None)
                if ytdlp_bin is None:
                    try:
                        r = subprocess.run(['sh', '-c', 'command -v yt-dlp'],
                                           capture_output=True, text=True, timeout=5)
                        ytdlp_bin = r.stdout.strip() or None
                    except Exception:
                        pass
                cmd = [ytdlp_bin] if ytdlp_bin else [sys.executable, '-m', 'yt_dlp']
                r = subprocess.run(
                    cmd + ['--no-playlist', '--print',
                           '%(title)s|||%(duration_string)s', '--no-download', yt_url],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode != 0:
                    raise RuntimeError(r.stderr[-300:].strip() or 'yt-dlp error')
                line = r.stdout.strip()
                title_part, dur_part = (line.split('|||', 1) + [''])[:2]
                result = json.dumps({'title': title_part.strip(), 'duration': dur_part.strip()}).encode()
                self._send_json_response(200, result)
            except Exception as e:
                self._send_json_response(500, json.dumps({'error': str(e)}).encode())
            return True

        # ── /local/yt-upload ──────────────────────────────────
        if path == '/local/yt-upload' and self.command == 'POST':
            if not self._local_auth_ok(require_origin=True): return True
            body = self._read_body_capped()
            if body is None: return True
            try:
                payload = json.loads(body)
                yt_url  = payload.get('url', '').strip()
                title   = payload.get('title', '').strip()
                card_id = payload.get('card_id', '').strip()
                if not yt_url or not title or not card_id:
                    self._send_json_response(400, b'{"error":"url, title, card_id required"}')
                    return True
                if not re.search(r'(youtube\.com/watch|youtu\.be/)', yt_url):
                    self._send_json_response(400, b'{"error":"not a YouTube URL"}')
                    return True
                mp3_path = _tg_yt_download_mp3(yt_url, title)
                ogg_path = _tg_transcode_to_ogg(mp3_path)
                file_path = ogg_path if ogg_path else mp3_path
                ok, err = _tg_upload_core(file_path, title, {'cardId': card_id})
                # Clean up temp files (best-effort)
                for p in [mp3_path, ogg_path]:
                    if p:
                        try: Path(p).unlink()
                        except Exception: pass
                if ok:
                    self._send_json_response(200, b'{"ok":true}')
                else:
                    self._send_json_response(200, json.dumps({'ok': False, 'error': err}).encode())
            except Exception as e:
                self._send_json_response(200, json.dumps({'ok': False, 'error': str(e)}).encode())
            return True

        return False  # not a local route

    def _send_json_response(self, status, body: bytes):
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ── proxy ──────────────────────────────────────────────────
    def _try_proxy(self):
        for prefix, target in PROXY_ROUTES.items():
            if self.path.startswith(prefix):
                rest = self.path[len(prefix):]
                self._proxy(target + rest)
                return True
        return False

    def _proxy(self, url):
        body = self._read_body_capped()
        if body is None: return  # 413 already sent

        # Forward all headers except hop-by-hop and host-specific ones
        # Also strip accept-encoding so upstream always returns plain (not gzip) responses
        skip = {'host', 'connection', 'transfer-encoding', 'te', 'trailer',
                'upgrade', 'proxy-authorization', 'proxy-authenticate', 'keep-alive',
                'accept-encoding'}
        fwd_headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in skip}

        # Ensure a realistic User-Agent so Yoto's gateway doesn't reject us
        if 'user-agent' not in {k.lower() for k in fwd_headers}:
            fwd_headers['User-Agent'] = 'YotoApp/4.0 (iOS; com.yotoplay.Yoto)'

        _api_log(f'→ {self.command} {url}')
        print(f"\n  → {self.command} {url}")
        for k, v in fwd_headers.items():
            kl = k.lower()
            if kl == 'authorization':
                # Always redact — only print a short tail, regardless of value length
                tail = v[-8:] if len(v) > 16 else '[short]'
                print(f"      {k}: Bearer …{tail}")
            elif kl == 'cookie':
                print(f"      {k}: …[redacted]")
            else:
                print(f"      {k}: {v}")

        try:
            req = urllib.request.Request(
                url, data=body, headers=fwd_headers, method=self.command
            )
            with urllib.request.urlopen(req) as resp:
                resp_body = resp.read()
                ct = resp.headers.get('Content-Type', 'application/json')
                print(f"  ← {resp.status} {ct}")
                self._log_body(f'← {resp.status} ', resp_body)
                self._send_proxy_response(resp.status, resp_body, resp.headers)
        except urllib.error.HTTPError as e:
            err_body = e.read()
            print(f"  ← {e.code} ERROR")
            self._log_body(f'← {e.code} ERROR ', err_body)
            self._send_proxy_response(e.code, err_body, e.headers)
        except Exception as e:
            print(f"  ← 502 {e}")
            _api_log(f'← 502  {type(e).__name__}')
            self.send_error(502, str(e))

    def _log_body(self, prefix: str, body: bytes) -> None:
        """Decode, redact, then truncate body for both stdout and the log file.
        Redacting before truncation prevents tokens straddling the cut from
        leaking partially."""
        try:
            decoded = body.decode(errors='replace')
        except Exception:
            _api_log(prefix + ' (undecodable body)')
            return
        redacted  = _redact_for_log(decoded)
        truncated = redacted[:800]
        if len(redacted) > 800:
            truncated += '…'
        print(f"      {truncated}")
        _api_log(prefix + ' ' + truncated)

    def _send_proxy_response(self, status, body, upstream_headers):
        # Headers that must not be forwarded verbatim (hop-by-hop or we set them ourselves)
        skip_resp = {'connection', 'transfer-encoding', 'keep-alive', 'te',
                     'trailer', 'upgrade', 'content-encoding', 'content-length'}
        self.send_response(status)
        self._cors()
        for k, v in upstream_headers.items():
            if k.lower() not in skip_resp:
                self.send_header(k, v)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', 'http://localhost:' + str(self.server.server_address[1]))
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, PATCH, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept')

    # ── logging ────────────────────────────────────────────────
    def log_message(self, fmt, *args):
        status = args[1] if len(args) > 1 else '?'
        icon   = '✅' if str(status).startswith('2') else ('↪️ ' if str(status).startswith('3') else '⚠️ ')
        print(f"  {icon} {fmt % args}")


def find_port(start):
    port = start
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
        port += 1


def load_bot_config():
    """Load bot_config.json. Returns config dict or None if missing/invalid."""
    if not BOT_CONFIG_FILE.exists():
        return None
    try:
        cfg = json.loads(BOT_CONFIG_FILE.read_text())
        if not cfg.get('telegram_bot_token') or not cfg.get('allowed_group_id'):
            print('⚠️  bot_config.json is missing telegram_bot_token or allowed_group_id — bot disabled.')
            return None
        return cfg
    except Exception as e:
        print(f'⚠️  Could not parse bot_config.json: {e} — bot disabled.')
        return None


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    port = find_port(PORT)
    url  = f'http://localhost:{port}'

    print(f'\n🎧  Yoto Manager  →  {url}')
    print(f'    Press Ctrl+C to stop.\n')

    # Load Telegram bot config
    bot_cfg = load_bot_config()
    if bot_cfg:
        print(f'🤖  Telegram bot enabled (group {bot_cfg["allowed_group_id"]})')
        bot_thread = threading.Thread(
            target=run_telegram_bot,
            args=(bot_cfg,),
            daemon=True,
            name='telegram-bot',
        )
        bot_thread.start()
    else:
        print('ℹ️   No bot_config.json found — Telegram bot not started.')
        print('    Copy bot_config.json.example → bot_config.json to enable it.\n')

    def _tab_is_alive(threshold=75):
        try:
            data = json.loads(TAB_ALIVE_FILE.read_text())
            return (time.time() - data.get('last_seen', 0)) < threshold
        except Exception:
            return False

    if _tab_is_alive():
        print('  🔄  Existing tab detected — it will reload automatically.\n')
    else:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # ThreadingHTTPServer so a slow proxy call (e.g. icon upload) doesn't
    # block the heartbeat/ping endpoints — otherwise the dashboard's reload
    # screen would flash up during long requests.
    with http.server.ThreadingHTTPServer(('', port), YotoHandler) as httpd:
        httpd.serve_forever()
