"""Pipeline wiring + state machine (runnable without Henty/GPU)."""
import json
import re
from pathlib import Path

from app import articles, bookjson, extractor, tts

MARKDOWN = re.compile(r"[*_`#>]")


def _book(slug):
    m = articles.load_meta(slug)
    return json.loads(Path(m["book_json_path"]).read_text(encoding="utf-8"))


def test_process_queued_builds_clean_book_from_body():
    md = "# Test Article\n\n" + ("This is a sentence. " * 30) + \
         "\n\n## Part Two\n\nA **markdown** para with `code` and a [ref](http://y).\n"
    meta = articles.new_article(title="Test Article", source_url="", body=md,
                                raw_html=None, extraction_method="manual", state="queued")
    tts.process_queued(meta["slug"])
    m = articles.load_meta(meta["slug"])
    assert m["state"] == "approved"          # auto_approve default
    book = _book(meta["slug"])
    headings = [b["text"] for b in book["blocks"] if b["type"] == "heading"]
    assert headings == ["Test Article", "Part Two"]
    paras = [b for b in book["blocks"] if b["type"] == "para"]
    assert paras
    assert all(len(b["text"]) <= bookjson.MAX_CHUNK_CHARS for b in paras)
    assert not any(MARKDOWN.search(b["text"]) for b in book["blocks"])


def test_process_queued_fetches_when_body_empty(monkeypatch):
    monkeypatch.setattr(extractor, "fetch", lambda url, **k: extractor.Fetched(
        "html", b"<html/>", url, "<html/>"))
    monkeypatch.setattr(extractor, "extract", lambda html, url=None: {
        "body": "# Heading\n\nHello world. Second sentence here.\n",
        "title": "Fetched", "author": "", "method": "trafilatura",
        "confidence_low": False,
    })
    meta = articles.new_article(title="", source_url="http://x/9", body="",
                                raw_html=None, extraction_method="lloydio-queue", state="queued")
    tts.process_queued(meta["slug"])
    m = articles.load_meta(meta["slug"])
    assert m["state"] == "approved"
    assert m["title"] == "Fetched"
    assert Path(m["book_json_path"]).exists()
    assert articles.raw_html_path(meta["slug"]).exists()


def test_synthesize_without_henty_fails_gracefully():
    meta = articles.new_article(title="X", source_url="", body="# X\n\nHi there friend.\n",
                                raw_html=None, extraction_method="manual", state="approved")
    tts.synthesize(meta["slug"])
    m = articles.load_meta(meta["slug"])
    # First failure: back to approved, waiting out the backoff.
    assert m["state"] == "approved" and m["retry_after"] and not tts.due(m)
    assert "HENTY_BOOKS_DIR" in (m["error"] or "")
    for _ in range(tts.MAX_ATTEMPTS - 1):
        tts.synthesize(meta["slug"])
    m = articles.load_meta(meta["slug"])
    assert m["state"] == "failed" and m["synth_attempts"] == tts.MAX_ATTEMPTS


def test_fetch_stitched_uses_henty_audio_path(tmp_path):
    # Henty's stitch-best-takes returns audio_path/audio_file/audio_url
    wav = tmp_path / "E2E_stitched_1.wav"
    wav.write_bytes(b"RIFF")
    st = {"success": True, "audio_file": wav.name,
          "audio_url": f"/api/project/audio/{wav.name}", "audio_path": str(wav)}
    assert tts._fetch_stitched(None, st, str(tmp_path / "td")) == wav


def test_extract_retries_claude_failure_then_keeps_draft(monkeypatch):
    from app import claude_extract
    monkeypatch.setenv("CLAUDE_EXTRACT", "always")
    monkeypatch.setattr(extractor, "fetch", lambda url, **k: extractor.Fetched(
        "html", b"<html/>", url, "<html/>"))
    monkeypatch.setattr(extractor, "extract", lambda html, url=None: {
        "body": "Draft text here. " * 20, "title": "T", "author": "",
        "method": "trafilatura", "confidence_low": False})

    def limited(*a, **k):
        raise claude_extract.ClaudeExtractError("session limit")
    monkeypatch.setattr(claude_extract, "clean_with_claude", limited)
    meta = articles.new_article(title="T", source_url="http://x/r", body="",
                                raw_html=None, extraction_method="lloydio-queue", state="queued")
    slug = meta["slug"]
    articles.set_state(slug, "queued", narrate=False)
    tts.process_queued(slug)
    m = articles.load_meta(slug)
    assert m["state"] == "queued" and m["extract_attempts"] == 1 and not tts.due(m)
    assert not articles.load_body(slug).strip()

    # Last attempt: the automatic draft is kept, so the link still gets text.
    articles.set_state(slug, "queued", extract_attempts=tts.MAX_ATTEMPTS - 1, retry_after=None)
    tts.process_queued(slug)
    m = articles.load_meta(slug)
    assert m["state"] == "text_ready" and "Draft text" in articles.load_body(slug)
