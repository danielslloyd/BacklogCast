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
import time
from datetime import datetime
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
    generated = data.get("generated") if isinstance(data, dict) else None
    # Each item carries the queue's build time, so sync can tell a stale
    # listing (built before our upload landed) from a genuine re-queue.
    return [{**it, "_generated": generated} for it in items if isinstance(it, dict)]


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

    Returns {"created": [slugs], "requeued": [slugs], "skipped": [urls]}.
    Deduped by source_url so re-polling is idempotent. An item that is back in
    the queue after the owner cancelled it resumes; one whose episode was
    deleted on lloydio gets its existing audio re-uploaded (publish_pending).
    `create=False` does a dry run.
    """
    created: list[str] = []
    requeued: list[str] = []
    skipped: list[str] = []
    for item in fetch_queue(base_url, transport=transport):
        url = item.get("url") or ""
        if not needs_narration(item):
            skipped.append(url or item.get("slug") or "?")
            continue
        existing = articles.find_by_source_url(url)
        if existing:
            if create and _revive(existing, item):
                requeued.append(existing["slug"])
            else:
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
    log.info("lloydio sync: +%d new, %d requeued, %d skipped",
             len(created), len(requeued), len(skipped))
    return {"created": created, "requeued": requeued, "skipped": skipped}


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _revive(meta: dict[str, Any], item: dict[str, Any]) -> bool:
    """Re-activate a local job whose URL is queued again on lloydio."""
    slug, lslug = meta["slug"], item.get("slug") or meta.get("lloydio_slug")
    if meta.get("state") == "cancelled":
        # Keep any extracted text: an empty body re-extracts, a full one just
        # rebuilds book.json.
        articles.set_state(slug, "queued", error=None, lloydio_slug=lslug)
        _cancel_checked.pop(lslug, None)
        log.info("requeued %s (re-added after cancel)", slug)
        return True
    uploaded = meta.get("lloydio_published_at")
    if meta.get("state") == "published" and uploaded:
        # Only a queue built *after* our upload means it was deleted and queued
        # again; an older one just hasn't caught up with the rebuild yet.
        if not item.get("_generated") or _ts(item["_generated"]) <= _ts(uploaded):
            return False
        # Deleted on lloydio, then queued again: re-upload the audio we have.
        articles.set_state(slug, "published", lloydio_published_at=None, lloydio_slug=lslug)
        log.info("will re-upload %s (episode deleted on lloydio, re-queued)", slug)
        return True
    return False


EPISODE_PATH = "/api/episode"
STATUS_PATH = "/api/episode-status"
CANCEL_CHECK_SECONDS = 20.0
_cancel_checked: dict[str, tuple[float, bool]] = {}


def _writeback_ready() -> bool:
    return bool(lloydio_base_url() and lloydio_episode_token())


def report_status(
    lloydio_slug: str | None, state: str,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Tell lloydio's /reading page what this job is doing (in_progress |
    failed | cancelled | clear). Best effort: never fails the job."""
    if not lloydio_slug or not _writeback_ready():
        return
    try:
        with httpx.Client(timeout=15.0, transport=transport) as c:
            c.post(f"{lloydio_base_url()}{STATUS_PATH}",
                   json={"slug": lloydio_slug, "state": state},
                   headers={"Authorization": f"Bearer {lloydio_episode_token()}"}
                   ).raise_for_status()
    except httpx.HTTPError as e:
        log.warning("status report %s=%s failed: %s", lloydio_slug, state, e)


def cancel_requested(
    lloydio_slug: str | None, transport: httpx.BaseTransport | None = None,
) -> bool:
    """True when the owner cancelled this item on /reading. Checked at most
    every CANCEL_CHECK_SECONDS per slug; network errors read as 'not cancelled'."""
    if not lloydio_slug or not _writeback_ready():
        return False
    now = time.monotonic()
    last = _cancel_checked.get(lloydio_slug)
    if last and now - last[0] < CANCEL_CHECK_SECONDS:
        return last[1]
    cancelled = False
    try:
        with httpx.Client(timeout=10.0, transport=transport) as c:
            r = c.get(f"{lloydio_base_url()}{STATUS_PATH}", params={"slug": lloydio_slug},
                      headers={"Authorization": f"Bearer {lloydio_episode_token()}"})
            r.raise_for_status()
            cancelled = r.json().get("state") == "cancelled"
    except (httpx.HTTPError, ValueError) as e:
        log.warning("cancel check for %s failed: %s", lloydio_slug, e)
    _cancel_checked[lloydio_slug] = (now, cancelled)
    return cancelled


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
        # Send the file as bytes so the request has a Content-Length: a chunked
        # upload reaches the Worker as a stream of unknown length, which R2's
        # put() rejects (HTTP 500).
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(f"{base}{EPISODE_PATH}", data=fields,
                       files={"audio": (audio.name, audio.read_bytes(), ctype)},
                       headers={"Authorization": f"Bearer {token}"})
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{r.status_code} from /api/episode: {r.text[:300]}",
                    request=r.request, response=r)
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
