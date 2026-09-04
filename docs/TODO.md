# Yoto Manager: Performance & Robustness Backlog

No existing to-do file was found in this repo (checked for `docs/`, README mentions, sibling
repos `rcc`/`musicdealer`). Created following the `docs/*-TODO.md` convention used in `fub`
(`docs/DEPLOYMENT-TODO.md`), adapted for a running-service optimization/robustness backlog rather
than a pre-launch deployment checklist.

Every item below is grounded in something actually measured or read in the code during the
2026-09-03/04 search-speed investigation and the real end-to-end tests run against FUBARS —
not generic advice. Ordered by value; the last section is things considered and *not* recommended.

## Status at time of writing

Search (`/find`) was fixed to ~14s (from ~40s) via `player_client=android`. That leaves two other
per-track costs as the dominant remaining latency, both largely **outside this codebase's
control**: download (~5-10s, network + yt-dlp bound) and Yoto's own server-side transcode wait
(~15-20s, observed as 15 poll iterations at ~1s each in two separate real uploads). Neither has an
obvious lever to pull directly — the real lever on total wall-clock time when a user is dealing
with more than one track is item 1.

## 1. Parallelize the `/find` multi-track path (High value / Medium effort / Low risk)

**What:** `_process_job()` — the job the standard `/find` → multiselect → "Done" flow queues —
processes its `tracks` list with a plain `for item in yt_tracks:` loop: download, transcode,
upload, fully sequential, one track at a time, on the single global job-worker thread
(`_ensure_job_worker` starts exactly one).

**The catch:** this codebase already has a parallel version of the same work. `_do_yt_batch_upload()`
(used by `/findplay` playlist imports) explicitly parallelizes the download phase across up to
`MAX_WORKERS = 3` threads via `ThreadPoolExecutor`, then drives uploads sequentially in the main
thread via `as_completed()` so each upload starts as soon as its download finishes — see its own
docstring at line ~1849. `_process_job` doesn't use this pattern at all.

**Benefit:** for N tracks selected in one `/find` session, wall-clock time is currently N ×
(download+transcode+upload); with the existing pattern applied, it'd be closer to
max(downloads in parallel) + N × upload, i.e. real savings scale with how many tracks a family
member selects at once — which the UI explicitly supports (multiselect keyboard).

**Effort:** the pattern to copy already exists and is proven in production (`_do_yt_batch_upload`);
this is porting it to `_process_job`, not designing it from scratch.

**Risk:** low — `_upload_core` is already documented as safe for concurrent callers
(`_CONTENT_LOCK` serializes the chapter-list read-modify-write specifically for this reason).

## 2. Audit remaining yt-dlp calls for the same fix (Medium value / Low effort / Low risk)

`yt_get_playlist_info()` (the path taken when a user pastes a YouTube *playlist* URL directly,
not `/findplay`'s search) still uses the default `player_client`, no `--extractor-args`, and a
flat 60s timeout that was never measured — the exact pre-fix shape `yt_search()` had. It's not
confirmed slow (not measured this session), but it's the same code pattern that silently returned
"nothing found" for `/find` before the fix, so it's worth the same treatment rather than waiting
for a report.

## 3. Verify (don't assume) the local pre-transcode step is actually saving time (Medium value if true, wasted local CPU if false / Low effort to test / Low risk)

**What:** `transcode_to_ogg()` re-encodes the downloaded mp3 to ogg/opus locally, matching Yoto's
exact target loudnorm/codec settings, specifically so — per its own docstring — "server-side
transcoding completes almost instantly."

**What I actually observed:** two real uploads during this session's testing both took **exactly
15 server-side poll iterations** to finish transcoding — one where the uploaded file was the
pre-transcoded ogg (via the real `_process_job` path), one where it was the raw untranscoded mp3
(a direct `_upload_core` call that bypassed `transcode_to_ogg` entirely). If the local step were
delivering its stated benefit, the pre-transcoded upload should have finished server-side
transcoding meaningfully faster than the raw one. It didn't, in the two cases observed.

**Why this is only a "verify" item, not a "remove" item:** n=2, not a controlled A/B — could be
coincidence, could depend on track length/format in ways these two examples didn't exercise. But
if it holds up under a real test (upload the *same* track both ways, compare poll counts), cutting
this step removes a full local ffmpeg two-pass loudnorm encode from every track's critical path for
zero benefit.

## 4. Fix `_load_pending()`'s silent failure mode (High value / Low effort / Low risk)

```python
def _load_pending() -> dict:
    ...
    except Exception:
        return {}
```

Any corruption of `bot_pending.json`, or a concurrent write from a second process, is swallowed
completely — no log line, no console output, nothing. Every user's in-flight `/find` selection
state is silently gone. This isn't hypothetical: **there is no file locking on `bot_pending.json`
at all** — confirmed while reading the code, and the reason a real Telegram end-to-end test earlier
this session had to stop the live systemd service first rather than run alongside it. Contrast with
`_save_pending()`, which at least `print()`s a warning on failure (not routed through `log_error`,
but not silent either) — `_load_pending` is the more severe of the two.

## 5. Audit the other bare `except Exception:` blocks (Medium value / Medium effort / Low risk)

Counted **10** bare `except Exception:` handlers (no captured exception variable) in
`telegram_bot.py`. Only `_load_pending()` (item 4) was characterized in depth this session — the
other 9 are unreviewed. This is the same failure class as the Dropbox backup path that silently
failed every single write for months (fixed 2026-09-03) before anyone noticed — worth a deliberate
pass rather than waiting for the next one to surface by accident.

## 6. Give soft-fail paths a visible health signal, not just a log line (Medium value / Medium effort / Low risk)

The backup-path bug above is the direct cautionary example: `backup_track()` was failing on every
single call for months, correctly logged to `yoto_errors.log` every time exactly as designed — and
nobody saw it, because nothing reads that log unless someone goes looking. The dashboard
(`server.py` + `index.html`) already exists and already surfaces a retry queue for failed uploads;
a small "last successful backup: <time>" (or "never") line there would have caught this in days,
not months, without requiring log-diving.

## 7. Reconsider the flat single retry on upload failure (Low value / Low effort / Low risk)

`_process_job`'s YouTube-track loop: `if not ok: time.sleep(5); ok, err = _upload_core(...)` — one
retry, flat 5s delay, no backoff, no distinction between a transient network blip (worth retrying)
and a real non-retryable failure like bad auth (retrying just wastes 5s before failing the same
way). Not observed to cause a real problem this session — flagging for awareness, not urgent.

## Considered and not recommended

- **Speeding up the download step itself** — already network- and yt-dlp-bound; no lever found
  during this session's investigation beyond what item 1 (parallelism) already captures.
- **Speeding up Yoto's own server-side transcode wait** — that's Yoto's infrastructure, not this
  codebase; nothing to change here. (Item 3 addresses the one thing under our control that touches
  it.)
- **Other environment-specific config landmines like the backup path** — looked for more, didn't
  find one. `_yt_dlp_cmd()`'s multi-path binary search already handles cross-platform deployment
  deliberately (Homebrew Mac paths, `/usr/local/bin`, PATH fallback); `TEMP_DIR`/`DOWNLOAD_DIR`
  under the system temp dir is already portable. Worth saying plainly rather than padding this list
  with a manufactured finding.
- **Longer/shorter poll interval in `_upload_core`'s transcode-wait loop** — the 1s interval adds
  API call volume but doesn't change how long the user actually waits, since that's bounded by
  Yoto's own transcode time either way. Not worth touching.
