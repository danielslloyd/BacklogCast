"""Henty client + ASR feedback loop (Stage C).

Henty is a local, GPU-only Flask TTS studio (Chatterbox / Chatterbox-Turbo) with
a built-in Whisper verifier. It exposes `/api/project/*` (auth: `X-API-Key`).
Book import is an offline CLI (`batch_import_books.py`), not an API, so we write
`book.json` into `HENTY_BOOKS_DIR/<slug>/` and run that importer, then drive
generation per chunk with a transcription feedback loop:

    generate-chunk-audio -> transcribe-take (Whisper) -> keep best-scoring take
    -> set-chunk-best-take -> stitch-best-takes (per chapter)

Field names in `/api/project/info` (chapter/chunk ids) are accessed defensively
because they can only be confirmed against a live Henty; see `_iter_chunks`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from .config import (
    asr_max_retries, asr_threshold, default_voice,
    henty_api_key, henty_base_url, henty_books_dir,
)

log = logging.getLogger("backlogcast.henty")


class HentyError(RuntimeError):
    pass


# --- markup resolution (mirror Henty's spoken-side of {display|spoken}) ------

_MARKUP_RE = re.compile(r"\{([^}]+)\}")


def resolve_markup(text: str) -> str:
    """Resolve {display|spoken} -> spoken (what Henty actually voices), so ASR
    similarity is measured against the spoken form, not the display form."""
    def repl(m: re.Match[str]) -> str:
        parts = m.group(1).split("|", 1)
        return parts[1].strip() if len(parts) == 2 else m.group(0)
    return _MARKUP_RE.sub(repl, text)


# --- HTTP client ------------------------------------------------------------

class HentyClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 600.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or henty_base_url()).rstrip("/")
        key = api_key if api_key is not None else henty_api_key()
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={"X-API-Key": key} if key else {},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HentyClient":
        return self

    def __exit__(self, *_a: Any) -> None:
        self.close()

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        r = self._client.post(path, json=payload)
        if r.status_code >= 400:
            raise HentyError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def _get(self, path: str) -> dict[str, Any]:
        r = self._client.get(path)
        if r.status_code >= 400:
            raise HentyError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def get_bytes(self, url: str) -> bytes:
        """Fetch raw bytes (e.g. a stitched WAV). Accepts absolute or relative URL."""
        r = self._client.get(url)
        if r.status_code >= 400:
            raise HentyError(f"GET {url} -> {r.status_code}")
        return r.content

    def load_project(self, project_path: str) -> dict[str, Any]:
        return self._post("/api/project/load", {"project_path": project_path})

    def project_info(self) -> dict[str, Any]:
        return self._get("/api/project/info")

    def generate_chunk(
        self, text_file_id: Any, chunk_id: Any, chunk_text: str, *,
        voice_sample: str | None = None, exaggeration: float = 0.5,
        cfg_weight: float = 0.5, temperature: float = 0.8,
        tts_model: str = "chatterbox",
    ) -> dict[str, Any]:
        return self._post("/api/project/generate-chunk-audio", {
            "text_file_id": text_file_id,
            "chunk_id": chunk_id,
            "chunk_text": chunk_text,
            "voice_sample": voice_sample or default_voice(),
            "exaggeration": exaggeration,
            "cfg_weight": cfg_weight,
            "temperature": temperature,
            "tts_model": tts_model,
        })

    def transcribe_take(
        self, audio_file: str, chunk_text: str, text_file_id: Any, chunk_id: Any,
    ) -> dict[str, Any]:
        return self._post("/api/project/transcribe-take", {
            "audio_file": audio_file,
            "chunk_text": chunk_text,
            "text_file_id": text_file_id,
            "chunk_id": chunk_id,
        })

    def set_best_take(self, text_file_id: Any, chunk_id: Any, audio_filename: str) -> dict[str, Any]:
        return self._post("/api/project/set-chunk-best-take", {
            "text_file_id": text_file_id,
            "chunk_id": chunk_id,
            "audio_filename": audio_filename,
        })

    def stitch_chapter(self, chapter_id: Any) -> dict[str, Any]:
        return self._post("/api/project/stitch-best-takes", {"chapter_id": chapter_id})


# --- ASR feedback loop ------------------------------------------------------

@dataclass
class ChunkResult:
    chapter_id: Any
    chunk_id: Any
    similarity: float          # 0..1 (best take)
    attempts: int
    truncated: bool
    audio_file: str | None
    ok: bool                   # met threshold and not truncated


def _norm_similarity(raw: Any) -> float:
    """Henty returns similarity as 0..100 (round(ratio*100,1)); normalize to 0..1."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return v / 100.0 if v > 1.0 else v


def _take_truncated(gen: dict[str, Any], audio_file: str | None) -> bool:
    """`possibly_truncated` lives on the new take inside `chunk.generated_audios`,
    NOT at the top level of the generate-chunk-audio response."""
    takes = (gen.get("chunk") or {}).get("generated_audios") or []
    for t in takes:
        if audio_file and t.get("audio_file") == audio_file:
            return bool(t.get("possibly_truncated"))
    return bool(takes[-1].get("possibly_truncated")) if takes else False


def synthesize_chunk_with_retries(
    client: HentyClient, text_file_id: Any, chunk_id: Any, chunk_text: str, *,
    voice_sample: str | None = None, threshold: float | None = None,
    max_retries: int | None = None, tts_model: str = "chatterbox",
) -> ChunkResult:
    """Generate a chunk, transcribe it, and regenerate while the Whisper
    similarity is below `threshold` (or the take is truncated), up to
    `max_retries` extra tries. Keeps and sets the highest-scoring take."""
    threshold = asr_threshold() if threshold is None else threshold
    max_retries = asr_max_retries() if max_retries is None else max_retries
    compare_text = resolve_markup(chunk_text)

    best_sim = 0.0
    best_file: str | None = None
    best_trunc = True
    best_key = (-1, -1.0)  # (non_truncated_flag, similarity): non-truncated wins ties
    attempts = 0
    while attempts <= max_retries:
        attempts += 1
        # Nudge temperature up on retries (effective once Henty forwards it).
        temperature = min(0.8 + 0.1 * (attempts - 1), 1.2)
        gen = client.generate_chunk(
            text_file_id, chunk_id, chunk_text,
            voice_sample=voice_sample, temperature=temperature, tts_model=tts_model,
        )
        audio_file = gen.get("audio_file")
        if not audio_file:
            continue  # generation produced nothing; try again
        truncated = bool(gen.get("possibly_truncated")) or _take_truncated(gen, audio_file)
        tr = client.transcribe_take(audio_file, compare_text, text_file_id, chunk_id)
        sim = _norm_similarity(tr.get("similarity_score"))
        key = (0 if truncated else 1, sim)
        if key > best_key:
            best_key, best_sim, best_file, best_trunc = key, sim, audio_file, truncated
        if sim >= threshold and not truncated:
            break

    if best_file:
        client.set_best_take(text_file_id, chunk_id, Path(best_file).name)
    ok = best_sim >= threshold and not best_trunc
    if not ok:
        log.warning("chunk %s/%s best similarity %.3f after %d tries (truncated=%s)",
                    text_file_id, chunk_id, best_sim, attempts, best_trunc)
    return ChunkResult(text_file_id, chunk_id, round(max(best_sim, 0.0), 3),
                       attempts, best_trunc, best_file, ok)


def _iter_chunks(info: dict[str, Any]) -> Iterator[tuple[Any, Any, str]]:
    """Yield (chapter_id, chunk_id, text) for voiceable chunks in a project.

    Defensive about id field names (`id`/`chapter_id`/`text_file_id`) since they
    are only confirmable against a live Henty.
    """
    meta = info.get("metadata") or info
    for c_idx, ch in enumerate(meta.get("chapters", [])):
        chapter_id = ch.get("id", ch.get("chapter_id", ch.get("text_file_id", c_idx)))
        for k_idx, chunk in enumerate(ch.get("chunks", [])):
            ctype = chunk.get("type", "text")
            if ctype not in ("text", "para", "verse", None):
                continue  # skip pause / common-file chunks
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            chunk_id = chunk.get("id", chunk.get("chunk_id", k_idx))
            yield chapter_id, chunk_id, text


def synthesize_project(
    client: HentyClient, *, voice_sample: str | None = None,
    threshold: float | None = None, max_retries: int | None = None,
    progress: Callable[[ChunkResult], None] | None = None,
) -> dict[str, Any]:
    """Run the ASR loop over every chunk in the loaded project, then stitch each
    chapter. Returns {"reports": [ChunkResult...], "stitched": [stitch dict...]}."""
    info = client.project_info()
    reports: list[ChunkResult] = []
    stitched: list[dict[str, Any]] = []
    seen_chapters: list[Any] = []
    for chapter_id, chunk_id, text in _iter_chunks(info):
        res = synthesize_chunk_with_retries(
            client, chapter_id, chunk_id, text,
            voice_sample=voice_sample, threshold=threshold, max_retries=max_retries,
        )
        reports.append(res)
        if progress:
            progress(res)
        if chapter_id not in seen_chapters:
            seen_chapters.append(chapter_id)
    for chapter_id in seen_chapters:
        st = client.stitch_chapter(chapter_id)
        if st.get("stitched_filename") or st.get("stitched_url"):
            stitched.append(st)
    return {"reports": reports, "stitched": stitched}


# --- book.json import (co-located with Henty) -------------------------------

def write_book_json(slug: str, book: dict[str, Any], books_dir: str | None = None) -> Path:
    """Write book.json into HENTY_BOOKS_DIR/<slug>/. Returns the folder path."""
    books_dir = books_dir or henty_books_dir()
    if not books_dir:
        raise HentyError("HENTY_BOOKS_DIR is not configured")
    folder = Path(books_dir) / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "book.json").write_text(
        json.dumps(book, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return folder


def run_importer(henty_dir: str | None = None) -> tuple[bool, str]:
    """Run Henty's `batch_import_books.py` to build project.json for the books in
    BOOKS_DIR. Requires the orchestrator to be co-located with Henty (HENTY_DIR).
    If not, drop book.json and run the importer manually, then call load_project.
    """
    henty_dir = henty_dir or os.environ.get("HENTY_DIR", "")
    if not henty_dir:
        raise HentyError(
            "HENTY_DIR not set: run `python batch_import_books.py` on the Henty "
            "box, then call HentyClient.load_project(<books_dir>/<slug>/project.json)"
        )
    proc = subprocess.run(
        [sys.executable, "batch_import_books.py"],
        cwd=henty_dir, capture_output=True, text=True,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def project_path_for(slug: str, books_dir: str | None = None) -> str:
    books_dir = books_dir or henty_books_dir()
    return str(Path(books_dir) / slug / "project.json")
