"""Stage A: lloydio queue client — verified with a mocked HTTP transport."""
import httpx

from app import articles, lloydio


def _transport(payload):
    return httpx.MockTransport(lambda req: httpx.Response(200, json=payload))


def test_needs_narration_rules():
    assert lloydio.needs_narration({"url": "u", "has_audio": False, "status": "queued"})
    assert not lloydio.needs_narration({"url": "u", "has_audio": True})
    assert not lloydio.needs_narration({"has_audio": False})  # missing url


def test_sync_creates_jobs_and_filters():
    payload = {"items": [
        {"slug": "a", "url": "http://x/1", "title": "One",
         "tags": ["podcast"], "status": "queued", "has_audio": False},
        {"slug": "b", "url": "http://x/2", "title": "Two",
         "status": "queued", "has_audio": True},              # already has audio -> skip
        {"slug": "c", "title": "NoURL", "status": "queued", "has_audio": False},  # no url -> skip
    ]}
    res = lloydio.sync(base_url="http://lloydio.test", transport=_transport(payload))
    assert res["created"] and len(res["created"]) == 1
    metas = list(articles.iter_articles())
    assert len(metas) == 1
    m = metas[0]
    assert m["source_url"] == "http://x/1"
    assert m["state"] == "queued"
    assert m["extraction_method"] == "lloydio-queue"
    assert m.get("tags") == ["podcast"]


def test_sync_is_idempotent():
    payload = {"items": [
        {"slug": "a", "url": "http://x/1", "title": "One", "status": "queued", "has_audio": False},
    ]}
    r1 = lloydio.sync(base_url="http://l.test", transport=_transport(payload))
    r2 = lloydio.sync(base_url="http://l.test", transport=_transport(payload))
    assert len(r1["created"]) == 1
    assert len(r2["created"]) == 0
    assert r2["skipped"] == ["http://x/1"]
    assert len(list(articles.iter_articles())) == 1
