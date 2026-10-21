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


def wants(item: dict[str, Any]) -> tuple[bool, bool]:
    """(needs_text, needs_audio) for a queue item. Queue v2 says so directly;
    a v1 queue only listed narration work."""
    if not item.get("url"):
        return False, False
    if "needs_text" in item or "needs_audio" in item:
        return bool(item.get("needs_text")), bool(item.get("needs_audio"))
    narrated = bool(item.get("has_audio"))
    return not (item.get("article_exists") or narrated), not narrated


def needs_narration(item: dict[str, Any]) -> bool:
    """True if this queue item should be narrated."""
    return wants(item)[1]


def sync(
    base_url: str | None = None,
    create: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, list[str]]:
    """Poll the queue and create or update local jobs.

    Every listed link gets a job, so its clean text is produced; only those
    with needs_audio are narrated (`narrate` on the job). Existing jobs follow
    the owner's queue/un-queue changes, judged against the queue's build time
    so a listing that hasn't caught up with a rebuild is ignored.

    Returns {"created": [slugs], "requeued": [slugs], "skipped": [urls]}.
    Deduped by source_url so re-polling is idempotent. `create=False` does a
    dry run.
    """
    created: list[str] = []
    requeued: list[str] = []
    skipped: list[str] = []
    for item in fetch_queue(base_url, transport=transport):
        url = item.get("url") or ""
        needs_text, needs_audio = wants(item)
        if not (needs_text or needs_audio):
            skipped.append(url or item.get("slug") or "?")
            continue
        existing = articles.find_by_source_url(url)
        if existing:
            if create and _update(existing, item, needs_audio):
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
            narrate=needs_audio,
        )
        created.append(meta["slug"])
        log.info("queued from lloydio: %s (%s)%s", meta["slug"], url,
                 " for narration" if needs_audio else "")
    log.info("lloydio sync: +%d new, %d updated, %d skipped",
             len(created), len(requeued), len(skipped))
    return {"created": created, "requeued": requeued, "skipped": skipped}


def _ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def _newer(item: dict[str, Any], than: str | None) -> bool:
    """Was this queue listing built after `than`? (True if `than` is unset.)"""
    if not than:
        return True
    return bool(item.get("_generated")) and _ts(item["_generated"]) > _ts(than)


def narrating(meta: dict[str, Any]) -> bool:
    # Jobs from before text-only processing existed were all narration jobs.
    return bool(meta.get("narrate", True))


def _update(meta: dict[str, Any], item: dict[str, Any], needs_audio: bool) -> bool:
    """Bring an existing job in line with the queue. Returns True if changed."""
    slug, lslug = meta["slug"], item.get("slug") or meta.get("lloydio_slug")
    state, has_body = meta.get("state"), bool(articles.load_body(slug).strip())

    if state == "removed":
        if not _newer(item, meta.get("removed_at")):
            return False
        # Re-added to the reading list after removal: start over from the text.
        has_body = bool(articles.load_body(slug).strip())
        articles.set_state(slug, "text_ready" if has_body else "queued", lloydio_slug=lslug,
                           narrate=False, removed_at=None, lloydio_text_published_at=None,
                           lloydio_published_at=None, extract_attempts=0, synth_attempts=0,
                           narrate_changed_at=articles.utcnow_iso())
        log.info("%s is back on the reading list", slug)
        meta = articles.load_meta(slug)
        state = meta["state"]
        if has_body:
            publish_text_safely(slug)

    if state == "cancelled":
        # Older cancel state: park as text-only (re-queuing below can lift it).
        state = "text_ready" if has_body else "queued"
        articles.set_state(slug, state, narrate=False, lloydio_slug=lslug,
                           narrate_changed_at=meta.get("narrate_changed_at") or articles.utcnow_iso())
        meta = articles.load_meta(slug)

    if needs_audio and not narrating(meta) and _newer(item, meta.get("narrate_changed_at")):
        # Queued for listening (again): narrate, keeping any extracted text.
        nxt = "approved" if state in ("text_ready", "book_built") else state
        articles.set_state(slug, nxt, narrate=True, lloydio_slug=lslug, error=None,
                           synth_attempts=0, retry_after=None,
                           narrate_changed_at=articles.utcnow_iso(),
                           approved_at=articles.utcnow_iso())
        _cancel_checked.pop(lslug, None)
        log.info("queued %s for narration", slug)
        return True

    uploaded = meta.get("lloydio_published_at")
    if needs_audio and state == "published" and uploaded and _newer(item, uploaded):
        # Episode deleted on lloydio, then queued again: re-upload our audio.
        articles.set_state(slug, "published", lloydio_published_at=None, lloydio_slug=lslug)
        log.info("will re-upload %s (episode deleted on lloydio, re-queued)", slug)
        return True

    if (not needs_audio and narrating(meta) and state in ("queued", "approved", "book_built")
            and _newer(item, meta.get("narrate_changed_at"))):
        # Un-queued before narration started: keep it text-only.
        nxt = "text_ready" if state in ("approved", "book_built") else state
        articles.set_state(slug, nxt, narrate=False, lloydio_slug=lslug,
                           narrate_changed_at=articles.utcnow_iso())
        log.info("un-queued %s from narration", slug)
        return True
    return state != meta.get("state")


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


def report_text_status(
    lloydio_slug: str | None, state: str, reason: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Tell /reading where this link's clean text stands, so a missing "md"
    link reads as in progress | waiting (retry scheduled) | failed, or `clear`.
    Best effort, like report_status."""
    if not lloydio_slug or not _writeback_ready():
        return
    body: dict[str, Any] = {"slug": lloydio_slug, "text": state}
    if reason:
        body["reason"] = reason[:300]
    try:
        with httpx.Client(timeout=15.0, transport=transport) as c:
            c.post(f"{lloydio_base_url()}{STATUS_PATH}", json=body,
                   headers={"Authorization": f"Bearer {lloydio_episode_token()}"}
                   ).raise_for_status()
    except httpx.HTTPError as e:
        log.warning("text status report %s=%s failed: %s", lloydio_slug, state, e)


def remote_state(
    lloydio_slug: str | None, transport: httpx.BaseTransport | None = None,
) -> str | None:
    """The owner's live state for this item on /reading (`cancelled`,
    `removed`, ...), or None. Checked at most every CANCEL_CHECK_SECONDS per
    slug; network errors read as None."""
    if not lloydio_slug or not _writeback_ready():
        return None
    now = time.monotonic()
    last = _cancel_checked.get(lloydio_slug)
    if last and now - last[0] < CANCEL_CHECK_SECONDS:
        return last[1]
    state = None
    try:
        with httpx.Client(timeout=10.0, transport=transport) as c:
            r = c.get(f"{lloydio_base_url()}{STATUS_PATH}", params={"slug": lloydio_slug},
                      headers={"Authorization": f"Bearer {lloydio_episode_token()}"})
            r.raise_for_status()
            state = r.json().get("state")
    except (httpx.HTTPError, ValueError) as e:
        log.warning("status check for %s failed: %s", lloydio_slug, e)
    _cancel_checked[lloydio_slug] = (now, state)
    return state


def cancel_requested(
    lloydio_slug: str | None, transport: httpx.BaseTransport | None = None,
) -> bool:
    """True when the owner cancelled (or removed) this item on /reading."""
    return remote_state(lloydio_slug, transport) in ("cancelled", "removed")


def removed(lloydio_slug: str | None, transport: httpx.BaseTransport | None = None) -> bool:
    """True when the owner took this link off the reading list."""
    return remote_state(lloydio_slug, transport) == "removed"


class Removed(RuntimeError):
    """lloydio refused a write-back: the link is off the reading list."""


def mark_removed(slug: str) -> None:
    articles.set_state(slug, "removed", removed_at=articles.utcnow_iso(),
                       retry_after=None, error=None)
    log.info("%s was removed from lloydio's reading list; dropping it", slug)


def publishable(meta: dict[str, Any]) -> bool:
    """Locally published, has a source URL (lloydio's schema requires one) and
    an audio file, and hasn't been written back yet."""
    return (meta.get("state") == "published" and bool(meta.get("source_url"))
            and bool(meta.get("audio_filename")) and not meta.get("lloydio_published_at"))


def text_publishable(meta: dict[str, Any]) -> bool:
    """Clean text extracted for a text-only job and not yet on lloydio."""
    return (meta.get("state") == "text_ready" and bool(meta.get("source_url"))
            and not meta.get("lloydio_text_published_at"))


def _post_episode(slug: str, with_audio: bool, base_url: str | None,
                  timeout: float, transport: httpx.BaseTransport | None) -> dict[str, Any]:
    meta = articles.load_meta(slug)
    base = (base_url or lloydio_base_url()).rstrip("/")
    token = lloydio_episode_token()
    if not base or not token:
        raise RuntimeError("LLOYDIO_BASE_URL and LLOYDIO_EPISODE_TOKEN must be set")
    fields = {
        "slug": meta.get("lloydio_slug") or slug,
        "title": meta.get("title") or slug,
        "url": meta["source_url"],
        "date": meta.get("published_at") or meta.get("fetched_at") or articles.utcnow_iso(),
        "markdown": articles.load_body(slug),
        "author": meta.get("author") or "",
        "source": meta.get("publication") or "",
        "published": meta.get("published") or "",
    }
    files = None
    if with_audio:
        audio = article_dir(slug) / meta["audio_filename"]
        fields["duration_seconds"] = str(meta.get("duration_seconds") or 0)
        ctype = "audio/mpeg" if audio.suffix == ".mp3" else "audio/wav"
        # Send the file as bytes so the request has a Content-Length: a chunked
        # upload reaches the Worker as a stream of unknown length, which R2's
        # put() rejects (HTTP 500).
        files = {"audio": (audio.name, audio.read_bytes(), ctype)}
    else:
        # Still multipart: the endpoint reads formData either way.
        files = {"_": ("", b"", "application/octet-stream")}
    try:
        with httpx.Client(timeout=timeout, transport=transport) as c:
            r = c.post(f"{base}{EPISODE_PATH}", data=fields, files=files,
                       headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 410:
                mark_removed(slug)
                raise Removed(f"{slug} is no longer on the reading list")
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"{r.status_code} from /api/episode: {r.text[:300]}",
                    request=r.request, response=r)
            return r.json()
    except (httpx.HTTPError, ValueError) as e:
        articles.set_state(slug, meta["state"], lloydio_error=str(e)[:500])
        raise


def publish_episode(
    slug: str,
    base_url: str | None = None,
    timeout: float = 300.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Upload one finished episode (text + audio) to lloydio. Records
    lloydio_published_at on success, or lloydio_error on failure (the job
    itself stays published)."""
    res = _post_episode(slug, True, base_url, timeout, transport)
    now = articles.utcnow_iso()
    state = articles.load_meta(slug)["state"]
    articles.set_state(slug, state, lloydio_published_at=now, lloydio_text_published_at=now,
                       lloydio_audio_url=res.get("audio_url"), lloydio_error=None)
    log.info("published %s to lloydio (%s)", slug, res.get("audio_url"))
    return res


def publish_text(
    slug: str,
    base_url: str | None = None,
    timeout: float = 60.0,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Upload a text-only job's clean text, giving /reading its "md" link."""
    res = _post_episode(slug, False, base_url, timeout, transport)
    state = articles.load_meta(slug)["state"]
    articles.set_state(slug, state, lloydio_text_published_at=articles.utcnow_iso(),
                       lloydio_error=None)
    log.info("published text for %s to lloydio", slug)
    return res


def publish_text_safely(slug: str) -> None:
    try:
        publish_text(slug)
    except Exception:
        log.exception("lloydio text write-back failed for %s", slug)  # publish_pending retries


def publish_pending(transport: httpx.BaseTransport | None = None) -> list[str]:
    """Retry write-back for every episode or text-only job not yet on lloydio."""
    if not (lloydio_base_url() and lloydio_episode_token()):
        return []
    done: list[str] = []
    for meta in articles.iter_articles():
        try:
            if publishable(meta):
                publish_episode(meta["slug"], transport=transport)
            elif text_publishable(meta):
                publish_text(meta["slug"], transport=transport)
            else:
                continue
            done.append(meta["slug"])
        except Removed:
            pass
        except Exception:
            log.exception("lloydio write-back failed for %s", meta["slug"])
    return done
