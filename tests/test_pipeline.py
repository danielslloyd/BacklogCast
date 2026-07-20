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
    monkeypatch.setattr(extractor, "fetch_url", lambda url, **k: "<html/>")
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
    assert m["state"] == "failed"
    assert "HENTY_BOOKS_DIR" in (m["error"] or "")
