"""App smoke tests: the FastAPI app builds and core endpoints work.

Uses TestClient without the lifespan context manager, so no background worker /
poller threads start during the test.
"""
import json

from fastapi.testclient import TestClient

from app import paths
from app.main import app

client = TestClient(app)


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
