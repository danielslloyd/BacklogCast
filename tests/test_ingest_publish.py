"""Fetch hardening, Claude cleanup pass, and lloydio write-back (all mocked)."""
import json
import subprocess
from pathlib import Path

import httpx
import pytest

from app import articles, claude_extract, extractor, lloydio, tts


# --- fetch -----------------------------------------------------------------

def test_clean_url_strips_tracking_and_fragment():
    u = ("https://x.substack.com/p/post?selection=abc&utm_campaign=posts&"
         "triedRedirect=true&page=2#:~:text=I%20told")
    assert extractor.clean_url(u) == "https://x.substack.com/p/post?page=2"


def _mock_get(monkeypatch, handler, curl=None):
    real = httpx.Client

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)
    monkeypatch.setattr(extractor.httpx, "Client", factory)
    monkeypatch.setattr(extractor, "_curl_get", curl or (lambda url, timeout: None))


def test_fetch_falls_back_to_curl_when_httpx_is_refused(monkeypatch):
    curl_calls = []

    def curl(url, timeout):
        curl_calls.append(url)
        return extractor._Resp(200, "text/html; charset=utf-8", url,
                               b"<html>via curl</html>", "<html>via curl</html>")
    _mock_get(monkeypatch, lambda req: httpx.Response(403), curl=curl)
    f = extractor.fetch("https://pub.substack.com/p/x")
    assert f.text == "<html>via curl</html>" and curl_calls == ["https://pub.substack.com/p/x"]


def test_fetch_raises_when_every_route_fails(monkeypatch):
    _mock_get(monkeypatch, lambda req: httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        extractor.fetch("https://example.org/gone")


def test_fetch_detects_pdf_by_magic_bytes(monkeypatch):
    _mock_get(monkeypatch, lambda req: httpx.Response(
        200, content=b"%PDF-1.4 ...", headers={"content-type": "application/octet-stream"}))
    f = extractor.fetch("https://example.org/paper")
    assert f.kind == "pdf" and f.content.startswith(b"%PDF-")


def test_fetch_sends_browser_headers_and_retries_403(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(403)
        return httpx.Response(200, text="<html>ok</html>",
                              headers={"content-type": "text/html"})
    _mock_get(monkeypatch, handler)
    f = extractor.fetch("https://example.org/a?utm_source=x")
    assert f.kind == "html" and "ok" in f.text
    assert len(seen) == 2
    assert "Windows NT" in seen[0].headers["user-agent"]
    assert seen[0].headers["accept-language"].startswith("en-US")
    assert "utm_source" not in str(seen[0].url)


def test_fetch_falls_back_to_substack_api(monkeypatch):
    def handler(req):
        if req.url.path.startswith("/api/v1/posts/"):
            return httpx.Response(200, json={"title": "Post", "body_html": "<p>Body.</p>"})
        return httpx.Response(403)
    _mock_get(monkeypatch, handler)
    f = extractor.fetch("https://pub.substack.com/p/my-post")
    assert f.kind == "html"
    assert "<h1>Post</h1>" in f.text and "<p>Body.</p>" in f.text


# --- Claude cleanup --------------------------------------------------------

def _fake_claude(monkeypatch, write_text=None, returncode=0, timeout=False, result=None):
    monkeypatch.setattr(claude_extract, "claude_bin", lambda: "claude")
    calls = []

    def run(cmd, cwd, **k):
        calls.append((cmd, Path(cwd)))
        if timeout:
            raise subprocess.TimeoutExpired(cmd, k.get("timeout"))
        assert (Path(cwd) / "instructions.md").exists()
        if write_text is not None:
            (Path(cwd) / "clean.md").write_text(write_text, encoding="utf-8")
        out = result or {"is_error": False, "result": "done", "total_cost_usd": 0.05}
        return subprocess.CompletedProcess(cmd, returncode, json.dumps(out), "")
    monkeypatch.setattr(claude_extract.subprocess, "run", run)
    return calls


def test_claude_cleanup_ok(monkeypatch, tmp_path):
    src = tmp_path / "raw.html"
    src.write_text("<html/>")
    calls = _fake_claude(monkeypatch, write_text="# Title\n\nBody text.\n")
    r = claude_extract.clean_with_claude(src, "html", "draft")
    assert r.verdict == "ok" and r.markdown.startswith("# Title")
    assert r.cost_usd == 0.05
    cmd = calls[0][0]
    assert "Read,Write" in cmd and "WebFetch" in ",".join(cmd)  # web tools denied


def test_claude_cleanup_not_article(monkeypatch, tmp_path):
    src = tmp_path / "raw.html"
    src.write_text("<html/>")
    _fake_claude(monkeypatch, write_text="NOT_ARTICLE: paywall teaser only")
    r = claude_extract.clean_with_claude(src, "html", "draft")
    assert r.verdict == "not_article" and "paywall" in r.reason


def test_claude_cleanup_timeout_and_missing_output(monkeypatch, tmp_path):
    src = tmp_path / "source.pdf"
    src.write_bytes(b"%PDF-")
    _fake_claude(monkeypatch, timeout=True)
    with pytest.raises(claude_extract.ClaudeExtractError, match="timed out"):
        claude_extract.clean_with_claude(src, "pdf", "")
    _fake_claude(monkeypatch, write_text=None)
    with pytest.raises(claude_extract.ClaudeExtractError, match="clean.md"):
        claude_extract.clean_with_claude(src, "pdf", "")


def test_worker_routes_pdf_through_claude(monkeypatch):
    monkeypatch.setenv("CLAUDE_EXTRACT", "always")
    monkeypatch.setattr(extractor, "fetch", lambda url, **k: extractor.Fetched(
        "pdf", b"%PDF-1.4", url))
    _fake_claude(monkeypatch, write_text="# The Lost Tools\n\n" + "Prose. " * 50)
    meta = articles.new_article(title="https://x/p.pdf", source_url="https://x/p.pdf",
                                body="", raw_html=None, extraction_method="pending")
    tts.process_queued(meta["slug"])
    m = articles.load_meta(meta["slug"])
    assert m["state"] == "approved"
    assert m["title"] == "The Lost Tools" and m["extraction_method"] == "claude"
    assert articles.source_pdf_path(meta["slug"]).exists()


def test_worker_fails_not_article_without_retry(monkeypatch):
    monkeypatch.setenv("CLAUDE_EXTRACT", "always")
    monkeypatch.setattr(extractor, "fetch", lambda url, **k: extractor.Fetched(
        "html", b"", url, "<html/>"))
    monkeypatch.setattr(extractor, "extract", lambda html, url=None: {
        "body": "Subscribe to read. " * 40, "title": "T", "author": "",
        "method": "trafilatura", "confidence_low": False})
    _fake_claude(monkeypatch, write_text="NOT_ARTICLE: paywall")
    meta = articles.new_article(title="T", source_url="https://x/a", body="",
                                raw_html=None, extraction_method="pending")
    tts.process_queued(meta["slug"])
    m = articles.load_meta(meta["slug"])
    assert m["state"] == "failed" and "paywall" in m["error"]
    assert m["extract_attempts"] == 1 and not m["retry_after"]  # permanent: no retry


# --- lloydio write-back ----------------------------------------------------

def _published(slug_title="Ep"):
    meta = articles.new_article(title=slug_title, source_url="https://x/a",
                                body="# Ep\n\nText.\n", raw_html=None,
                                extraction_method="claude")
    slug = meta["slug"]
    (articles.article_dir(slug) / "audio.mp3").write_bytes(b"ID3fake")
    articles.set_state(slug, "published", audio_filename="audio.mp3",
                       duration_seconds=42, published_at="2026-10-21T12:00:00Z",
                       lloydio_slug="ep-abc123")
    return slug


def test_publish_episode_posts_multipart(monkeypatch):
    monkeypatch.setenv("LLOYDIO_BASE_URL", "https://lloyd.test")
    monkeypatch.setenv("LLOYDIO_EPISODE_TOKEN", "tok")
    slug = _published()
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        seen["body"] = req.read()
        return httpx.Response(200, json={"ok": True, "audio_url": "https://media/ep.mp3"})
    lloydio.publish_episode(slug, transport=httpx.MockTransport(handler))
    assert seen["auth"] == "Bearer tok"
    assert seen["url"] == "https://lloyd.test/api/episode"
    assert b'name="slug"\r\n\r\nep-abc123' in seen["body"]
    assert b'name="duration_seconds"\r\n\r\n42' in seen["body"]
    assert b"ID3fake" in seen["body"]
    m = articles.load_meta(slug)
    assert m["lloydio_published_at"] and m["lloydio_audio_url"] == "https://media/ep.mp3"
    assert not lloydio.publishable(m)


def test_publish_failure_is_recorded_and_retried(monkeypatch):
    monkeypatch.setenv("LLOYDIO_BASE_URL", "https://lloyd.test")
    monkeypatch.setenv("LLOYDIO_EPISODE_TOKEN", "tok")
    slug = _published()
    lloydio.publish_pending(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    m = articles.load_meta(slug)
    assert m["state"] == "published" and m["lloydio_error"]
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"audio_url": "u"}))
    assert lloydio.publish_pending(transport=ok) == [slug]
