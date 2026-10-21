"""App smoke tests: the FastAPI app builds and core endpoints work.

Uses TestClient without the lifespan context manager, so no background worker /
poller threads start during the test.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import paths
from app.main import app

PW = "test-secret-pw"
client = TestClient(app)
client.auth = ("admin", PW)  # /api/* sits behind HTTP Basic auth


@pytest.fixture(autouse=True)
def admin_password(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)


def test_config_exposes_new_keys():
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    for k in ("henty_base_url", "asr_similarity_threshold", "lloydio_base_url",
              "default_voice", "auto_approve"):
        assert k in body


def test_config_roundtrip_new_keys():
    r = client.put("/api/config", json={"henty_books_dir": "/tmp/books", "asr_max_retries": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["henty_books_dir"] == "/tmp/books"
    assert body["asr_max_retries"] == 3


def test_feed_renders_for_valid_token():
    tok = list(json.loads(paths.TOKENS_PATH.read_text()).values())[0]
    r = client.get(f"/feed/{tok}.xml")
    assert r.status_code == 200
    assert "<rss" in r.text
    assert "<itunes:owner>" in r.text


def test_feed_rejects_bad_token():
    assert client.get("/feed/not-a-real-token.xml").status_code == 404


def test_add_text_article_creates_queued_job():
    r = client.post("/api/articles", json={"text": "Hello world. This is a test.", "title": "T"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "queued"
    assert body["slug"]


def test_queues_lists_work_in_worker_order(monkeypatch):
    from app import articles, henty, main, tts
    monkeypatch.setattr(henty, "is_available", lambda *a, **k: False)
    main._henty_seen = (0.0, False)
    mk = lambda t, st, **kw: articles.set_state(  # noqa: E731
        articles.new_article(title=t, source_url=f"https://x/{t}", body="",
                             raw_html=None, extraction_method="lloydio-queue")["slug"], st, **kw)
    mk("later", "queued", retry_after="2999-01-01T00:00:00Z", extract_attempts=1)
    mk("now", "queued")
    mk("narrate", "approved", approved_at="2026-01-01T00:00:00Z")
    mk("broke", "failed", error="extract: boom")
    running = mk("busy", "synthesizing")
    tts.set_progress(running["slug"], "synth", "narrating", 3, 10)
    try:
        q = client.get("/api/queues").json()
    finally:
        tts.clear_progress(running["slug"])
    assert [m["title"] for m in q["extraction"]["waiting"]] == ["now", "later"]
    assert [m["title"] for m in q["extraction"]["failed"]] == ["broke"]
    assert [m["title"] for m in q["synthesis"]["waiting"]] == ["narrate"]
    assert q["synthesis"]["running"][0]["title"] == "busy"
    assert q["active"]["done"] == 3 and q["active"]["total"] == 10
    assert q["henty_up"] is False
