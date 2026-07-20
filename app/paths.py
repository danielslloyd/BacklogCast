"""Filesystem paths and bootstrap."""
from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

ROOT = Path(os.environ.get("BACKLOGCAST_DATA", "./data")).resolve()
ARTICLES_DIR = ROOT / "articles"
CONFIG_PATH = ROOT / "config.json"
TOKENS_PATH = ROOT / "tokens.json"
COVER_PATH = ROOT / "cover.jpg"

DEFAULT_CONFIG = {
    "podcast_title": "BacklogCast",
    "podcast_description": "My personal reading backlog, narrated.",
    "podcast_language": "en-us",
    "podcast_author": "Me",
    "owner_name": "Me",
    "owner_email": "me@example.com",
    "category": "Technology",
    "explicit": False,
    "public_base_url": "",
    "wpm": 160,
    # --- integration: lloydio (ingest) + Henty (TTS) ---
    # lloydio hosts capture + the /api/podcast-queue.json work queue we poll.
    "lloydio_base_url": "",
    # Henty is the local GPU TTS studio (Flask). API key is env-only (HENTY_API_KEY).
    "henty_base_url": "http://127.0.0.1:5000",
    # Directory Henty scans for book.json projects. Empty = don't drop book.json
    # locally (assume a shared/mounted path is configured on the Henty box).
    "henty_books_dir": "",
    # Reference voice sample name in Henty's voice_samples/ (never its built-in default).
    "default_voice": "Haggard",
    # ASR feedback loop: regenerate a chunk while Whisper similarity is below this,
    # up to this many tries, keeping the best-scoring take.
    "asr_similarity_threshold": 0.85,
    "asr_max_retries": 4,
    # Auto-advance jobs through approve/publish gates (True) or require the UI (False).
    "auto_approve": True,
    "auto_publish": True,
    # background poll of lloydio's queue, in seconds (0 = disabled)
    "lloydio_poll_seconds": 0,
}


def ensure_data_tree() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
    if not TOKENS_PATH.exists():
        # bootstrap a single token so the user has something usable on day one
        initial = {"default": secrets.token_urlsafe(32)}
        TOKENS_PATH.write_text(json.dumps(initial, indent=2))


def article_dir(slug: str) -> Path:
    return ARTICLES_DIR / slug
