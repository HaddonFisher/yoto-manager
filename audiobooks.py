"""
Audiobook support for the Telegram bot's /audiobook command.

Three independent pieces, all stdlib-only like the rest of this project:

  • DropboxClient  — refresh-token auth (short-lived access tokens fetched on
                     demand), recursive folder listing, and chunked downloads
                     to disk. The Audiobooks folder is NOT synced locally on
                     the host, so everything goes through the HTTP API.
  • find_books / match_books — turn a recursive listing into "book folders"
                     (a folder that directly holds audio files; CD1/Disc 2
                     style subfolders fold into their parent) and fuzzy-rank
                     them against a typed title.
  • prepare_tracks — probe a downloaded file with ffprobe and cut it with
                     ffmpeg (stream copy, no re-encode) into pieces that fit
                     Yoto's per-track limits, splitting .m4b files on their
                     chapter markers.

Yoto MYO limits (support.yotoplay.com "How much audio fits on a Make Your
Own card", checked 2026-09-28): 100 MB / 60 minutes per track; 100 tracks
and 500 MB / ~5 hours per card. Pieces target a little under the per-track
limits so container overhead and bitrate variance can't push one over.
"""
from __future__ import annotations

import difflib
import json
import math
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

# ── Yoto limits ───────────────────────────────────────────────────────────
YOTO_MAX_TRACK_BYTES   = 100 * 1024 * 1024
YOTO_MAX_TRACK_SECONDS = 60 * 60
YOTO_MAX_CARD_TRACKS   = 100
YOTO_MAX_CARD_BYTES    = 500 * 1024 * 1024

# Split targets — deliberately below the hard limits.
TARGET_TRACK_BYTES   = 90 * 1024 * 1024
TARGET_TRACK_SECONDS = 55 * 60

AUDIO_EXTS = {'.mp3', '.m4a', '.m4b', '.aac', '.ogg', '.oga', '.opus', '.flac', '.wav'}
COVER_NAMES = ('cover.jpg', 'cover.jpeg', 'cover.png', 'folder.jpg', 'folder.jpeg', 'folder.png')

# "CD 1", "Disc2", "Disk 03", "Part 1" — folders that are pieces of one book.
DISC_RE = re.compile(r'^\s*(cd|disc|disk|part)\s*[-_.]?\s*\d+\s*$', re.IGNORECASE)

DEFAULT_AUDIOBOOKS_ROOT = '/Audiobooks'


class DropboxError(RuntimeError):
    pass


class DropboxNotConfigured(DropboxError):
    pass


# ═══════════════════════════════════════════════════════════════════════════
#  Dropbox client
# ═══════════════════════════════════════════════════════════════════════════

DROPBOX_TOKEN_URL   = 'https://api.dropboxapi.com/oauth2/token'
DROPBOX_API_URL     = 'https://api.dropboxapi.com/2'
DROPBOX_CONTENT_URL = 'https://content.dropboxapi.com/2'
DOWNLOAD_CHUNK      = 1024 * 1024


def dropbox_authorize_url(app_key: str) -> str:
    """URL the user opens once to approve the app. token_access_type=offline
    is what makes Dropbox hand back a long-lived refresh token."""
    return 'https://www.dropbox.com/oauth2/authorize?' + urllib.parse.urlencode({
        'client_id':         app_key,
        'response_type':     'code',
        'token_access_type': 'offline',
    })


def dropbox_exchange_code(app_key: str, app_secret: str, code: str) -> dict:
    """Swap the one-time code from the authorize page for tokens.
    Returns Dropbox's JSON (refresh_token, access_token, expires_in, …)."""
    return _post_form(DROPBOX_TOKEN_URL, {
        'grant_type':    'authorization_code',
        'code':          code.strip(),
        'client_id':     app_key,
        'client_secret': app_secret,
    })


def _post_form(url: str, fields: dict) -> dict:
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method='POST',
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode(errors='replace')
        raise DropboxError(f'Dropbox auth failed ({e.code}): {body}') from e


class DropboxClient:
    """Minimal Dropbox API client using an app key/secret + refresh token.

    Access tokens are short-lived (~4h); one is fetched on first use and
    re-fetched a minute before it expires, or immediately after a 401.
    """

    def __init__(self, app_key: str, app_secret: str, refresh_token: str):
        self.app_key       = app_key
        self.app_secret    = app_secret
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._expires_at   = 0.0
        self._lock         = threading.Lock()

    # ── auth ──────────────────────────────────────────────────────────────
    def access_token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._access_token or time.time() > self._expires_at - 60:
                data = _post_form(DROPBOX_TOKEN_URL, {
                    'grant_type':    'refresh_token',
                    'refresh_token': self.refresh_token,
                    'client_id':     self.app_key,
                    'client_secret': self.app_secret,
                })
                if not data.get('access_token'):
                    raise DropboxError(f'Dropbox returned no access_token: {str(data)[:200]}')
                self._access_token = data['access_token']
                self._expires_at   = time.time() + int(data.get('expires_in', 14400))
            return self._access_token

    def _open(self, url: str, data: bytes, headers: dict, timeout: int):
        """urlopen with bearer auth; retries once with a fresh token on 401."""
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, method='POST')
            req.add_header('Authorization', f'Bearer {self.access_token(force=attempt > 0)}')
            for k, v in headers.items():
                req.add_header(k, v)
            try:
                return urllib.request.urlopen(req, timeout=timeout)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                body = e.read()[:300].decode(errors='replace')
                raise DropboxError(f'Dropbox {url.rsplit("/2/", 1)[-1]} → {e.code}: {body}') from e
        raise DropboxError('unreachable')

    def rpc(self, endpoint: str, body: dict) -> dict:
        with self._open(f'{DROPBOX_API_URL}/{endpoint}', json.dumps(body).encode(),
                        {'Content-Type': 'application/json'}, timeout=60) as resp:
            return json.loads(resp.read() or b'{}')

    # ── listing ───────────────────────────────────────────────────────────
    def list_recursive(self, path: str) -> list:
        """Every entry under path (files and folders), following cursors."""
        path = '' if path in ('', '/') else '/' + path.strip('/')
        data = self.rpc('files/list_folder', {
            'path': path, 'recursive': True, 'limit': 2000,
            'include_non_downloadable_files': False,
        })
        entries = list(data.get('entries', []))
        while data.get('has_more'):
            data = self.rpc('files/list_folder/continue', {'cursor': data['cursor']})
            entries.extend(data.get('entries', []))
        return entries

    # ── download ──────────────────────────────────────────────────────────
    def download(self, path_or_id: str, dest: Path,
                 progress: Optional[Callable[[int, int], None]] = None) -> int:
        """Stream one file to dest in DOWNLOAD_CHUNK pieces — never holds the
        whole file in memory (audiobook .m4b files are often several hundred
        MB). Returns bytes written. Removes a partial file on failure."""
        arg = json.dumps({'path': path_or_id})
        # Dropbox-API-Arg must be ASCII; json.dumps escapes non-ASCII already.
        dest = Path(dest)
        written = 0
        try:
            with self._open(f'{DROPBOX_CONTENT_URL}/files/download', b'',
                            {'Dropbox-API-Arg': arg, 'Content-Type': 'text/plain'},
                            timeout=120) as resp, open(dest, 'wb') as out:
                total = int(resp.headers.get('Content-Length') or 0)
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(written, total)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return written


def client_from_config(cfg: dict) -> DropboxClient:
    dbx = cfg.get('dropbox') or {}
    missing = [k for k in ('app_key', 'app_secret', 'refresh_token') if not dbx.get(k)]
    if missing:
        raise DropboxNotConfigured(
            'Dropbox is not connected (missing ' + ', '.join(missing) +
            ' in bot_config.json). Run `python3 install.py` and do the Dropbox sign-in step.'
        )
    return DropboxClient(dbx['app_key'], dbx['app_secret'], dbx['refresh_token'])


def audiobooks_root(cfg: dict) -> str:
    return (cfg.get('audiobooks') or {}).get('root') or DEFAULT_AUDIOBOOKS_ROOT


# ═══════════════════════════════════════════════════════════════════════════
#  Book discovery
# ═══════════════════════════════════════════════════════════════════════════

def natural_key(s: str) -> list:
    """'Chapter 2' < 'Chapter 10'."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r'(\d+)', s)]


def find_books(entries: list, root: str) -> list:
    """Group a recursive listing into books.

    A book is a folder that directly contains audio files. Disc-style
    subfolders (CD 1, Disc 2, …) are merged into their parent so a two-disc
    book shows up once. Audio sitting directly in the root is ignored — it
    has no folder name to match on. Works the same for Root/Book/*.mp3 and
    Root/Author/Book/*.mp3.

    Each book: {name, path, parent, files: [{id, path, name, size, rel}],
                size, cover: {id, path, size} | None}
    """
    root_lower = '/' + root.strip('/').lower() if root.strip('/') else ''
    display: dict = {}          # path_lower → path_display for folders we see
    images: dict  = {}          # folder_lower → {name_lower: entry}
    audio: list   = []

    for e in entries:
        tag = e.get('.tag')
        pl, pd = e.get('path_lower', ''), e.get('path_display', '')
        if tag == 'folder':
            display[pl] = pd
            continue
        if tag != 'file':
            continue
        parent_l = str(PurePosixPath(pl).parent)
        parent_d = str(PurePosixPath(pd).parent)
        display.setdefault(parent_l, parent_d)
        name_l = PurePosixPath(pl).name
        if name_l in COVER_NAMES:
            images.setdefault(parent_l, {})[name_l] = e
        if PurePosixPath(pl).suffix in AUDIO_EXTS:
            audio.append(e)

    def book_folder(folder_l: str) -> Optional[str]:
        if folder_l == root_lower or not folder_l.startswith(root_lower + '/'):
            return None
        name = PurePosixPath(display.get(folder_l, folder_l)).name
        parent_l = str(PurePosixPath(folder_l).parent)
        if DISC_RE.match(name) and parent_l != root_lower:
            return parent_l
        return folder_l

    books: dict = {}
    for e in audio:
        pl = e['path_lower']
        folder_l = book_folder(str(PurePosixPath(pl).parent))
        if not folder_l:
            continue
        b = books.get(folder_l)
        if b is None:
            path_d   = display.get(folder_l, folder_l)
            parent_l = str(PurePosixPath(folder_l).parent)
            b = books[folder_l] = {
                'name':   PurePosixPath(path_d).name,
                'path':   path_d,
                'parent': (PurePosixPath(display.get(parent_l, parent_l)).name
                           if parent_l != root_lower else ''),
                'files':  [],
                'size':   0,
                'cover':  None,
            }
        rel = e['path_display'][len(b['path']):].lstrip('/')
        b['files'].append({
            'id':   e.get('id') or e['path_lower'],
            'path': e['path_display'],
            'name': e.get('name') or PurePosixPath(e['path_display']).name,
            'size': int(e.get('size') or 0),
            'rel':  rel,
        })
        b['size'] += int(e.get('size') or 0)

    for folder_l, b in books.items():
        b['files'].sort(key=lambda f: natural_key(f['rel']))
        found = images.get(folder_l, {})
        for cname in COVER_NAMES:
            if cname in found:
                c = found[cname]
                b['cover'] = {'id': c.get('id') or c['path_lower'],
                              'path': c['path_display'], 'size': int(c.get('size') or 0)}
                break
    return sorted(books.values(), key=lambda b: natural_key(b['path']))


_books_cache: dict = {}          # root → (fetched_at, books)
BOOKS_CACHE_TTL = 300


def list_books(client: DropboxClient, root: str, use_cache: bool = True) -> list:
    hit = _books_cache.get(root)
    if use_cache and hit and time.time() - hit[0] < BOOKS_CACHE_TTL:
        return hit[1]
    try:
        entries = client.list_recursive(root)
    except DropboxError as e:
        if 'not_found' in str(e):
            raise DropboxError(f'Audiobooks folder {root!r} was not found in Dropbox.') from e
        raise
    books = find_books(entries, root)
    _books_cache[root] = (time.time(), books)
    return books


# ═══════════════════════════════════════════════════════════════════════════
#  Title matching
# ═══════════════════════════════════════════════════════════════════════════

_ARTICLES = {'the', 'a', 'an'}


def normalize(s: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace.
    "The Hobbit: Or, There & Back Again" → "the hobbit or there and back again"."""
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = s.lower().replace('&', ' and ')
    s = re.sub(r"['’`]", '', s)                 # don't → dont, not "don t"
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return ' '.join(s.split())


def _drop_articles(tokens: list) -> list:
    return [t for t in tokens if t not in _ARTICLES] or tokens


def _token_hit(q: str, tokens: list) -> bool:
    for t in tokens:
        if q == t or (len(q) >= 3 and t.startswith(q)):
            return True
        if len(q) >= 4 and difflib.SequenceMatcher(None, q, t).ratio() >= 0.8:
            return True          # small typos: "hobit" → "hobbit"
    return False


def score_book(query: str, book: dict) -> float:
    """0..1. Exact (ignoring case/punctuation/articles) = 1.0; a query that's
    a whole-word run inside the title scores ≥0.8; otherwise a blend of how
    many query words appear (title or author/parent folder) and overall
    character similarity."""
    nq = normalize(query)
    nn = normalize(book['name'])
    if not nq or not nn:
        return 0.0
    q_tokens = _drop_articles(nq.split())
    n_tokens = _drop_articles(nn.split())
    if nq == nn or q_tokens == n_tokens:
        return 1.0

    seq = difflib.SequenceMatcher(None, ' '.join(q_tokens), ' '.join(n_tokens)).ratio()
    padded_n, padded_q = f' {nn} ', f' {nq} '
    if padded_q in padded_n:
        return max(0.8 + 0.15 * len(nq) / len(nn), seq)

    p_tokens  = normalize(book.get('parent', '')).split()
    in_title  = sum(_token_hit(t, n_tokens) for t in q_tokens)
    in_either = sum(_token_hit(t, n_tokens + p_tokens) for t in q_tokens)
    coverage  = in_either / len(q_tokens)
    score = 0.65 * coverage + 0.35 * seq
    if in_title == len(q_tokens):
        score += 0.05
    return min(score, 0.95)


MIN_SCORE    = 0.45
STRONG_SCORE = 0.9
STRONG_GAP   = 0.15


def match_books(query: str, books: list, n: int = 8) -> list:
    """Ranked [(score, book)] with score ≥ MIN_SCORE, best first."""
    scored = [(score_book(query, b), b) for b in books]
    scored = [sb for sb in scored if sb[0] >= MIN_SCORE]
    # Near-ties (a series: 'harry potter' vs every volume) fall back to
    # folder order, so Book 1 is listed before Book 2.
    scored.sort(key=lambda sb: (-round(sb[0], 2), natural_key(sb[1]['path'])))
    return scored[:n]


def classify_matches(matches: list) -> str:
    """'strong' → go straight to confirm; 'several' → offer buttons; 'none'."""
    if not matches:
        return 'none'
    top = matches[0][0]
    second = matches[1][0] if len(matches) > 1 else 0.0
    if top >= STRONG_SCORE and top - second >= STRONG_GAP:
        return 'strong'
    if len(matches) == 1 and top >= 0.6:
        return 'strong'
    return 'several'


# ═══════════════════════════════════════════════════════════════════════════
#  Splitting for Yoto's per-track limits
# ═══════════════════════════════════════════════════════════════════════════

def clean_track_title(filename: str) -> str:
    """'01 - Chapter One.mp3' → 'Chapter One'. Falls back to the stem when
    stripping leaves nothing (e.g. '01.mp3')."""
    stem = PurePosixPath(filename).stem
    cleaned = re.sub(r'^[\d\s\-_.]+', '', stem).strip()
    return cleaned or stem


def ffmpeg_available() -> bool:
    return bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def probe(path: Path) -> dict:
    """{'duration': seconds, 'chapters': [{'start', 'end', 'title'}]}"""
    r = subprocess.run(
        ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
         '-show_chapters', '-of', 'json', str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f'ffprobe failed on {path.name}: {r.stderr.strip()[:200]}')
    data = json.loads(r.stdout or '{}')
    duration = float((data.get('format') or {}).get('duration') or 0)
    chapters = []
    for c in data.get('chapters') or []:
        start, end = float(c.get('start_time', 0)), float(c.get('end_time', 0))
        if end > start:
            chapters.append({'start': start, 'end': end,
                             'title': ((c.get('tags') or {}).get('title') or '').strip()})
    return {'duration': duration, 'chapters': chapters}


def _output_ext(src: Path) -> str:
    ext = src.suffix.lower()
    # .m4b is just MP4 audio; Yoto's uploader and mimetypes both know .m4a.
    return '.m4a' if ext in ('.m4b', '.mp4') else ext


def plan_segments(size: int, duration: float, chapters: list, base_title: str,
                  is_m4b: bool) -> list:
    """Decide the cuts. Returns [(start, end, title)] with end=None meaning
    "to the end", or [] when the file can be uploaded untouched."""
    use_chapters = is_m4b and len(chapters) > 1
    if not use_chapters:
        fits = size <= TARGET_TRACK_BYTES and (not duration or duration <= TARGET_TRACK_SECONDS)
        if fits and not is_m4b:
            return []
        spans = [(0.0, duration or None, base_title)]
    else:
        spans = [(c['start'], c['end'], c['title'] or f'Chapter {i}')
                 for i, c in enumerate(chapters, 1)]

    bytes_per_sec = size / duration if duration else 0
    out = []
    for start, end, title in spans:
        length = (end - start) if end else 0
        est_bytes = length * bytes_per_sec if length else size
        parts = max(1,
                    math.ceil(length / TARGET_TRACK_SECONDS) if length else 1,
                    math.ceil(est_bytes / TARGET_TRACK_BYTES))
        if parts == 1:
            out.append((start, end, title))
            continue
        if not length:
            raise RuntimeError(f'{title!r} is over the Yoto track limit but its duration is unknown')
        step = length / parts
        for k in range(parts):
            out.append((start + k * step,
                        start + (k + 1) * step if k < parts - 1 else end,
                        f'{title} (part {k + 1} of {parts})'))
    return out


def _cut(src: Path, start: float, end: Optional[float], dest: Path) -> None:
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', f'{start:.3f}', '-i', str(src)]
    if end:
        cmd += ['-t', f'{end - start:.3f}']
    # Audio only (drops embedded cover-art video streams), stream copy (no
    # re-encode), and no chapter table in the pieces.
    cmd += ['-map', '0:a:0', '-c', 'copy', '-map_chapters', '-1', str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f'ffmpeg failed cutting {src.name}: {r.stderr.strip()[:300]}')


def prepare_tracks(src: Path, base_title: str, workdir: Path) -> list:
    """Return [(path, title)] ready for upload. Either [(src, base_title)]
    untouched, or pieces written into workdir (caller cleans up)."""
    src = Path(src)
    size = src.stat().st_size
    is_m4b = src.suffix.lower() == '.m4b'
    if not ffmpeg_available():
        if size > YOTO_MAX_TRACK_BYTES:
            raise RuntimeError(f'{src.name} is over 100 MB and ffmpeg is not installed to split it')
        return [(src, base_title)]

    info = probe(src)
    segments = plan_segments(size, info['duration'], info['chapters'], base_title, is_m4b)
    if not segments:
        return [(src, base_title)]

    workdir.mkdir(parents=True, exist_ok=True)
    ext = _output_ext(src)
    out = []
    for i, (start, end, title) in enumerate(segments, 1):
        dest = workdir / f'{src.stem[:40]}-{i:03d}{ext}'
        _cut(src, start, end, dest)
        out.append((dest, title))
    return out
