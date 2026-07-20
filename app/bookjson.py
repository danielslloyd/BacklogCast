"""Clean article markdown -> Henty book.json (the flat 'blocks' importer format).

Henty's `batch_import_books.py` consumes:

    {"title": "...", "blocks": [{"type": "heading"|"para"|"verse", "text": "..."}]}

where each `heading` starts a new chapter and `para`/`verse` blocks are content.
It does NOT re-split chunks, and Chatterbox garbles/truncates inputs past a few
hundred characters, so we hand it text pre-split to <= MAX_CHUNK_CHARS on
sentence boundaries, with all markdown stripped (see app.extractor).
"""
from __future__ import annotations

import re
from typing import Any

from .extractor import sanitize_inline, strip_block_marker

# Henty's own MAX_CHUNK_SIZE is 500 (measured after markup resolution). Our text
# has no markup left, so length is final; keep a margin under 500.
MAX_CHUNK_CHARS = 480

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*\S)\s*$")
_CODE_FENCE_LINE = re.compile(r"^\s*```")
# split after . ! ? (optionally followed by a closing quote/paren) + whitespace,
# before something that looks like a new sentence start.
_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])["\')\]]?\s+(?=["\'(\[]?[A-Z0-9])')


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [p.strip() for p in _SENT_SPLIT_RE.split(text) if p.strip()]


def _hard_wrap(sentence: str, limit: int) -> list[str]:
    """Split an over-long sentence on word boundaries (last resort)."""
    out: list[str] = []
    cur = ""
    for word in sentence.split():
        if cur and len(cur) + 1 + len(word) > limit:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
        # a single word longer than limit: emit it alone rather than loop forever
        while len(cur) > limit:
            out.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        out.append(cur)
    return out


def chunk_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Pack sentences into <=limit chunks; hard-wrap any lone >limit sentence."""
    chunks: list[str] = []
    cur = ""
    for sent in split_sentences(text):
        pieces = [sent] if len(sent) <= limit else _hard_wrap(sent, limit)
        for piece in pieces:
            if cur and len(cur) + 1 + len(piece) > limit:
                chunks.append(cur)
                cur = piece
            else:
                cur = f"{cur} {piece}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def build(md: str, title: str = "", limit: int = MAX_CHUNK_CHARS) -> dict[str, Any]:
    """Parse cleaned/markdown article text into a Henty 'blocks' book.json dict."""
    blocks: list[dict[str, Any]] = []
    para_lines: list[str] = []
    in_fence = False

    def flush_para() -> None:
        if not para_lines:
            return
        raw = " ".join(l for l in para_lines if l)
        para_lines.clear()
        clean = sanitize_inline(raw)
        for chunk in chunk_text(clean, limit):
            if chunk:
                blocks.append({"type": "para", "text": chunk})

    for line in md.splitlines():
        if _CODE_FENCE_LINE.match(line):
            in_fence = not in_fence
            flush_para()
            continue
        if in_fence:
            continue  # drop code blocks entirely
        if not line.strip():
            flush_para()
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            htext = sanitize_inline(m.group(1))
            if htext:
                blocks.append({"type": "heading", "text": htext})
            continue
        para_lines.append(strip_block_marker(line.strip()))
    flush_para()

    # Guarantee at least one named chapter: if the source had no headings, use
    # the article title so Henty doesn't dump everything into "Front Matter".
    if title and not any(b["type"] == "heading" for b in blocks):
        blocks.insert(0, {"type": "heading", "text": sanitize_inline(title)})

    return {"title": title or "Untitled", "blocks": blocks}


def chunk_texts(book: dict[str, Any]) -> list[str]:
    """All spoken chunk texts in order (para/verse blocks) — handy for tests."""
    return [b["text"] for b in book.get("blocks", []) if b.get("type") in ("para", "verse")]
