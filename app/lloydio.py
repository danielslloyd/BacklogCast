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
and publishes to the local feed.

Write-back (Stage E): `publish_episode` POSTs the finished mp3 + clean text to
lloydio's `/api/episode` (Bearer LLOYDIO_EPISODE_TOKEN). lloydio stores the mp3
in R2 and commits `src/content/articles/<slug>.md`, which takes the item off the
queue and puts it in the always-on feed. Uploads that fail are retried by
`publish_pending`.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from . import articles
from .articles import article_dir
from .config import lloydio_base_url, lloydio_episode_token

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


EPISODE_PATH = "/api/episode"


def publishable(meta: dict[str, Any]) -> bool:
    """Locally published, has a source URL (lloydio's schema requires one) and
    an audio file, and hasn't been written back yet."""
    return (meta.get("state") == "published" and bool(meta.get("source_url"))
            and bool(meta.get("audio_filename")) and not meta.get("lloydio_published_at"))


def publish_episode(
    slug: str,
    base_url: str | None = None,
    timeout: float = 300.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Upload one finished episode to lloydio. Records lloydio_published_at on
    success, or lloydio_error on failure (the job itself stays published)."""
    meta = articles.load_meta(slug)
    base = (base_url or lloydio_base_url()).rstrip("/")
    token = lloydio_episode_token()
    if not base or not token:
        raise RuntimeError("LLOYDIO_BASE_URL and LLOYDIO_EPISODE_TOKEN must be set")
    audio = article_dir(slug) / meta["audio_filename"]
    fields = {
        "slug": meta.get("lloydio_slug") or slug,
        "title": meta.get("title") or slug,
        "url": meta["source_url"],
        "date": meta.get("published_at") or articles.utcnow_iso(),
        "duration_seconds": str(meta.get("duration_seconds") or 0),
        "markdown": articles.load_body(slug),
    }
    ctype = "audio/mpeg" if audio.suffix == ".mp3" else "audio/wav"
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c, audio.open("rb") as fh:
            r = c.post(f"{base}{EPISODE_PATH}", data=fields,
                       files={"audio": (audio.name, fh, ctype)},
                       headers={"Authorization": f"Bearer {token}"})
            r.raise_for_status()
            res = r.json()
    except (httpx.HTTPError, ValueError) as e:
        articles.set_state(slug, meta["state"], lloydio_error=str(e)[:500])
        raise
    articles.set_state(slug, meta["state"], lloydio_published_at=articles.utcnow_iso(),
                       lloydio_audio_url=res.get("audio_url"), lloydio_error=None)
    log.info("published %s to lloydio (%s)", slug, res.get("audio_url"))
    return res


def publish_pending(transport: httpx.BaseTransport | None = None) -> list[str]:
    """Retry write-back for every published episode not yet on lloydio."""
    if not (lloydio_base_url() and lloydio_episode_token()):
        return []
    done: list[str] = []
    for meta in articles.iter_articles(state="published"):
        if not publishable(meta):
            continue
        try:
            publish_episode(meta["slug"], transport=transport)
            done.append(meta["slug"])
        except Exception:
            log.exception("lloydio write-back failed for %s", meta["slug"])
    return done
