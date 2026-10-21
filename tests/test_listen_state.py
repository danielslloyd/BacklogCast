"""Live listen state for lloydio's /reading: progress reports, cancel, requeue."""
import json

import httpx
import pytest

from app import articles, henty, lloydio, tts


@pytest.fixture(autouse=True)
def writeback(monkeypatch):
    monkeypatch.setenv("LLOYDIO_BASE_URL", "https://lloyd.test")
    monkeypatch.setenv("LLOYDIO_EPISODE_TOKEN", "tok")
    lloydio._cancel_checked.clear()


def _status_server(states, calls):
    """Fake lloydio: /api/episode-status GET ?slug= reads `states`, POST records
    calls; /api/episode records text/audio uploads as ("episode", slug)."""
    def handler(req):
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path == "/api/episode":
            req.read()
            slug = req.content.split(b'name="slug"\r\n\r\n')[1].split(b"\r\n")[0].decode()
            calls.append(("episode", slug))
            return httpx.Response(200, json={"ok": True, "slug": slug})
        if req.method == "POST":
            body = json.loads(req.content)
            if "text" in body:
                calls.append(("text", body["slug"], body["text"]))
            else:
                calls.append((body["slug"], body["state"]))
            return httpx.Response(200, json={"ok": True})
        slug = req.url.params["slug"]
        return httpx.Response(200, json={"slug": slug, "state": states.get(slug)})
    return httpx.MockTransport(handler)


def _route_httpx(monkeypatch, transport):
    real = httpx.Client

    def factory(*a, **k):
        k["transport"] = transport
        return real(*a, **k)
    monkeypatch.setattr(lloydio.httpx, "Client", factory)


def test_report_status_posts_and_never_raises(monkeypatch):
    calls = []
    lloydio.report_status("ep-1", "in_progress", transport=_status_server({}, calls))
    assert calls == [("ep-1", "in_progress")]
    down = httpx.MockTransport(lambda r: httpx.Response(500))
    lloydio.report_status("ep-1", "failed", transport=down)  # logged, not raised
    lloydio.report_status(None, "failed")                     # no lloydio slug: no-op


def test_cancel_requested_is_throttled(monkeypatch):
    hits = []
    states = {"ep-1": "cancelled"}

    def handler(req):
        hits.append(1)
        return httpx.Response(200, json={"state": states.get(req.url.params["slug"])})
    t = httpx.MockTransport(handler)
    assert lloydio.cancel_requested("ep-1", transport=t)
    assert lloydio.cancel_requested("ep-1", transport=t)
    assert len(hits) == 1                     # second call served from the cache
    assert not lloydio.cancel_requested("ep-2", transport=t)


def test_synthesize_project_stops_between_chunks():
    class C:
        def project_info(self):
            return {"chapters": [{"id": "ch", "chunks": [
                {"id": 1, "text": "a"}, {"id": 2, "text": "b"}]}]}
    with pytest.raises(henty.JobCancelled):
        henty.synthesize_project(C(), should_stop=lambda: True)


def _job(state, **extra):
    meta = articles.new_article(title="T", source_url="https://x/a", body="# T\n\nText.\n",
                                raw_html=None, extraction_method="claude")
    articles.set_state(meta["slug"], state, lloydio_slug="t-abc", **extra)
    return meta["slug"]


def test_process_queued_reports_progress_then_cancel(monkeypatch):
    calls, states = [], {}
    _route_httpx(monkeypatch, _status_server(states, calls))
    slug = _job("queued")
    tts.process_queued(slug)
    assert articles.load_meta(slug)["state"] == "approved"
    assert ("t-abc", "in_progress") in calls

    # Cancel: narration stops, the clean text stays and goes to lloydio.
    states["t-abc"] = "cancelled"
    lloydio._cancel_checked.clear()
    tts.synthesize(slug)
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and m["narrate"] is False
    assert ("t-abc", "clear") in calls and ("episode", "t-abc") in calls
    assert m["lloydio_text_published_at"]


def test_text_only_job_skips_narration(monkeypatch):
    calls = []
    _route_httpx(monkeypatch, _status_server({}, calls))
    slug = _job("queued", narrate=False)
    tts.process_queued(slug)
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and m["lloydio_text_published_at"]
    # Text progress is reported; no narration in_progress for text-only work.
    assert calls == [("text", "t-abc", "in_progress"), ("text", "t-abc", "clear"),
                     ("episode", "t-abc")]


def _queue(*items, generated="2026-10-21T13:00:00.000Z"):
    return httpx.MockTransport(lambda r: httpx.Response(
        200, json={"version": 2, "generated": generated, "items": list(items)}))


def _item(needs_text=False, needs_audio=False):
    return {"slug": "t-abc", "url": "https://x/a", "title": "T",
            "needs_text": needs_text, "needs_audio": needs_audio}


def test_sync_creates_text_only_and_narration_jobs():
    a = {**_item(needs_text=True), "url": "https://x/1", "slug": "one"}
    b = {**_item(needs_text=True, needs_audio=True), "url": "https://x/2", "slug": "two"}
    res = lloydio.sync(base_url="https://lloyd.test", transport=_queue(a, b))
    assert len(res["created"]) == 2
    by_url = {m["source_url"]: m for m in articles.iter_articles()}
    assert by_url["https://x/1"]["narrate"] is False
    assert by_url["https://x/2"]["narrate"] is True


def test_sync_queue_and_unqueue_respect_queue_age():
    slug = _job("text_ready", narrate=False, narrate_changed_at="2026-10-21T12:00:00Z")
    # A listing built before the un-queue is stale: ignored.
    stale = _queue(_item(needs_audio=True), generated="2026-10-21T11:00:00.000Z")
    assert lloydio.sync(base_url="https://lloyd.test", transport=stale)["requeued"] == []
    # Queued for narration afterwards: narrate, keeping the text.
    res = lloydio.sync(base_url="https://lloyd.test", transport=_queue(_item(needs_audio=True)))
    m = articles.load_meta(slug)
    assert res["requeued"] == [slug] and m["state"] == "approved" and m["narrate"] is True
    # Un-queued again before narration started, while its text is still
    # listed as pending (a later listing): text-only. (Once the text exists the
    # item leaves the queue; then the R2 `cancelled` status un-queues it.)
    later = _queue(_item(needs_text=True), generated="2026-10-21T23:00:00.000Z")
    lloydio.sync(base_url="https://lloyd.test", transport=later)
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and m["narrate"] is False


def test_unqueue_via_r2_status_stops_before_narration(monkeypatch):
    calls = []
    _route_httpx(monkeypatch, _status_server({"t-abc": "cancelled"}, calls))
    slug = _job("approved", narrate=True)
    tts.synthesize(slug)
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and m["narrate"] is False
    assert ("t-abc", "clear") in calls


def test_sync_reuploads_deleted_episode_only_after_rebuild():
    slug = _job("published", audio_filename="audio.mp3",
                lloydio_published_at="2026-10-21T12:00:00Z")
    stale = _queue(_item(needs_audio=True), generated="2026-10-21T11:00:00.000Z")
    assert lloydio.sync(base_url="https://lloyd.test", transport=stale)["requeued"] == []
    assert articles.load_meta(slug)["lloydio_published_at"]
    res = lloydio.sync(base_url="https://lloyd.test", transport=_queue(_item(needs_audio=True)))
    m = articles.load_meta(slug)
    assert res["requeued"] == [slug] and m["lloydio_published_at"] is None
    assert lloydio.publishable(m)


def test_sync_moves_legacy_cancelled_to_text_ready():
    slug = _job("cancelled")
    lloydio.sync(base_url="https://lloyd.test", transport=_queue(_item(needs_text=False)))
    # Not listed for text or audio -> skipped; listed for text -> migrated.
    lloydio.sync(base_url="https://lloyd.test", transport=_queue(_item(needs_text=True)))
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and m["narrate"] is False


def test_sync_leaves_failed_jobs_alone():
    _job("failed")
    res = lloydio.sync(base_url="https://lloyd.test", transport=_queue(_item(needs_audio=True)))
    assert res["requeued"] == [] and res["skipped"] == ["https://x/a"]


def test_removed_on_lloydio_drops_the_job(monkeypatch):
    calls = []
    _route_httpx(monkeypatch, _status_server({"t-abc": "removed"}, calls))
    slug = _job("queued", narrate=False)
    tts.process_queued(slug)
    m = articles.load_meta(slug)
    assert m["state"] == "removed" and m["removed_at"]
    assert not any(c[0] == "episode" for c in calls)  # never written back


def test_episode_410_marks_removed(monkeypatch):
    def handler(req):
        if req.url.path == "/api/episode":
            return httpx.Response(410, json={"error": "removed", "removed": True})
        if req.method == "POST":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"state": None})
    _route_httpx(monkeypatch, httpx.MockTransport(handler))
    slug = _job("text_ready", narrate=False)
    assert lloydio.publish_pending() == []
    assert articles.load_meta(slug)["state"] == "removed"


def test_readded_link_revives_removed_job():
    slug = _job("removed", removed_at="2026-10-21T12:00:00Z", narrate=False)
    stale = _queue(_item(needs_text=True), generated="2026-10-21T11:00:00.000Z")
    lloydio.sync(transport=stale)
    assert articles.load_meta(slug)["state"] == "removed"  # listing predates removal
    lloydio.sync(transport=_queue(_item(needs_text=True)))  # built 13:00, after
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and not m.get("removed_at")
