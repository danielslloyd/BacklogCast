"""TTS worker: drive Henty (Stage C) and assemble the local episode (Stage D).

The old in-process Chatterbox path is gone. Henty (a separate, GPU-only Flask
studio) is the sole TTS engine. The background worker advances jobs through the
pipeline, always doing text work before narration:

    queued  --extract + build book.json-->  text_ready      (text-only: text sent to lloydio)
                                       or->  approved        (narrate=True)
    approved  --Henty generate + ASR loop + stitch + mp3-->  stitched
    stitched  --(auto)-->  published   (served by the local feed, sent to lloydio)

Only lloydio (capture) is remote; everything here runs locally next to Henty.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import asdict
from urllib.parse import urlsplit
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mutagen import File as MutagenFile

from . import articles, bookjson, claude_extract, config, extractor, henty, lloydio
from .articles import article_dir

log = logging.getLogger("backlogcast.tts")


# --- audio assembly helpers (kept from the old worker) ----------------------

def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _concat_wavs(parts: list[Path], out: Path) -> None:
    parts = [p for p in parts if p and Path(p).exists()]
    if not parts:
        raise RuntimeError("no wav parts to concatenate")
    if len(parts) == 1:
        shutil.copyfile(parts[0], out)
        return
    with wave.open(str(parts[0]), "rb") as w0:
        params = w0.getparams()
        frames = [w0.readframes(w0.getnframes())]
    for p in parts[1:]:
        with wave.open(str(p), "rb") as w:
            frames.append(w.readframes(w.getnframes()))
    with wave.open(str(out), "wb") as wo:
        wo.setparams(params)
        for f in frames:
            wo.writeframes(f)


def _wav_to_mp3(src: Path, dst: Path) -> bool:
    if not _have_ffmpeg():
        return False
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         # 32k mono: a single narrated voice at 24 kHz needs no more, and it
         # keeps even a four-hour episode under the 100 MiB request body cap
         # Cloudflare puts in front of lloydio's /api/episode.
         "-codec:a", "libmp3lame", "-b:a", "32k", "-ac", "1", str(dst)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and Path(dst).exists()


def _measure_duration_seconds(path: Path) -> int:
    try:
        mf = MutagenFile(str(path))
        if mf is not None and mf.info and mf.info.length:
            return int(mf.info.length)
    except Exception:
        pass
    if str(path).lower().endswith(".wav"):
        try:
            with wave.open(str(path), "rb") as w:
                return int(w.getnframes() / float(w.getframerate()))
        except Exception:
            return 0
    return 0


# --- live progress ------------------------------------------------------------
#
# What the worker is doing right now, for the Queues tab: one job at a time, so
# a single dict. In memory only; it resets when the server restarts.

_progress: dict = {}
_progress_lock = threading.Lock()


def set_progress(slug: str, stage: str, step: str,
                 done: int | None = None, total: int | None = None) -> None:
    with _progress_lock:
        if _progress.get("slug") != slug or _progress.get("stage") != stage:
            _progress.clear()
            _progress.update(slug=slug, stage=stage, started=articles.utcnow_iso())
        _progress.update(step=step, done=done, total=total, updated=articles.utcnow_iso())


def clear_progress(slug: str) -> None:
    with _progress_lock:
        if _progress.get("slug") == slug:
            _progress.clear()


def current_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


# --- bounded retries ---------------------------------------------------------
#
# A failed extraction or narration goes back to its waiting state with a
# `retry_after` time, up to MAX_ATTEMPTS tries per stage; then it stays failed
# (a human can still retry it from the UI). The backoff is long enough to ride
# out a Claude session limit (resets within ~5h).

MAX_ATTEMPTS = 4
RETRY_DELAYS = (15 * 60, 60 * 60, 4 * 60 * 60)  # before attempts 2, 3, 4


class PermanentError(RuntimeError):
    """A failure retrying won't fix (e.g. the page isn't an article)."""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def due(meta: dict) -> bool:
    """False while a job is waiting out its retry backoff."""
    ra = meta.get("retry_after")
    return not ra or ra <= articles.utcnow_iso()


def attempt_no(meta: dict, stage: str) -> int:
    """1-based number of the attempt about to run for `stage` (extract|synth)."""
    return int(meta.get(f"{stage}_attempts") or 0) + 1


def _retry_or_fail(slug: str, stage: str, error: str, retry_state: str,
                   permanent: bool = False) -> str | None:
    """Record a failed attempt. Returns the retry time if another attempt is
    scheduled (job back in `retry_state`), else None (job is `failed`)."""
    n = attempt_no(articles.load_meta(slug), stage)
    if permanent or n >= MAX_ATTEMPTS:
        articles.set_state(slug, "failed", error=error, retry_after=None,
                           **{f"{stage}_attempts": n})
        return None
    when = _iso(datetime.now(timezone.utc) + timedelta(seconds=RETRY_DELAYS[n - 1]))
    articles.set_state(slug, retry_state, error=error, retry_after=when,
                       **{f"{stage}_attempts": n})
    log.info("%s of %s failed (attempt %d/%d); retrying after %s",
             stage, slug, n, MAX_ATTEMPTS, when)
    return when


# --- Stage B: fetch + extract + build book.json -----------------------------

def _h1_title(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            break
    return ""


def extract_source(slug: str, last_try: bool = True) -> str | None:
    """Fetch the job's URL (reusing a saved raw.html / source.pdf), draft it
    with trafilatura, then run the Claude cleanup per CLAUDE_EXTRACT. Saves the
    result as the article body. Returns a review reason when a human should
    look before synthesis, else None.

    A failed Claude cleanup raises (so the job is retried) unless `last_try`,
    when the automatic draft is kept instead. Raises PermanentError when Claude
    says the page isn't an article."""
    meta = articles.load_meta(slug)
    url = meta.get("source_url")
    raw, pdf = articles.raw_html_path(slug), articles.source_pdf_path(slug)
    if pdf.exists():
        kind, src = "pdf", pdf
    elif raw.exists():
        kind, src = "html", raw
    else:
        set_progress(slug, "extract", "fetching")
        f = extractor.fetch(url)
        if f.kind == "pdf":
            pdf.write_bytes(f.content)
            kind, src = "pdf", pdf
        else:
            raw.write_text(f.text, encoding="utf-8")
            kind, src = "html", raw

    title, author, draft, method, low = meta.get("title") or "", "", "", "pdf", True
    publication, published = "", ""
    if kind == "html":
        set_progress(slug, "extract", "drafting")
        result = extractor.extract(src.read_text(encoding="utf-8"), url=url)
        draft, method, low = result["body"], result["method"], result["confidence_low"]
        title, author = result["title"] or title, result["author"]
        publication, published = result.get("publication", ""), result.get("published", "")

    body, review = draft, ("low-confidence extraction" if low else None)
    mode = config.claude_extract_mode()
    if mode == "always" or (mode == "fallback" and (low or kind == "pdf")):
        try:
            set_progress(slug, "extract", "Claude cleanup")
            cr = claude_extract.clean_with_claude(src, kind, draft)
            if cr.verdict == "not_article":
                raise PermanentError(f"not an article ({cr.reason})")
            else:
                body, method = cr.markdown, "claude"
                title = _h1_title(body) or title
                # Claude read the actual document: its byline facts win.
                author = cr.meta.get("author") or author
                publication = cr.meta.get("publication") or publication
                published = cr.meta.get("date") or published
                review = None
                if cr.verdict == "suspect":
                    log.warning("claude cleanup for %s looks off: %s", slug, cr.reason)
        except claude_extract.ClaudeExtractError as e:
            log.warning("claude cleanup failed for %s: %s", slug, e)
            if not draft.strip() or not last_try:
                raise
            review = f"Claude cleanup failed ({e}); automatic draft kept"

    if not body.strip():
        # Likely a block/interstitial page: drop it so a retry fetches afresh.
        src.unlink(missing_ok=True)
        raise RuntimeError(f"no text extracted from {kind}")
    articles.update_article_text(slug, title=title, author=author, body=body)
    articles.set_state(slug, "extracting", extraction_method=method,
                       publication=publication or _host(url), published=published)
    return review


def _host(url: str | None) -> str:
    host = urlsplit(url or "").hostname or ""
    return host.removeprefix("www.")


_MONTHS = ("January February March April May June July August September "
           "October November December").split()


def _spoken_date(d: str) -> str:
    """'2024-03-05' -> 'March 5, 2024'; partial dates degrade gracefully."""
    parts = d.split("-")
    try:
        if len(parts) >= 2:
            month = _MONTHS[int(parts[1]) - 1]
            return f"{month} {int(parts[2])}, {parts[0]}" if len(parts) == 3 else f"{month} {parts[0]}"
    except (ValueError, IndexError):
        return d
    return d


def intro_text(meta: dict, title_spoken: bool) -> str:
    """The spoken opening: title (unless a heading already says it), author,
    publication and date, as far as they're known."""
    bits = []
    title = (meta.get("title") or "").strip()
    if title and not title_spoken and not title.startswith("http"):
        bits.append(f"{title}.")
    if meta.get("author"):
        bits.append(f"By {meta['author']}.")
    pub, date = meta.get("publication") or "", meta.get("published") or ""
    if pub and date:
        bits.append(f"Published in {pub}, {_spoken_date(date)}.")
    elif pub:
        bits.append(f"From {pub}.")
    elif date:
        bits.append(f"Published {_spoken_date(date)}.")
    return " ".join(bits)


def build_book(slug: str, last_try: bool = True) -> dict:
    """Extract (if the body is still empty), build Henty book.json, and
    persist it (to HENTY_BOOKS_DIR/<slug>/ when configured, else the article
    dir). Advances state to book_built (or needs_review when flagged)."""
    meta = articles.load_meta(slug)
    review = None
    if not articles.load_body(slug).strip() and meta.get("source_url"):
        review = extract_source(slug, last_try=last_try)
        meta = articles.load_meta(slug)
    body = articles.load_body(slug)

    set_progress(slug, "extract", "building book.json")
    book = bookjson.build(body, title=meta.get("title") or "",
                          intro=lambda title_spoken: intro_text(meta, title_spoken))

    books_dir = config.henty_books_dir()
    if books_dir:
        folder = henty.write_book_json(slug, book, books_dir)
        book_path = str(folder / "book.json")
    else:
        p = article_dir(slug) / "book.json"
        p.write_text(json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8")
        book_path = str(p)

    articles.set_state(
        slug, "needs_review" if review else "book_built",
        book_json_path=book_path, n_chunks=len(bookjson.chunk_texts(book)),
        review_reason=review,
    )
    return book


def _stop_narrating(slug: str, lslug: str | None) -> None:
    """Owner cancelled or un-queued narration on lloydio. The link stays on the
    reading list, so keep (or still produce) its clean text: text-only from
    here, until it's queued for narration again."""
    has_body = bool(articles.load_body(slug).strip())
    articles.set_state(slug, "text_ready" if has_body else "queued", error=None,
                       narrate=False, narrate_changed_at=articles.utcnow_iso())
    lloydio.report_status(lslug, "clear")
    log.info("narration of %s cancelled from lloydio", slug)
    if has_body:
        _send_text(slug)


def _send_text(slug: str) -> None:
    meta = articles.load_meta(slug)
    if lloydio.text_publishable(meta) and config.lloydio_episode_token():
        try:
            lloydio.publish_text(slug)
        except lloydio.Removed:
            pass
        except Exception:
            # publish_pending retries it
            log.exception("lloydio text write-back failed for %s", slug)


def process_queued(slug: str) -> None:
    """Extract clean text. Narration jobs then go on to Henty (approved);
    text-only jobs are done once their text is on lloydio (text_ready).
    Failures are retried with backoff (see _retry_or_fail)."""
    meta = articles.load_meta(slug)
    lslug = meta.get("lloydio_slug")
    if lloydio.removed(lslug):
        lloydio.mark_removed(slug)
        return
    narrate = lloydio.narrating(meta)
    if narrate and lloydio.cancel_requested(lslug):
        articles.set_state(slug, "queued", narrate=False,
                           narrate_changed_at=articles.utcnow_iso())
        lloydio.report_status(lslug, "clear")
        narrate = False
    last_try = attempt_no(meta, "extract") >= MAX_ATTEMPTS
    articles.set_state(slug, "extracting", error=None)
    lloydio.report_text_status(lslug, "in_progress")
    if narrate:
        lloydio.report_status(lslug, "in_progress")
    set_progress(slug, "extract", "starting")
    try:
        build_book(slug, last_try=last_try)
    except Exception as e:
        clear_progress(slug)
        log.exception("extract/build failed for %s", slug)
        err = f"extract: {e}"
        retry = _retry_or_fail(slug, "extract", err, "queued",
                               permanent=isinstance(e, PermanentError))
        lloydio.report_text_status(lslug, "waiting" if retry else "failed", err)
        if narrate:
            lloydio.report_status(lslug, "clear" if retry else "failed")
        return
    clear_progress(slug)
    meta = articles.set_state(slug, articles.load_meta(slug)["state"], retry_after=None)
    lloydio.report_text_status(lslug, "clear")
    if not narrate:
        # Text-only: a flagged draft is still better than no md link.
        articles.set_state(slug, "text_ready")
        _send_text(slug)
    elif meta.get("state") == "needs_review":
        lloydio.report_status(lslug, "failed")
    elif meta.get("state") == "book_built" and config.load_config().get("auto_approve", True):
        articles.set_state(slug, "approved", approved_at=articles.utcnow_iso())


# --- Stage C + D: Henty synthesis + local episode assembly ------------------

def _fetch_stitched(client: henty.HentyClient, st: dict, dest_dir: str) -> Path | None:
    """Get one stitched chapter WAV onto local disk. Prefers a co-located file
    under HENTY_BOOKS_DIR; otherwise downloads it over HTTP."""
    # Henty's stitch-best-takes returns audio_path (absolute), audio_file, audio_url
    path = st.get("audio_path")
    if path and Path(path).is_file():
        return Path(path)
    fname = st.get("audio_file")
    books_dir = config.henty_books_dir()
    if fname and books_dir:
        for cand in Path(books_dir).rglob(fname):
            return cand
    # stitched WAVs are served at /api/project/audio/<file>
    url = st.get("audio_url") or (f"/api/project/audio/{fname}" if fname else None)
    if not url:
        return None
    try:
        data = client.get_bytes(url)
    except henty.HentyError:
        return None
    dest = Path(dest_dir) / (fname or "chapter.wav")
    dest.write_bytes(data)
    return dest


def synthesize(slug: str) -> None:
    """Drive Henty for one job: import -> ASR loop per chunk -> stitch -> mp3."""
    meta = articles.load_meta(slug)
    lslug = meta.get("lloydio_slug")
    if lloydio.removed(lslug):
        lloydio.mark_removed(slug)
        return
    if not lloydio.narrating(meta) or lloydio.cancel_requested(lslug):
        _stop_narrating(slug, lslug)
        return
    articles.set_state(slug, "synthesizing", error=None)
    lloydio.report_status(lslug, "in_progress")
    set_progress(slug, "synth", "importing into Henty")
    try:
        books_dir = config.henty_books_dir()
        if not books_dir:
            raise henty.HentyError(
                "HENTY_BOOKS_DIR not configured; cannot hand book.json to Henty"
            )
        book_folder = Path(books_dir) / slug
        if not (book_folder / "book.json").exists():
            build_book(slug)
        ok, out = henty.run_importer(folder=str(book_folder))
        if not ok:
            raise henty.HentyError(f"book import failed: {out}")
        project_path = henty.project_path_for(slug)

        total = articles.load_meta(slug).get("n_chunks")
        done = 0

        def chunk_done(_res) -> None:
            nonlocal done
            done += 1
            set_progress(slug, "synth", "narrating", done, total)

        with henty.HentyClient() as client:
            client.load_project(project_path)
            set_progress(slug, "synth", "narrating", 0, total)
            out = henty.synthesize_project(
                client, voice_sample=config.default_voice(),
                progress=chunk_done,
                should_stop=lambda: lloydio.cancel_requested(lslug),
            )
            set_progress(slug, "synth", "stitching", done, total)
            with tempfile.TemporaryDirectory() as td:
                wavs = [w for st in out["stitched"]
                        if (w := _fetch_stitched(client, st, td))]
                if not wavs:
                    raise henty.HentyError("Henty produced no stitched audio")
                combined = Path(td) / "combined.wav"
                _concat_wavs(wavs, combined)
                d = article_dir(slug)
                d.mkdir(parents=True, exist_ok=True)
                mp3 = d / "audio.mp3"
                set_progress(slug, "synth", "encoding mp3", done, total)
                if _wav_to_mp3(combined, mp3):
                    final, fname = mp3, "audio.mp3"
                else:
                    final = d / "audio.wav"
                    shutil.copyfile(combined, final)
                    fname = "audio.wav"

        dur = _measure_duration_seconds(final)
        below = [r for r in out["reports"] if not r.ok]
        articles.set_state(
            slug, "stitched", audio_filename=fname, audio_bytes=final.stat().st_size,
            duration_seconds=dur, error=None,
            chunk_reports=[asdict(r) for r in out["reports"]],
            chunks_below_threshold=len(below),
        )
        log.info("synthesized %s (%ss, %d chunks, %d below threshold)",
                 slug, dur, len(out["reports"]), len(below))
        if config.load_config().get("auto_publish", True):
            articles.set_state(slug, "published", published_at=articles.utcnow_iso())
            if lloydio.publishable(articles.load_meta(slug)) and config.lloydio_episode_token():
                set_progress(slug, "synth", "uploading to lloydio", done, total)
                try:
                    lloydio.publish_episode(slug)
                except lloydio.Removed:
                    pass
                except Exception:
                    # stays published locally; publish_pending retries it
                    log.exception("lloydio write-back failed for %s", slug)
    except henty.JobCancelled:
        if lloydio.removed(lslug):
            lloydio.mark_removed(slug)
        else:
            _stop_narrating(slug, lslug)
    except Exception as e:
        log.exception("synthesis failed for %s", slug)
        retry = _retry_or_fail(slug, "synth", str(e), "approved")
        # A scheduled retry reads as plain "queued" on /reading meanwhile.
        lloydio.report_status(lslug, "clear" if retry else "failed")
    finally:
        clear_progress(slug)


# --- background worker ------------------------------------------------------

WRITEBACK_RETRY_SECONDS = 600


def recover_interrupted() -> list[str]:
    """Jobs left mid-step by a restart (only one worker runs, so nothing else
    is working on them): put them back where the worker will pick them up.
    Doesn't count as a failed attempt."""
    back = {"extracting": "queued", "synthesizing": "approved"}
    moved = []
    for m in articles.iter_articles():
        nxt = back.get(m.get("state"))
        if nxt:
            articles.set_state(m["slug"], nxt)
            moved.append(m["slug"])
            log.info("resuming %s (was %s when the server stopped)", m["slug"], m["state"])
    return moved


class TTSWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick = threading.Event()
        self._last_writeback = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tts-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._tick.set()

    def kick(self) -> None:
        self._tick.set()

    def _run(self) -> None:
        log.info("TTS worker started")
        recover_interrupted()
        while not self._stop.is_set():
            try:
                if time.monotonic() - self._last_writeback > WRITEBACK_RETRY_SECONDS:
                    self._last_writeback = time.monotonic()
                    lloydio.publish_pending()
                # Text first: every pending extraction runs before any narration.
                queued = sorted((m for m in articles.iter_articles(state="queued") if due(m)),
                                key=lambda m: m.get("fetched_at") or "")
                if queued:
                    process_queued(queued[0]["slug"])
                    continue
                approved = sorted((m for m in articles.iter_articles(state="approved") if due(m)),
                                  key=lambda m: m.get("approved_at") or "")
                # Only synthesize when Henty (GPU) is up; else jobs wait in `approved`.
                if approved and henty.is_available():
                    synthesize(approved[0]["slug"])
                    continue
            except Exception:
                log.exception("worker loop error")
            self._tick.wait(timeout=5.0)
            self._tick.clear()
        log.info("TTS worker stopped")


worker = TTSWorker()
