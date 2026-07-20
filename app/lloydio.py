"""lloydio ingest (Stage A): poll the hosted capture queue, create local jobs.

lloydio exposes a public, unauthenticated queue at
`GET {LLOYDIO_BASE_URL}/api/podcast-queue.json`:

    {"version": .., "generated": .., "repo": .., "branch": ..,
     "items": [{"slug", "url", "title", "date", "tags", "status",
                "article_exists", "has_audio", "article_path",
                "suggested_audio_repo_path", "suggested_audio_url"}, ...]}

The queue is already filtered to items needing work (`status=="queued"` or no
audio). We dedupe against the local store by `source_url` and create jobs in
state `queued`; the TTS worker then extracts, builds book.json, drives Henty,
and publishes to the local feed. We never write back to lloydio — the local
store is the source of truth for "done".
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import articles
from .config import lloydio_base_url

log = logging.getLogger("backlogcast.lloydio")

QUEUE_PATH = "/api/podcast-queue.json"


def fetch_queue(
    base_url: str | None = None,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Return the queue's items list. Raises if lloydio is unreachable/unset."""
    base = (base_url or lloydio_base_url()).rstrip("/")
    if not base:
        raise RuntimeError("lloydio_base_url is not configured")
    url = f"{base}{QUEUE_PATH}"
    with httpx.Client(timeout=timeout, follow_redirects=True, transport=transport) as c:
        r = c.get(url)
        r.raise_for_status()
        data = r.json()
    items = data.get("items", []) if isinstance(data, dict) else []
    return [it for it in items if isinstance(it, dict)]


def needs_narration(item: dict[str, Any]) -> bool:
    """True if this queue item should be narrated (has a URL, no audio yet)."""
    if not item.get("url"):
        return False
    if item.get("has_audio"):
        return False
    status = (item.get("status") or "queued").lower()
    return status == "queued" or not item.get("has_audio")


def sync(
    base_url: str | None = None,
    create: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, list[str]]:
    """Poll the queue and create local jobs for new items.

    Returns {"created": [slugs], "skipped": [urls]}. Deduped by source_url so
    re-polling is idempotent. `create=False` does a dry run.
    """
    created: list[str] = []
    skipped: list[str] = []
    for item in fetch_queue(base_url, transport=transport):
        url = item.get("url") or ""
        if not needs_narration(item):
            skipped.append(url or item.get("slug") or "?")
            continue
        if articles.find_by_source_url(url):
            skipped.append(url)
            continue
        if not create:
            created.append(url)
            continue
        meta = articles.new_article(
            title=item.get("title") or url,
            source_url=url,
            body="",
            raw_html=None,
            extraction_method="lloydio-queue",
            author="",
            state="queued",
        )
        articles.set_state(
            meta["slug"],
            "queued",
            lloydio_slug=item.get("slug"),
            tags=item.get("tags") or [],
        )
        created.append(meta["slug"])
        log.info("queued from lloydio: %s (%s)", meta["slug"], url)
    log.info("lloydio sync: +%d new, %d skipped", len(created), len(skipped))
    return {"created": created, "skipped": skipped}
