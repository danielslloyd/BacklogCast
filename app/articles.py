"""Article model: filesystem-backed CRUD + state transitions."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from slugify import slugify

from .paths import ARTICLES_DIR, article_dir

# Job lifecycle for the lloydio -> Henty -> local-feed pipeline.
STATES = {
    "queued",        # ingested from lloydio / manually added; awaiting processing
    "extracting",    # fetch + clean + build book.json (transient)
    "book_built",    # book.json ready; awaiting approval (auto or manual)
    "needs_review",  # flagged for human review/annotation before synthesis
    "approved",      # cleared for synthesis; the TTS worker picks these up
    "synthesizing",  # Henty generation + ASR feedback loop in progress
    "stitched",      # audio ready locally; awaiting publish
    "published",     # included in the feed
    "failed",        # error; see the `error` field
    "text_ready",    # clean text extracted (and sent to lloydio); not narrated
    "cancelled",     # legacy: narration cancelled; sync moves these to text_ready
}

# States from which the article can be (re)published into the feed.
PUBLISHABLE = {"stitched", "published"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_slug(title: str, when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    base = slugify(title or "untitled", max_length=60) or "untitled"
    date_prefix = when.strftime("%Y-%m-%d")
    candidate = f"{date_prefix}-{base}"
    # collide-proof: if it already exists, append -2, -3, ...
    i = 2
    final = candidate
    while (ARTICLES_DIR / final).exists():
        final = f"{candidate}-{i}"
        i += 1
    return final


# --- frontmatter helpers ----------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, m.group(2)


def join_frontmatter(fm: dict[str, str], body: str) -> str:
    lines = ["---"]
    for k, v in fm.items():
        # collapse any newlines so frontmatter stays single-line per key
        sv = str(v).replace("\n", " ").strip()
        lines.append(f"{k}: {sv}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body.lstrip("\n")


# --- IO ---------------------------------------------------------------------

def meta_path(slug: str) -> Path:
    return article_dir(slug) / "meta.json"


def md_path(slug: str) -> Path:
    return article_dir(slug) / "article.md"


def raw_html_path(slug: str) -> Path:
    return article_dir(slug) / "raw.html"


def source_pdf_path(slug: str) -> Path:
    return article_dir(slug) / "source.pdf"


def load_meta(slug: str) -> dict[str, Any]:
    return json.loads(meta_path(slug).read_text())


def save_meta(slug: str, meta: dict[str, Any]) -> None:
    meta_path(slug).write_text(json.dumps(meta, indent=2))


def load_body(slug: str) -> str:
    if not md_path(slug).exists():
        return ""
    _, body = split_frontmatter(md_path(slug).read_text(encoding="utf-8"))
    return body


def load_full_md(slug: str) -> str:
    return md_path(slug).read_text(encoding="utf-8") if md_path(slug).exists() else ""


def save_md(slug: str, fm: dict[str, str], body: str) -> None:
    md_path(slug).write_text(join_frontmatter(fm, body), encoding="utf-8")


def list_slugs() -> list[str]:
    if not ARTICLES_DIR.exists():
        return []
    return sorted(p.name for p in ARTICLES_DIR.iterdir() if (p / "meta.json").exists())


def iter_articles(state: str | None = None) -> Iterator[dict[str, Any]]:
    for slug in list_slugs():
        try:
            meta = load_meta(slug)
        except Exception:
            continue
        if state and meta.get("state") != state:
            continue
        yield meta


def delete_article(slug: str) -> None:
    d = article_dir(slug)
    if d.exists():
        shutil.rmtree(d)


def delete_audio(slug: str) -> tuple[dict[str, Any], int]:
    """Delete generated audio but keep the article's text and metadata.

    Removes the episode file *and* the whole Henty project folder. That folder
    holds every per-chunk take -- the bulk of the disk use -- and leaving it
    behind would make Henty's importer bail with "skipped (audio already
    exists)" on the next run and re-narrate stale text.

    Lands in `needs_review`: the TTS worker only picks up `queued` and
    `approved`, so the audio we just freed won't immediately regenerate. The
    UI's "approve" button re-narrates on demand.

    Returns (meta, bytes_freed).
    """
    from .config import henty_books_dir  # local import keeps this module leaf-level

    meta = load_meta(slug)
    freed = 0

    fname = meta.get("audio_filename")
    if fname:
        p = article_dir(slug) / fname
        if p.exists():
            freed += p.stat().st_size
            p.unlink()

    books_dir = henty_books_dir()
    if books_dir:
        project = Path(books_dir) / slug
        if project.is_dir():
            freed += sum(f.stat().st_size for f in project.rglob("*") if f.is_file())
            shutil.rmtree(project)

    meta = set_state(
        slug, "needs_review",
        audio_filename=None, audio_bytes=None, duration_seconds=None,
        chunk_reports=[], chunks_below_threshold=0,
        book_json_path=None, henty_project_path=None,
        published_at=None, error=None,
    )
    return meta, freed


def audio_path(slug: str) -> Path | None:
    meta = load_meta(slug)
    fname = meta.get("audio_filename")
    if not fname:
        return None
    p = article_dir(slug) / fname
    return p if p.exists() else None


def find_by_source_url(url: str) -> dict[str, Any] | None:
    """Return the first article whose source_url matches, else None (dedupe)."""
    if not url:
        return None
    for meta in iter_articles():
        if meta.get("source_url") == url:
            return meta
    return None


def new_article(
    *,
    title: str,
    source_url: str | None,
    body: str,
    raw_html: str | None,
    extraction_method: str,
    author: str = "",
    state: str = "queued",
) -> dict[str, Any]:
    slug = make_slug(title)
    d = article_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "slug": slug,
        "state": state,
        "title": title or "Untitled",
        "author": author,
        "source_url": source_url or "",
        "fetched_at": utcnow_iso(),
        "approved_at": None,
        "published_at": None,
        "duration_seconds": None,
        "audio_filename": None,
        "audio_bytes": None,
        "extraction_method": extraction_method,
        # integration fields
        "book_json_path": None,      # where we wrote Henty's book.json
        "henty_project_path": None,  # Henty project.json path after import
        "chunk_reports": [],         # [{chunk_id, similarity, retries, truncated}]
        "error": None,
    }
    save_meta(slug, meta)
    fm = {
        "title": meta["title"],
        "author": meta["author"],
        "source_url": meta["source_url"],
        "fetched_at": meta["fetched_at"],
    }
    save_md(slug, fm, body)
    if raw_html:
        raw_html_path(slug).write_text(raw_html, encoding="utf-8")
    return meta


def update_article_text(
    slug: str, *, title: str | None, author: str | None, body: str | None
) -> dict[str, Any]:
    meta = load_meta(slug)
    fm, current_body = split_frontmatter(load_full_md(slug))
    if title is not None:
        meta["title"] = title
        fm["title"] = title
    if author is not None:
        meta["author"] = author
        fm["author"] = author
    new_body = body if body is not None else current_body
    fm.setdefault("source_url", meta.get("source_url", ""))
    fm.setdefault("fetched_at", meta.get("fetched_at", ""))
    save_md(slug, fm, new_body)
    save_meta(slug, meta)
    return meta


def set_state(slug: str, state: str, **extra: Any) -> dict[str, Any]:
    if state not in STATES:
        raise ValueError(f"unknown state: {state}")
    meta = load_meta(slug)
    meta["state"] = state
    for k, v in extra.items():
        meta[k] = v
    save_meta(slug, meta)
    return meta
