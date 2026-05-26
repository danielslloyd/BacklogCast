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

STATES = {
    "fetched",
    "needs_review",
    "approved",
    "synthesizing",
    "ready",
    "published",
    "failed",
}


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


def audio_path(slug: str) -> Path | None:
    meta = load_meta(slug)
    fname = meta.get("audio_filename")
    if not fname:
        return None
    p = article_dir(slug) / fname
    return p if p.exists() else None


def new_article(
    *,
    title: str,
    source_url: str | None,
    body: str,
    raw_html: str | None,
    extraction_method: str,
    author: str = "",
) -> dict[str, Any]:
    slug = make_slug(title)
    d = article_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    meta = {
        "slug": slug,
        "state": "needs_review",
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
