"""Chatterbox TTS worker.

Chatterbox is treated as an external dependency that may be invoked one of two ways,
in this order of preference:

1. As an imported Python module (`chatterbox.tts`). This is the cleanest path and
   lets us synthesize in-process and skip subprocess overhead.
2. As a CLI subprocess (`chatterbox` on PATH). Useful when the install is in a
   separate venv or otherwise not importable here.

Long articles are chunked on paragraph boundaries and concatenated. Concatenation
is straight-binary for WAV (via wave module) and ffmpeg concat-demuxer for MP3.
If ffmpeg isn't available the worker emits WAV; the RSS feed will set the
enclosure type accordingly.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from pathlib import Path

from mutagen import File as MutagenFile

from . import articles
from .articles import audio_path

log = logging.getLogger("backlogcast.tts")


# --- Chatterbox adapter ----------------------------------------------------

class ChatterboxError(RuntimeError):
    pass


def _has_chatterbox_import() -> bool:
    try:
        import chatterbox  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _has_chatterbox_cli() -> bool:
    return shutil.which("chatterbox") is not None


def _synthesize_via_import(text: str, out_path: Path) -> None:
    # Import lazily so the rest of the app runs even without chatterbox installed.
    try:
        from chatterbox.tts import ChatterboxTTS  # type: ignore
    except Exception as e:
        raise ChatterboxError(f"chatterbox import failed: {e}")
    device = os.environ.get("CHATTERBOX_DEVICE", "cuda")
    model = ChatterboxTTS.from_pretrained(device=device)
    wav = model.generate(text)
    # Chatterbox returns a torch tensor; save via torchaudio if present, else wave.
    try:
        import torchaudio  # type: ignore
        torchaudio.save(str(out_path), wav, model.sr)
    except Exception:
        import numpy as np
        arr = wav.detach().cpu().numpy().squeeze()
        with wave.open(str(out_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(model.sr)
            wf.writeframes((arr * 32767).astype("int16").tobytes())


def _synthesize_via_cli(text: str, out_path: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        tf.write(text)
        text_path = tf.name
    try:
        cmd = ["chatterbox", "--text-file", text_path, "--output", str(out_path)]
        device = os.environ.get("CHATTERBOX_DEVICE")
        if device:
            cmd += ["--device", device]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise ChatterboxError(
                f"chatterbox CLI failed ({result.returncode}): {result.stderr.strip()}"
            )
    finally:
        try:
            os.unlink(text_path)
        except OSError:
            pass


def synthesize_chunk(text: str, out_path: Path) -> None:
    if _has_chatterbox_import():
        _synthesize_via_import(text, out_path)
        return
    if _has_chatterbox_cli():
        _synthesize_via_cli(text, out_path)
        return
    raise ChatterboxError(
        "Chatterbox not available: neither `chatterbox` Python module nor CLI found. "
        "Install it and retry; see README."
    )


# --- chunking & concatenation ---------------------------------------------

# Chatterbox prompts are typically <= ~500 chars; chunk well below that for safety.
CHUNK_TARGET_CHARS = int(os.environ.get("BACKLOGCAST_CHUNK_CHARS", "1200"))


def chunk_text(body: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    cur_len = 0
    for p in paragraphs:
        if cur_len + len(p) > target and buf:
            chunks.append("\n\n".join(buf))
            buf = [p]
            cur_len = len(p)
        else:
            buf.append(p)
            cur_len += len(p) + 2
    if buf:
        chunks.append("\n\n".join(buf))
    return chunks or [body.strip()]


def _concat_wavs(parts: list[Path], out: Path) -> None:
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


def _have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _wav_to_mp3(src: Path, dst: Path) -> bool:
    if not _have_ffmpeg():
        return False
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-codec:a", "libmp3lame", "-qscale:a", "4", str(dst)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and dst.exists()


def _measure_duration_seconds(path: Path) -> int:
    try:
        mf = MutagenFile(str(path))
        if mf is not None and mf.info and mf.info.length:
            return int(mf.info.length)
    except Exception:
        pass
    # wave fallback
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                return int(w.getnframes() / float(w.getframerate()))
        except Exception:
            return 0
    return 0


# --- worker ---------------------------------------------------------------

def synthesize_article(slug: str) -> None:
    """Synthesize TTS for one article. Updates state on the article."""
    articles.set_state(slug, "synthesizing", error=None)
    try:
        body = articles.load_body(slug)
        if not body.strip():
            raise ChatterboxError("article body is empty")
        chunks = chunk_text(body)
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            wav_parts: list[Path] = []
            for i, chunk in enumerate(chunks):
                part = tdir / f"part_{i:04d}.wav"
                synthesize_chunk(chunk, part)
                wav_parts.append(part)
            combined_wav = tdir / "combined.wav"
            _concat_wavs(wav_parts, combined_wav)

            art_dir = articles.article_dir(slug)
            art_dir.mkdir(parents=True, exist_ok=True)
            mp3_target = art_dir / "audio.mp3"
            wav_target = art_dir / "audio.wav"
            if _wav_to_mp3(combined_wav, mp3_target):
                final = mp3_target
                fname = "audio.mp3"
            else:
                shutil.copyfile(combined_wav, wav_target)
                final = wav_target
                fname = "audio.wav"

        duration = _measure_duration_seconds(final)
        articles.set_state(
            slug,
            "ready",
            audio_filename=fname,
            audio_bytes=final.stat().st_size,
            duration_seconds=duration,
            error=None,
        )
        log.info("synthesized %s (%ss, %s bytes)", slug, duration, final.stat().st_size)
    except Exception as e:
        log.exception("synthesis failed for %s", slug)
        articles.set_state(slug, "failed", error=str(e))


# --- background loop ------------------------------------------------------

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
                pending = sorted(
                    (m for m in articles.iter_articles(state="approved")),
                    key=lambda m: m.get("approved_at") or "",
                )
                if pending:
                    synthesize_article(pending[0]["slug"])
                    continue  # immediately look for the next one
            except Exception:
                log.exception("worker loop error")
            # wait for a kick or a 5s tick
            self._tick.wait(timeout=5.0)
            self._tick.clear()
        log.info("TTS worker stopped")


worker = TTSWorker()
