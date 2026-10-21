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
    """Fake /api/episode-status: GET ?slug= reads `states`, POST records calls."""
    def handler(req):
        assert req.headers["authorization"] == "Bearer tok"
        if req.method == "POST":
            body = json.loads(req.content)
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

    states["t-abc"] = "cancelled"
    lloydio._cancel_checked.clear()
    tts.synthesize(slug)
    assert articles.load_meta(slug)["state"] == "cancelled"
    assert calls[-1] == ("t-abc", "clear")


def test_sync_requeues_cancelled_and_reuploads_deleted():
    cancelled = _job("cancelled")
    item = {"slug": "t-abc", "url": "https://x/a", "title": "T", "status": "queued", "has_audio": False}
    q = httpx.MockTransport(lambda r: httpx.Response(200, json={"items": [item]}))
    res = lloydio.sync(base_url="https://lloyd.test", transport=q)
    assert res["requeued"] == [cancelled]
    assert articles.load_meta(cancelled)["state"] == "queued"

    articles.set_state(cancelled, "published", audio_filename="audio.mp3",
                       lloydio_published_at="2026-10-21T00:00:00Z")
    res = lloydio.sync(base_url="https://lloyd.test", transport=q)
    m = articles.load_meta(cancelled)
    assert res["requeued"] == [cancelled] and m["lloydio_published_at"] is None
    assert lloydio.publishable(m)


def test_sync_leaves_failed_jobs_alone():
    _job("failed")
    item = {"slug": "t-abc", "url": "https://x/a", "status": "queued", "has_audio": False}
    q = httpx.MockTransport(lambda r: httpx.Response(200, json={"items": [item]}))
    res = lloydio.sync(base_url="https://lloyd.test", transport=q)
    assert res["requeued"] == [] and res["skipped"] == ["https://x/a"]
