"""
Tests for /audiobook: matching, book discovery, splitting, the Dropbox
client, the streamed Yoto upload, and the Telegram flow end to end.

Everything external is mocked (Telegram, Dropbox, Yoto). ffmpeg is used for
real when installed, to prove .m4b chapter splitting works on a real file.
All bot state files are redirected to a temp dir — the live service's
bot_pending.json etc. are never touched.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audiobooks as ab  # noqa: E402
import telegram_bot as tb  # noqa: E402

HAVE_FFMPEG = ab.ffmpeg_available()


def _book(name, parent='', path=None, files=None, size=1000, cover=None):
    return {'name': name, 'parent': parent, 'path': path or f'/Audiobooks/{name}',
            'files': files or [{'id': 'id:1', 'path': '/x/01.mp3', 'name': '01.mp3',
                                'size': size, 'rel': '01.mp3'}],
            'size': size, 'cover': cover}


# ═══════════════════════════════════════════════════════════════════════════
#  Matching
# ═══════════════════════════════════════════════════════════════════════════

class MatchingTests(unittest.TestCase):
    BOOKS = [
        _book('The Hobbit', 'J.R.R. Tolkien'),
        _book('Harry Potter and the Philosopher\'s Stone', 'J.K. Rowling'),
        _book('Harry Potter and the Chamber of Secrets', 'J.K. Rowling'),
        _book('Harry Potter and the Goblet of Fire', 'J.K. Rowling'),
        _book('Charlotte\'s Web'),
        _book('The Wind in the Willows'),
        _book('Matilda', 'Roald Dahl'),
        _book('The BFG', 'Roald Dahl'),
    ]

    def names(self, q):
        return [b['name'] for _, b in ab.match_books(q, self.BOOKS)]

    def test_normalize(self):
        self.assertEqual(ab.normalize("Charlotte's Web!"), 'charlottes web')
        self.assertEqual(ab.normalize('Rosie & Jim — Vol. 2'), 'rosie and jim vol 2')
        self.assertEqual(ab.normalize('Émile'), 'emile')

    def test_exact_ignoring_case_punctuation_and_articles(self):
        for q in ('the hobbit', 'HOBBIT', 'Hobbit.', 'charlottes web', "CHARLOTTE'S WEB"):
            m = ab.match_books(q, self.BOOKS)
            self.assertEqual(ab.classify_matches(m), 'strong', q)
        self.assertEqual(self.names('hobbit')[0], 'The Hobbit')

    def test_partial_unique_is_strong(self):
        m = ab.match_books('goblet', self.BOOKS)
        self.assertEqual(m[0][1]['name'], 'Harry Potter and the Goblet of Fire')
        self.assertEqual(ab.classify_matches(m), 'strong')

    def test_typo(self):
        m = ab.match_books('hobit', self.BOOKS)
        self.assertEqual(m[0][1]['name'], 'The Hobbit')
        self.assertEqual(ab.classify_matches(m), 'strong')

    def test_series_gives_several_ranked(self):
        m = ab.match_books('harry potter', self.BOOKS)
        self.assertEqual(ab.classify_matches(m), 'several')
        self.assertEqual(len(m), 3)
        self.assertTrue(all('Harry Potter' in b['name'] for _, b in m))

    def test_full_title_of_one_in_series_is_strong(self):
        m = ab.match_books('harry potter and the chamber of secrets', self.BOOKS)
        self.assertEqual(ab.classify_matches(m), 'strong')
        self.assertEqual(m[0][1]['name'], 'Harry Potter and the Chamber of Secrets')

    def test_author_from_parent_folder(self):
        self.assertEqual(set(self.names('roald dahl')), {'Matilda', 'The BFG'})

    def test_none(self):
        m = ab.match_books('gruffalo', self.BOOKS)
        self.assertEqual(ab.classify_matches(m), 'none')

    def test_clean_track_title(self):
        self.assertEqual(ab.clean_track_title('01 - Chapter One.mp3'), 'Chapter One')
        self.assertEqual(ab.clean_track_title('07.mp3'), '07')
        self.assertEqual(ab.clean_track_title('Prologue.m4b'), 'Prologue')


# ═══════════════════════════════════════════════════════════════════════════
#  Book discovery
# ═══════════════════════════════════════════════════════════════════════════

def _f(path, size=100, id_=None):
    return {'.tag': 'file', 'path_display': path, 'path_lower': path.lower(),
            'name': path.rsplit('/', 1)[-1], 'size': size, 'id': id_ or f'id:{path}'}


def _d(path):
    return {'.tag': 'folder', 'path_display': path, 'path_lower': path.lower(),
            'name': path.rsplit('/', 1)[-1]}


class FindBooksTests(unittest.TestCase):
    def test_layouts(self):
        entries = [
            _d('/Audiobooks'),
            _d('/Audiobooks/Matilda'),
            _f('/Audiobooks/Matilda/10 - End.mp3', 5),
            _f('/Audiobooks/Matilda/2 - Middle.mp3', 5),
            _f('/Audiobooks/Matilda/1 - Start.mp3', 5),
            _f('/Audiobooks/Matilda/Cover.JPG', 1),
            _f('/Audiobooks/Matilda/notes.txt', 1),
            _d('/Audiobooks/Tolkien'),
            _d('/Audiobooks/Tolkien/The Hobbit'),
            _f('/Audiobooks/Tolkien/The Hobbit/The Hobbit.m4b', 900),
            _d('/Audiobooks/Tolkien/The Hobbit/Extras'),
            _d('/Audiobooks/Big Book'),
            _d('/Audiobooks/Big Book/CD 2'),
            _d('/Audiobooks/Big Book/CD 1'),
            _f('/Audiobooks/Big Book/CD 2/01.mp3', 3),
            _f('/Audiobooks/Big Book/CD 1/01.mp3', 3),
            _f('/Audiobooks/Big Book/CD 1/02.mp3', 3),
            _f('/Audiobooks/stray.mp3', 3),
        ]
        books = {b['name']: b for b in ab.find_books(entries, '/Audiobooks')}
        self.assertEqual(set(books), {'Matilda', 'The Hobbit', 'Big Book'})

        m = books['Matilda']
        self.assertEqual([f['name'] for f in m['files']],
                         ['1 - Start.mp3', '2 - Middle.mp3', '10 - End.mp3'])
        self.assertEqual(m['size'], 15)
        self.assertEqual(m['parent'], '')
        self.assertEqual(m['cover']['path'], '/Audiobooks/Matilda/Cover.JPG')

        self.assertEqual(books['The Hobbit']['parent'], 'Tolkien')
        self.assertIsNone(books['The Hobbit']['cover'])

        big = books['Big Book']
        self.assertEqual([f['rel'] for f in big['files']],
                         ['CD 1/01.mp3', 'CD 1/02.mp3', 'CD 2/01.mp3'])

    def test_root_case_insensitive_and_nested_root(self):
        entries = [_f('/Media/AUDIOBOOKS/Book/a.mp3')]
        self.assertEqual(len(ab.find_books(entries, '/media/audiobooks')), 1)


# ═══════════════════════════════════════════════════════════════════════════
#  Splitting
# ═══════════════════════════════════════════════════════════════════════════

MB = 1024 * 1024


class PlanSegmentsTests(unittest.TestCase):
    def test_small_mp3_untouched(self):
        self.assertEqual(ab.plan_segments(20 * MB, 1800, [], 'T', False), [])

    def test_long_mp3_split_by_duration(self):
        segs = ab.plan_segments(80 * MB, 2.5 * 3600, [], 'Book', False)
        self.assertEqual(len(segs), 3)
        self.assertEqual(segs[0][2], 'Book (part 1 of 3)')
        self.assertLessEqual(max(e - s for s, e, _ in segs), ab.YOTO_MAX_TRACK_SECONDS)
        self.assertAlmostEqual(segs[-1][1], 2.5 * 3600)

    def test_big_file_split_by_size(self):
        segs = ab.plan_segments(250 * MB, 40 * 60, [], 'Book', False)
        self.assertEqual(len(segs), 3)

    def test_m4b_by_chapter_with_long_chapter_split(self):
        chapters = [{'start': 0, 'end': 600, 'title': 'Opening'},
                    {'start': 600, 'end': 600 + 7000, 'title': ''},
                    {'start': 7600, 'end': 8000, 'title': 'Last'}]
        segs = ab.plan_segments(100 * MB, 8000, chapters, 'Book', True)
        titles = [t for _, _, t in segs]
        self.assertEqual(titles, ['Opening', 'Chapter 2 (part 1 of 3)', 'Chapter 2 (part 2 of 3)',
                                  'Chapter 2 (part 3 of 3)', 'Last'])

    def test_m4b_without_chapters_is_remuxed(self):
        segs = ab.plan_segments(10 * MB, 600, [], 'Book', True)
        self.assertEqual(segs, [(0.0, 600, 'Book')])


def _make_audio(path: Path, seconds: int, chapters: list = None) -> None:
    """Generate a real, tiny test file with ffmpeg (optionally with chapters)."""
    cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
           '-i', f'sine=frequency=440:duration={seconds}']
    meta = None
    if chapters:
        meta = path.with_suffix('.meta')
        lines = [';FFMETADATA1']
        for s, e, t in chapters:
            lines += ['[CHAPTER]', 'TIMEBASE=1/1000', f'START={s * 1000}', f'END={e * 1000}',
                      f'title={t}']
        meta.write_text('\n'.join(lines) + '\n')
        cmd += ['-i', str(meta), '-map_metadata', '1', '-map_chapters', '1']
    if path.suffix in ('.m4b', '.m4a'):
        cmd += ['-c:a', 'aac', '-b:a', '32k', '-f', 'mp4']
    else:
        cmd += ['-c:a', 'libmp3lame', '-b:a', '32k']
    subprocess.run(cmd + [str(path)], check=True)
    if meta:
        meta.unlink()


@unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
class PrepareTracksFfmpegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_m4b_split_on_chapters(self):
        src = self.tmp / 'book.m4b'
        _make_audio(src, 9, [(0, 3, 'One'), (3, 6, 'Two'), (6, 9, 'Three')])
        out = ab.prepare_tracks(src, 'book', self.tmp / 'parts')
        self.assertEqual([t for _, t in out], ['One', 'Two', 'Three'])
        for p, _ in out:
            self.assertEqual(p.suffix, '.m4a')
            dur = ab.probe(p)['duration']
            self.assertAlmostEqual(dur, 3, delta=0.5)
            self.assertEqual(ab.probe(p)['chapters'], [])

    def test_small_mp3_passthrough(self):
        src = self.tmp / 'a.mp3'
        _make_audio(src, 2)
        self.assertEqual(ab.prepare_tracks(src, 'A', self.tmp / 'parts'), [(src, 'A')])

    def test_mp3_over_duration_limit_split(self):
        src = self.tmp / 'long.mp3'
        _make_audio(src, 10)
        with mock.patch.object(ab, 'TARGET_TRACK_SECONDS', 4):
            out = ab.prepare_tracks(src, 'Long', self.tmp / 'parts')
        self.assertEqual([t for _, t in out],
                         ['Long (part 1 of 3)', 'Long (part 2 of 3)', 'Long (part 3 of 3)'])
        total = sum(ab.probe(p)['duration'] for p, _ in out)
        self.assertAlmostEqual(total, 10, delta=1)


# ═══════════════════════════════════════════════════════════════════════════
#  Dropbox client (urlopen mocked)
# ═══════════════════════════════════════════════════════════════════════════

class _Resp(io.BytesIO):
    def __init__(self, body: bytes, headers=None):
        super().__init__(body)
        self.headers = headers or {}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class DropboxClientTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _fake(self, responses):
        def urlopen(req, timeout=None):
            self.calls.append(req)
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return urlopen

    def test_refresh_list_pagination_and_401_retry(self):
        tok = lambda t: _Resp(json.dumps({'access_token': t, 'expires_in': 14400}).encode())
        unauthorized = urllib.error.HTTPError('u', 401, 'x', {}, io.BytesIO(b'expired'))
        responses = [
            tok('A'),
            _Resp(json.dumps({'entries': [_f('/Audiobooks/B/1.mp3')], 'has_more': True,
                              'cursor': 'c1'}).encode()),
            unauthorized,
            tok('B'),
            _Resp(json.dumps({'entries': [_f('/Audiobooks/B/2.mp3')], 'has_more': False}).encode()),
        ]
        c = ab.DropboxClient('key', 'secret', 'refresh')
        with mock.patch('urllib.request.urlopen', self._fake(responses)):
            entries = c.list_recursive('/Audiobooks/')
        self.assertEqual(len(entries), 2)
        form = self.calls[0].data.decode()
        self.assertIn('grant_type=refresh_token', form)
        self.assertIn('refresh_token=refresh', form)
        self.assertEqual(json.loads(self.calls[1].data)['path'], '/Audiobooks')
        self.assertTrue(json.loads(self.calls[1].data)['recursive'])
        self.assertEqual(self.calls[1].get_header('Authorization'), 'Bearer A')
        self.assertEqual(json.loads(self.calls[4].data)['cursor'], 'c1')
        self.assertEqual(self.calls[4].get_header('Authorization'), 'Bearer B')

    def test_download_streams_in_chunks(self):
        body = os.urandom(int(2.5 * ab.DOWNLOAD_CHUNK))
        resp = _Resp(body, {'Content-Length': str(len(body))})
        reads = []
        orig_read = resp.read
        resp.read = lambda n=-1: (reads.append(n), orig_read(n))[1]
        c = ab.DropboxClient('k', 's', 'r')
        c._access_token, c._expires_at = 'T', 9e12
        seen = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch('urllib.request.urlopen', self._fake([resp])):
            dest = Path(d) / 'x.mp3'
            n = c.download('id:abc', dest, lambda done, total: seen.append((done, total)))
            self.assertEqual(dest.read_bytes(), body)
        self.assertEqual(n, len(body))
        self.assertTrue(all(r == ab.DOWNLOAD_CHUNK for r in reads))
        self.assertEqual(seen[-1], (len(body), len(body)))
        self.assertEqual(json.loads(self.calls[0].get_header('Dropbox-api-arg')), {'path': 'id:abc'})

    def test_failed_download_removes_partial_file(self):
        class Boom(_Resp):
            def read(self, n=-1):
                raise ConnectionResetError('drop')
        c = ab.DropboxClient('k', 's', 'r')
        c._access_token, c._expires_at = 'T', 9e12
        with tempfile.TemporaryDirectory() as d, \
                mock.patch('urllib.request.urlopen', self._fake([Boom(b'')])):
            dest = Path(d) / 'x.mp3'
            with self.assertRaises(ConnectionResetError):
                c.download('id:1', dest)
            self.assertFalse(dest.exists())

    def test_not_configured(self):
        with self.assertRaises(ab.DropboxNotConfigured):
            ab.client_from_config({'dropbox': {'app_key': 'k'}})

    def test_authorize_url_requests_offline_access(self):
        self.assertIn('token_access_type=offline', ab.dropbox_authorize_url('KEY'))


# ═══════════════════════════════════════════════════════════════════════════
#  Yoto upload streams from disk
# ═══════════════════════════════════════════════════════════════════════════

class UploadMediaTests(unittest.TestCase):
    def test_put_streams_file_object_with_length(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / 'Chapter 1.m4a'
            f.write_bytes(b'x' * 12345)
            gets = [
                {'upload': {'uploadId': 'U1', 'uploadUrl': 'https://s3/put'}},
                {'progress': {}},
                {'transcode': {'transcodedSha256': 'abc123', 'transcodedInfo': {'duration': 60}}},
            ]
            puts = []

            def urlopen(req, timeout=None):
                puts.append((req, hasattr(req.data, 'read'), req.get_header('Content-length'),
                             req.get_header('Content-type')))
                return _Resp(b'')

            with mock.patch.object(tb, 'yoto_get', side_effect=lambda t, p: gets.pop(0)), \
                    mock.patch.object(tb.time, 'sleep'), \
                    mock.patch('urllib.request.urlopen', urlopen):
                media = tb._upload_media(str(f), {'access_token': 'x'})
        self.assertEqual(media, {'trackUrl': 'yoto:#abc123', 'info': {'duration': 60}})
        req, is_stream, length, ctype = puts[0]
        self.assertTrue(is_stream, 'S3 PUT body should be a file object, not bytes')
        self.assertEqual(length, '12345')
        self.assertEqual(ctype, 'audio/mp4')

    def test_append_chapters_single_post_keeps_existing_and_sets_cover(self):
        existing = {'card': {'title': 'Book', 'metadata': {'title': 'Book'},
                             'content': {'chapters': [{'key': '00', 'title': 'Old', 'tracks': []}]}}}
        posted = []
        with mock.patch.object(tb, 'yoto_get', return_value=existing), \
                mock.patch.object(tb, 'yoto_post', side_effect=lambda t, p, b: posted.append(b)):
            tb._append_chapters({}, 'C1', [('One', {'trackUrl': 'yoto:#1', 'info': {'duration': 5}}),
                                           ('Two', {'trackUrl': 'yoto:#2', 'info': {}})],
                                cover_url='https://img')
        self.assertEqual(len(posted), 1)
        ch = posted[0]['content']['chapters']
        self.assertEqual([c['title'] for c in ch], ['Old', 'One', 'Two'])
        self.assertEqual([c['key'] for c in ch[1:]], ['01', '02'])
        self.assertEqual(ch[1]['tracks'][0]['duration'], 5)
        self.assertEqual(posted[0]['metadata']['cover'], {'imageL': 'https://img'})

    def test_append_chapters_does_not_replace_existing_cover(self):
        existing = {'card': {'metadata': {'title': 'B', 'cover': {'imageL': 'https://mine'}},
                             'content': {'chapters': []}}}
        posted = []
        with mock.patch.object(tb, 'yoto_get', return_value=existing), \
                mock.patch.object(tb, 'yoto_post', side_effect=lambda t, p, b: posted.append(b)):
            tb._append_chapters({}, 'C1', [('One', {'trackUrl': 'yoto:#1'})], cover_url='https://new')
        self.assertEqual(posted[0]['metadata']['cover']['imageL'], 'https://mine')


# ═══════════════════════════════════════════════════════════════════════════
#  Telegram flow (all network mocked, state files in a temp dir)
# ═══════════════════════════════════════════════════════════════════════════

class _StopPolling(BaseException):
    """Breaks run_telegram_bot's `while True` (it only catches Exception)."""


CHAT = 111
UID  = 111
BOOKS = [
    _book('The Hobbit', 'Tolkien', files=[
        {'id': 'id:h1', 'path': '/Audiobooks/Tolkien/The Hobbit/The Hobbit.m4b',
         'name': 'The Hobbit.m4b', 'size': 400 * MB, 'rel': 'The Hobbit.m4b'}],
        size=400 * MB, cover={'id': 'id:cov', 'path': '/Audiobooks/Tolkien/The Hobbit/cover.jpg',
                              'size': 10}),
    _book('Harry Potter and the Chamber of Secrets', 'Rowling'),
    _book('Harry Potter and the Goblet of Fire', 'Rowling'),
]


class TelegramFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        (self.tmp / 'bot_config.json').write_text(json.dumps({
            'telegram_bot_token': 'T', 'owner_chat_id': CHAT, 'allowed_user_ids': [UID],
            'dropbox': {'app_key': 'k', 'app_secret': 's', 'refresh_token': 'r'},
            'audiobooks': {'root': '/Audiobooks'},
        }))
        self.patches = [
            mock.patch.object(tb, name, self.tmp / fname) for name, fname in [
                ('PENDING_FILE', 'bot_pending.json'), ('OFFSET_FILE', 'bot_offset.json'),
                ('RECENT_PLAYLISTS_FILE', 'recent_playlists.json'),
                ('LAST_COMMAND_FILE', 'bot_last_command.json'),
                ('RESTART_ACK_FILE', 'bot_restart_ack.json'),
                ('ACTIVITY_LOG_FILE', 'activity.log'), ('ERROR_LOG_FILE', 'errors.log'),
                ('TOKEN_FILE', 'yoto_token.json'),
            ]
        ] + [
            mock.patch.object(tb, '_allowed_send_ids', set()),
            mock.patch.object(tb, '_ensure_job_worker', lambda: None),
            mock.patch.object(tb, '_activity_logger', None),
            mock.patch.object(tb, '_error_logger', None),
            mock.patch.object(ab, 'list_books', lambda client, root, use_cache=True: BOOKS),
            mock.patch.object(tb, 'fetch_cards', lambda: [{'cardId': 'EX', 'title': 'The Hobbit'}]),
        ]
        for p in self.patches:
            p.start()
        tb.pending_matches.clear()
        while not tb._JOB_QUEUE.empty():
            tb._JOB_QUEUE.get_nowait()
            tb._JOB_QUEUE.task_done()
        self.sent = []
        self.next_msg_id = 1000

    def tearDown(self):
        for p in self.patches:
            p.stop()
        tb.pending_matches.clear()
        while not tb._JOB_QUEUE.empty():
            tb._JOB_QUEUE.get_nowait()
            tb._JOB_QUEUE.task_done()
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── helpers ───────────────────────────────────────────────────────────
    def _tg(self, updates):
        """Fake tg_request: serves `updates` once to getUpdates, then stops."""
        served = {'done': False}

        def tg_request(token, method, payload=None, timeout=35):
            if method == 'getUpdates':
                if served['done']:
                    raise _StopPolling()
                served['done'] = True
                return {'result': updates}
            self.sent.append((method, payload))
            self.next_msg_id += 1
            return {'ok': True, 'result': {'message_id': self.next_msg_id}}
        return tg_request

    def run_updates(self, *updates):
        """Push messages ('text') or button taps (('tap', data)) through the
        real polling loop / router."""
        upd = []
        for i, u in enumerate(updates):
            if isinstance(u, tuple):
                upd.append({'update_id': i, 'callback_query': {
                    'id': f'cq{i}', 'from': {'id': UID}, 'data': u[1],
                    'message': {'message_id': 50 + i, 'chat': {'id': CHAT}}}})
            else:
                upd.append({'update_id': i, 'message': {
                    'message_id': 50 + i, 'chat': {'id': CHAT}, 'from': {'id': UID}, 'text': u}})
        self.sent.clear()
        with mock.patch.object(tb, 'tg_request', self._tg(upd)):
            with self.assertRaises(_StopPolling):
                tb.run_telegram_bot({'telegram_bot_token': 'T', 'owner_chat_id': CHAT,
                                     'allowed_user_ids': [UID]})
        return [p for m, p in self.sent if m in ('sendMessage', 'editMessageText')]

    def pending(self):
        return tb.pending_matches.get((CHAT, UID))

    @staticmethod
    def buttons(msg):
        return [b['callback_data'] for row in msg['reply_markup']['inline_keyboard'] for b in row]

    # ── tests ─────────────────────────────────────────────────────────────
    def test_ask_then_answer_strong_match_then_queue(self):
        out = self.run_updates('/audiobook')
        self.assertEqual(out[-1]['text'], '📚 Which book?')
        self.assertTrue(out[-1]['reply_markup']['force_reply'])
        self.assertEqual(self.pending()['type'], 'audiobook_ask')

        out = self.run_updates('the hobit')
        confirm = out[-1]
        self.assertIn('The Hobbit', confirm['text'])
        self.assertIn('1 file, 400 MB', confirm['text'])
        self.assertIn('chapter markers', confirm['text'])
        self.assertIn('cover.jpg', confirm['text'])
        self.assertIn('already have a card', confirm['text'])
        self.assertEqual(self.buttons(confirm), ['ab_add', 'ab_new', 'cancel'])

        out = self.run_updates(('tap', 'ab_add'))
        self.assertIn('Queued', out[-1]['text'])
        self.assertIsNone(self.pending())
        job = tb._JOB_QUEUE.get_nowait()
        tb._JOB_QUEUE.task_done()
        self.assertEqual(job['audiobook']['name'], 'The Hobbit')
        self.assertEqual(job['card'], {'cardId': 'EX', 'title': 'The Hobbit'})

    def test_inline_title_several_matches_pick_by_button(self):
        out = self.run_updates('/audiobook harry potter')
        msg = out[-1]
        self.assertIn('2 books match', msg['text'])
        self.assertEqual(self.buttons(msg), ['ab:0', 'ab:1', 'ab_again', 'cancel'])
        for data in self.buttons(msg):
            self.assertLessEqual(len(data.encode()), 64)

        out = self.run_updates(('tap', 'ab:1'))
        self.assertIn('Goblet of Fire', out[-1]['text'])
        self.assertIn('create a new card', out[-1]['text'])
        self.assertEqual(self.buttons(out[-1]), ['ab_new', 'cancel'])

        self.run_updates(('tap', 'ab_new'))
        job = tb._JOB_QUEUE.get_nowait()
        tb._JOB_QUEUE.task_done()
        self.assertIsNone(job['card'])
        self.assertEqual(job['create_name'], 'Harry Potter and the Goblet of Fire')

    def test_no_match_lets_him_retry(self):
        out = self.run_updates('/audiobook gruffalo')
        self.assertIn('No audiobook folder matches', out[-1]['text'])
        self.assertEqual(self.pending()['type'], 'audiobook_ask')
        out = self.run_updates('goblet')
        self.assertIn('Goblet of Fire', out[-1]['text'])
        self.assertEqual(self.pending()['type'], 'audiobook_confirm')

    def test_answer_that_looks_like_a_command_word_is_a_title(self):
        self.run_updates('/audiobook')
        with mock.patch.object(tb, 'handle_find_command') as find:
            out = self.run_updates('Finding the Goblet')
        find.assert_not_called()
        self.assertIn('Closest match', out[-1]['text'])
        self.assertIn('Goblet of Fire', json.dumps(out[-1]['reply_markup']))

    def test_cancel(self):
        self.run_updates('/audiobook')
        out = self.run_updates('/cancel')
        self.assertEqual(out[-1]['text'], '👍 Cancelled.')
        self.assertIsNone(self.pending())

    def test_pending_survives_restart_file(self):
        self.run_updates('/audiobook harry potter')
        saved = json.loads((self.tmp / 'bot_pending.json').read_text())
        self.assertEqual(saved[f'{CHAT}:{UID}']['type'], 'audiobook_pick')

    def test_dropbox_not_configured_message(self):
        (self.tmp / 'bot_config.json').write_text(json.dumps({'telegram_bot_token': 'T'}))
        out = self.run_updates('/audiobook hobbit')
        self.assertIn('install.py', out[-1]['text'])
        self.assertIsNone(self.pending())

    # ── background job ────────────────────────────────────────────────────
    def _run_job(self, job, files_on_disk, upload_side_effect=None, existing_chapters=0):
        (self.tmp / 'yoto_token.json').write_text(json.dumps({'access_token': 'x'}))
        tmpdirs = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(prefix='', dir=None):
            self.assertEqual(dir, '/tmp')
            d = real_mkdtemp(prefix=prefix, dir=str(self.tmp))
            tmpdirs.append(Path(d))
            return d

        class FakeClient:
            def download(self_inner, id_, dest, progress=None):
                shutil.copy(files_on_disk[id_], dest)
                if progress:
                    progress(dest.stat().st_size, dest.stat().st_size)
                return dest.stat().st_size

        uploads, appended = [], []

        def upload_media(path, token, max_polls=600):
            self.assertTrue(Path(path).exists())
            self.assertTrue(str(path).startswith(str(tmpdirs[0])))
            uploads.append(Path(path).name)
            if upload_side_effect:
                upload_side_effect(len(uploads))
            return {'trackUrl': f'yoto:#{len(uploads)}', 'info': {}}

        with mock.patch.object(tb.tempfile, 'mkdtemp', mkdtemp), \
                mock.patch.object(ab, 'client_from_config', lambda cfg: FakeClient()), \
                mock.patch.object(tb, '_upload_media', upload_media), \
                mock.patch.object(tb, '_upload_cover_image', return_value='https://cover'), \
                mock.patch.object(tb, 'create_playlist', return_value={'cardId': 'NEW'}), \
                mock.patch.object(tb, '_append_chapters',
                                  side_effect=lambda t, cid, items, cover_url='':
                                  appended.append((cid, items, cover_url)) or []), \
                mock.patch.object(tb, 'yoto_get', return_value={'card': {'content': {
                    'chapters': [{}] * existing_chapters}}}), \
                mock.patch.object(tb.time, 'sleep'), \
                mock.patch.object(tb, 'tg_request', self._tg([])):
            tb._process_job(job)
        return tmpdirs, uploads, appended

    @unittest.skipUnless(HAVE_FFMPEG, 'ffmpeg/ffprobe not installed')
    def test_job_downloads_splits_uploads_saves_once_and_cleans_up(self):
        src = self.tmp / 'src'
        src.mkdir()
        _make_audio(src / 'book.m4b', 6, [(0, 2, 'One'), (2, 4, 'Two'), (4, 6, 'Three')])
        _make_audio(src / 'epilogue.mp3', 1)
        (src / 'cover.jpg').write_bytes(b'\xff\xd8jpeg')
        book = _book('Test Book', files=[
            {'id': 'id:a', 'path': '/A/Test Book/01 - book.m4b', 'name': '01 - book.m4b',
             'size': 1, 'rel': '01 - book.m4b'},
            {'id': 'id:b', 'path': '/A/Test Book/02 - Epilogue.mp3', 'name': '02 - Epilogue.mp3',
             'size': 1, 'rel': '02 - Epilogue.mp3'}],
            cover={'id': 'id:c', 'path': '/A/Test Book/cover.jpg', 'size': 5})
        files = {'id:a': src / 'book.m4b', 'id:b': src / 'epilogue.mp3', 'id:c': src / 'cover.jpg'}

        tmpdirs, uploads, appended = self._run_job(
            {'bot_token': 'T', 'chat_id': CHAT, 'audiobook': book, 'card': None,
             'create_name': 'Test Book'}, files)

        self.assertEqual(len(uploads), 4)
        self.assertEqual(len(appended), 1, 'all chapters saved in one card update')
        card_id, items, cover = appended[0]
        self.assertEqual(card_id, 'NEW')
        self.assertEqual([t for t, _ in items], ['One', 'Two', 'Three', 'Epilogue'])
        self.assertEqual(cover, 'https://cover')
        self.assertFalse(tmpdirs[0].exists(), 'temp dir must be removed')
        texts = [p['text'] for m, p in self.sent if m in ('sendMessage', 'editMessageText')]
        self.assertTrue(any(t.startswith('📚 *Test Book*\n⏫') for t in texts), texts)
        self.assertIn('4 tracks added to a new card with cover art', texts[-1])

    def test_job_failure_midway_still_saves_uploaded_and_cleans_up(self):
        src = self.tmp / 'src'
        src.mkdir()
        for n in ('a', 'b', 'c'):
            (src / f'{n}.mp3').write_bytes(b'ID3' + b'\0' * 100)
        book = _book('Partial', files=[
            {'id': f'id:{n}', 'path': f'/A/Partial/{n}.mp3', 'name': f'{n}.mp3', 'size': 103,
             'rel': f'{n}.mp3'} for n in 'abc'])
        files = {f'id:{n}': src / f'{n}.mp3' for n in 'abc'}

        def fail_second(count):
            if count in (2, 3, 4):   # 2nd file fails all 3 attempts
                raise RuntimeError('S3 exploded')

        with mock.patch.object(ab, 'ffmpeg_available', lambda: False):
            tmpdirs, uploads, appended = self._run_job(
                {'bot_token': 'T', 'chat_id': CHAT, 'audiobook': book,
                 'card': {'cardId': 'EX', 'title': 'Partial'}, 'create_name': 'Partial'},
                files, upload_side_effect=fail_second)

        self.assertEqual(appended[0][0], 'EX')
        self.assertEqual([t for t, _ in appended[0][1]], ['a', 'c'])
        self.assertFalse(tmpdirs[0].exists())
        summary = [p['text'] for m, p in self.sent if m == 'sendMessage'][-1]
        self.assertIn('2 tracks added to the existing card', summary)
        self.assertIn('1 failed', summary)
        self.assertIn('S3 exploded', summary)

    def test_job_stops_when_existing_card_is_full(self):
        src = self.tmp / 'src'
        src.mkdir()
        for n in 'ab':
            (src / f'{n}.mp3').write_bytes(b'ID3' + b'\0' * 100)
        book = _book('Full', files=[
            {'id': f'id:{n}', 'path': f'/A/Full/{n}.mp3', 'name': f'{n}.mp3', 'size': 103,
             'rel': f'{n}.mp3'} for n in 'ab'])
        with mock.patch.object(ab, 'ffmpeg_available', lambda: False):
            tmpdirs, uploads, appended = self._run_job(
                {'bot_token': 'T', 'chat_id': CHAT, 'audiobook': book,
                 'card': {'cardId': 'EX', 'title': 'Full'}, 'create_name': 'Full'},
                {f'id:{n}': src / f'{n}.mp3' for n in 'ab'}, existing_chapters=99)
        self.assertEqual(uploads, ['001.mp3'])
        self.assertEqual([t for t, _ in appended[0][1]], ['a'])
        summary = [p['text'] for m, p in self.sent if m == 'sendMessage'][-1]
        self.assertIn('card is full', summary)
        self.assertFalse(tmpdirs[0].exists())

    def test_job_download_error_cleans_up(self):
        book = _book('Gone', files=[{'id': 'id:x', 'path': '/A/Gone/x.mp3', 'name': 'x.mp3',
                                     'size': 1, 'rel': 'x.mp3'}])
        tmpdirs, uploads, appended = self._run_job(
            {'bot_token': 'T', 'chat_id': CHAT, 'audiobook': book, 'card': None,
             'create_name': 'Gone'}, {})   # KeyError inside download
        self.assertEqual(uploads, [])
        self.assertEqual(appended, [])
        self.assertFalse(tmpdirs[0].exists())
        summary = [p['text'] for m, p in self.sent if m == 'sendMessage'][-1]
        self.assertIn('nothing was uploaded', summary)


if __name__ == '__main__':
    unittest.main()
