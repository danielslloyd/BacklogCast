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
import wave
from dataclasses import asdict
from pathlib import Path

from mutagen import File as MutagenFile

from . import articles, bookjson, config, extractor, henty
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

def build_book(slug: str) -> dict:
    """Fetch (if needed), extract clean markdown, build Henty book.json, and
    persist it (to HENTY_BOOKS_DIR/<slug>/ when configured, else the article
    dir). Advances state to book_built (or needs_review on low confidence)."""
    meta = articles.load_meta(slug)
    body = articles.load_body(slug)
    url = meta.get("source_url")
    low = False
    if not body.strip() and url:
        html = extractor.fetch_url(url)
        articles.raw_html_path(slug).write_text(html, encoding="utf-8")
        result = extractor.extract(html, url=url)
        articles.update_article_text(
            slug, title=result["title"] or meta.get("title"),
            author=result["author"], body=result["body"],
        )
        articles.set_state(slug, "extracting", extraction_method=result["method"])
        low = result["confidence_low"]
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
        slug, "needs_review" if low else "book_built",
        book_json_path=book_path, n_chunks=len(bookjson.chunk_texts(book)),
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
    fname = st.get("stitched_filename")
    books_dir = config.henty_books_dir()
    if fname and books_dir:
        for cand in Path(books_dir).rglob(fname):
            return cand
    url = st.get("stitched_url") or (f"/api/project/audio/{fname}" if fname else None)
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
        if not (Path(books_dir) / slug / "book.json").exists():
            build_book(slug)
        henty.run_importer()  # needs HENTY_DIR; raises with guidance otherwise
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
    except Exception as e:
        log.exception("synthesis failed for %s", slug)
        articles.set_state(slug, "failed", error=str(e))


# --- background worker ------------------------------------------------------

class TTSWorker:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick = threading.Event()

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
                if approved:
                    synthesize(approved[0]["slug"])
                    continue
            except Exception:
                log.exception("worker loop error")
            self._tick.wait(timeout=5.0)
            self._tick.clear()
        log.info("TTS worker stopped")


worker = TTSWorker()
