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


def test_board_buckets_articles_in_worker_order(monkeypatch):
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
    mk("read", "text_ready")
    running = mk("busy", "synthesizing")
    tts.set_progress(running["slug"], "synth", "narrating", 3, 10)
    try:
        b = client.get("/api/board").json()
    finally:
        tts.clear_progress(running["slug"])
    cols = {c["key"]: c for c in b["columns"]}
    titles = lambda k: [m["title"] for m in cols[k]["items"]]  # noqa: E731
    # Backoff waits its turn, and the running job heads its column.
    assert titles("intake") == ["now", "later"]
    assert [m["queue_pos"] for m in cols["intake"]["items"]] == [1, 2]
    assert cols["intake"]["items"][1]["due"] is False
    assert titles("narration") == ["busy", "narrate"]
    assert cols["narration"]["items"][0]["running"] is True
    assert titles("text") == ["read"]
    assert titles("attention") == ["broke"]
    assert b["active"]["done"] == 3 and b["active"]["total"] == 10
    assert b["henty_up"] is False
    # Henty being down is why narration is idle, and the board says so.
    assert "offline" in cols["narration"]["note"]


def test_board_covers_every_state():
    from app import articles
    from app.main import BOARD_COLUMNS
    mapped = {s for c in BOARD_COLUMNS for s in c["states"]}
    assert articles.STATES - mapped == set(), "a state no column shows"


def test_board_rows_omit_chunk_reports():
    """The board polls every couple of seconds; chunk_reports is far too big."""
    from app import articles
    slug = articles.new_article(title="big", source_url=None, body="hi", raw_html=None,
                                extraction_method="manual")["slug"]
    articles.set_state(slug, "stitched", chunk_reports=[{"i": i} for i in range(200)])
    b = client.get("/api/board").json()
    row = next(m for c in b["columns"] for m in c["items"] if m["slug"] == slug)
    assert "chunk_reports" not in row


def test_index_is_never_cached_and_stamps_its_assets():
    """A stale shell hides every UI change; the assets it names must bust too."""
    r = client.get("/")
    assert r.headers["cache-control"] == "no-store"
    assert "/static/app.js?v=" in r.text and "/static/style.css?v=" in r.text
