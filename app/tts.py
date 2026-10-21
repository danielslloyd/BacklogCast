"""TTS worker: drive Henty (Stage C) and assemble the local episode (Stage D).

The old in-process Chatterbox path is gone. Henty (a separate, GPU-only Flask
studio) is the sole TTS engine. The background worker advances jobs through the
pipeline:

    queued  --extract + build book.json-->  book_built  --(auto)-->  approved
    approved  --Henty generate + ASR loop + stitch + mp3-->  stitched
    stitched  --(auto)-->  published   (served by the local feed)

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
         "-codec:a", "libmp3lame", "-qscale:a", "4", str(dst)],
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


# --- Stage B: fetch + extract + build book.json -----------------------------

def _h1_title(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
        if line.strip():
            break
    return ""


def extract_source(slug: str) -> str | None:
    """Fetch the job's URL (reusing a saved raw.html / source.pdf), draft it
    with trafilatura, then run the Claude cleanup per CLAUDE_EXTRACT. Saves the
    result as the article body. Returns a review reason when a human should
    look before synthesis, else None."""
    meta = articles.load_meta(slug)
    url = meta.get("source_url")
    raw, pdf = articles.raw_html_path(slug), articles.source_pdf_path(slug)
    if pdf.exists():
        kind, src = "pdf", pdf
    elif raw.exists():
        kind, src = "html", raw
    else:
        f = extractor.fetch(url)
        if f.kind == "pdf":
            pdf.write_bytes(f.content)
            kind, src = "pdf", pdf
        else:
            raw.write_text(f.text, encoding="utf-8")
            kind, src = "html", raw

    title, author, draft, method, low = meta.get("title") or "", "", "", "pdf", True
    if kind == "html":
        result = extractor.extract(src.read_text(encoding="utf-8"), url=url)
        draft, method, low = result["body"], result["method"], result["confidence_low"]
        title, author = result["title"] or title, result["author"]

    body, review = draft, ("low-confidence extraction" if low else None)
    mode = config.claude_extract_mode()
    if mode == "always" or (mode == "fallback" and (low or kind == "pdf")):
        try:
            cr = claude_extract.clean_with_claude(src, kind, draft)
            if cr.verdict == "not_article":
                review = f"Claude: not an article ({cr.reason})"
            else:
                body, method = cr.markdown, "claude"
                title = _h1_title(body) or title
                review = None
                if cr.verdict == "suspect":
                    log.warning("claude cleanup for %s looks off: %s", slug, cr.reason)
        except claude_extract.ClaudeExtractError as e:
            log.warning("claude cleanup failed for %s: %s", slug, e)
            if not draft.strip():
                raise
            review = f"Claude cleanup failed ({e}); automatic draft kept"

    if not body.strip():
        raise RuntimeError(f"no text extracted from {kind}")
    articles.update_article_text(slug, title=title, author=author, body=body)
    articles.set_state(slug, "extracting", extraction_method=method)
    return review


def build_book(slug: str) -> dict:
    """Extract (if the body is still empty), build Henty book.json, and
    persist it (to HENTY_BOOKS_DIR/<slug>/ when configured, else the article
    dir). Advances state to book_built (or needs_review when flagged)."""
    meta = articles.load_meta(slug)
    review = None
    if not articles.load_body(slug).strip() and meta.get("source_url"):
        review = extract_source(slug)
        meta = articles.load_meta(slug)
    body = articles.load_body(slug)

    book = bookjson.build(body, title=meta.get("title") or "")

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


def process_queued(slug: str) -> None:
    articles.set_state(slug, "extracting", error=None)
    try:
        build_book(slug)
        meta = articles.load_meta(slug)
        if meta.get("state") == "book_built" and config.load_config().get("auto_approve", True):
            articles.set_state(slug, "approved", approved_at=articles.utcnow_iso())
    except Exception as e:
        log.exception("extract/build failed for %s", slug)
        articles.set_state(slug, "failed", error=f"extract: {e}")


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
    articles.set_state(slug, "synthesizing", error=None)
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

        with henty.HentyClient() as client:
            client.load_project(project_path)
            out = henty.synthesize_project(client, voice_sample=config.default_voice())
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
                try:
                    lloydio.publish_episode(slug)
                except Exception:
                    # stays published locally; publish_pending retries it
                    log.exception("lloydio write-back failed for %s", slug)
    except Exception as e:
        log.exception("synthesis failed for %s", slug)
        articles.set_state(slug, "failed", error=str(e))


# --- background worker ------------------------------------------------------

WRITEBACK_RETRY_SECONDS = 600


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
        while not self._stop.is_set():
            try:
                queued = sorted(articles.iter_articles(state="queued"),
                                key=lambda m: m.get("fetched_at") or "")
                if queued:
                    process_queued(queued[0]["slug"])
                    continue
                approved = sorted(articles.iter_articles(state="approved"),
                                  key=lambda m: m.get("approved_at") or "")
                # Only synthesize when Henty (GPU) is up; else jobs wait in `approved`.
                if approved and henty.is_available():
                    synthesize(approved[0]["slug"])
                    continue
                if time.monotonic() - self._last_writeback > WRITEBACK_RETRY_SECONDS:
                    self._last_writeback = time.monotonic()
                    lloydio.publish_pending()
            except Exception:
                log.exception("worker loop error")
            self._tick.wait(timeout=5.0)
            self._tick.clear()
        log.info("TTS worker stopped")


worker = TTSWorker()
