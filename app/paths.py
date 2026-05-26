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
