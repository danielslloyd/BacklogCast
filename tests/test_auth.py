"""Auth middleware: the admin surface is gated, the podcast surface is public."""
from fastapi.testclient import TestClient

from app.main import app

# Built without the lifespan context manager on purpose, so the TTS worker and
# lloydio poller threads never start during the test.
client = TestClient(app)
PW = "test-secret-pw"


def test_api_requires_auth(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    r = client.get("/api/config")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").startswith("Basic")


def test_api_rejects_wrong_password(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    assert client.get("/api/config", auth=("admin", "nope")).status_code == 401


def test_api_accepts_correct_password(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    r = client.get("/api/config", auth=("admin", PW))
    assert r.status_code == 200
    assert "podcast_title" in r.json()


def test_admin_ui_and_static_are_gated(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    assert client.get("/").status_code == 401
    assert client.get("/static/app.js").status_code == 401


def test_feed_is_public(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    # No credentials + a bogus token: the middleware must pass it through to the
    # handler (which 404s the unknown token) rather than short-circuit with 401.
    assert client.get("/feed/definitely-not-a-real-token.xml").status_code == 404


def test_audio_is_public(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    assert client.get("/audio/definitely-not-a-real-token/x.mp3").status_code == 404


def test_cover_is_public(monkeypatch):
    monkeypatch.setenv("BACKLOGCAST_PASSWORD", PW)
    # Public path: no cover in the test data dir → 404, but crucially never 401.
    assert client.get("/cover.jpg").status_code != 401
