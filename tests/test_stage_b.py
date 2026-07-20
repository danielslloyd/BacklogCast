"""Stage B: sanitizer + book.json builder — the core garbling fix.

These are the pieces that can be fully verified without Henty/lloydio/a GPU.
"""
import re

from app import bookjson
from app.extractor import sanitize_inline, tts_clean

# any of these reaching Chatterbox is what produces "comically garbled" speech
MARKDOWN_SYMBOLS = re.compile(r"[*_`#>]|\]\(|\[\^?\d")


def test_sanitize_inline_strips_all_markup():
    src = (
        "See **bold**, _italic_, `code`, a [link](http://x.com) and a footnote[^3]. "
        "Also ***all three*** and a bare cite[12]."
    )
    out = sanitize_inline(src)
    assert "bold" in out and "italic" in out and "code" in out and "link" in out
    assert "all three" in out
    assert not MARKDOWN_SYMBOLS.search(out), out
    assert "http" not in out  # the URL is dropped; only the visible link text is kept
    assert "[" not in out and "]" not in out


def test_sanitize_inline_normalizes_punct():
    out = sanitize_inline("“Hello” — A&B at 50% …done")
    assert "“" not in out and "—" not in out and "…" not in out
    assert " and " in out and "percent" in out


def test_tts_clean_removes_structure():
    md = (
        "# Title\n\n"
        "First **para** with a [link](http://x).\n\n"
        "## Section\n\n"
        "- bullet one\n- bullet two\n\n"
        "> a quote\n\n"
        "```\ncode = should_drop()\n```\n"
    )
    out = tts_clean(md)
    assert not MARKDOWN_SYMBOLS.search(out), out
    assert "Title" in out and "Section" in out and "para" in out
    assert "should_drop" not in out  # code fence dropped (not prose-like)
    # paragraphs preserved as blank-line separated blocks
    assert "\n\n" in out


def test_chunk_text_respects_limit_and_packs():
    text = " ".join(f"Sentence number {i}." for i in range(1, 40))
    chunks = bookjson.chunk_text(text, limit=80)
    assert chunks
    assert all(len(c) <= 80 for c in chunks), [len(c) for c in chunks]
    # sentences should be packed, not one-per-chunk
    assert len(chunks) < 39


def test_chunk_text_hard_wraps_long_sentence():
    long_sent = "word " * 200  # ~1000 chars, no sentence break
    chunks = bookjson.chunk_text(long_sent.strip(), limit=100)
    assert all(len(c) <= 100 for c in chunks)
    # nothing lost
    assert sum(c.count("word") for c in chunks) == 200


def test_build_headings_become_chapters_and_chunks_are_safe():
    md = (
        "# Intro\n\n"
        + ("This is a sentence. " * 60)  # long -> must split into many <=480 chunks
        + "\n\n## Deep Dive\n\n"
        + "Short **tail** paragraph with `code` and a [ref](http://y).\n"
    )
    book = bookjson.build(md, title="My Article")
    types = [b["type"] for b in book["blocks"]]
    assert types.count("heading") == 2  # Intro + Deep Dive
    assert book["blocks"][0] == {"type": "heading", "text": "Intro"}
    texts = bookjson.chunk_texts(book)
    assert texts, "expected para chunks"
    assert all(len(t) <= bookjson.MAX_CHUNK_CHARS for t in texts), [len(t) for t in texts]
    assert not any(MARKDOWN_SYMBOLS.search(t) for t in texts)


def test_build_inserts_title_when_no_headings():
    book = bookjson.build("Just one paragraph of plain text.", title="Fallback Title")
    assert book["blocks"][0]["type"] == "heading"
    assert book["blocks"][0]["text"] == "Fallback Title"


def test_build_handles_empty():
    book = bookjson.build("", title="")
    assert book == {"title": "Untitled", "blocks": []}
