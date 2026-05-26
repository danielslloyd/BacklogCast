"""RSS feed generation."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import articles
from .config import load_config, public_base_url
from .paths import COVER_PATH

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["xml"]),
)


def _hms(seconds: int | None) -> str:
    s = int(seconds or 0)
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _rfc822(iso: str | None) -> str:
    if not iso:
        return format_datetime(datetime.now(timezone.utc))
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.now(timezone.utc)
    return format_datetime(dt)


def _mime_for(fname: str) -> str:
    fname = fname.lower()
    if fname.endswith(".mp3"):
        return "audio/mpeg"
    if fname.endswith(".wav"):
        return "audio/wav"
    if fname.endswith(".ogg") or fname.endswith(".opus"):
        return "audio/ogg"
    if fname.endswith(".m4a"):
        return "audio/mp4"
    return "application/octet-stream"


def render_feed(token: str, request_host: str | None = None) -> str:
    cfg = load_config()
    base = public_base_url(request_host)
    feed_url = f"{base}/feed/{token}.xml"
    cover_url = f"{base}/cover.jpg" if COVER_PATH.exists() else ""

    items = []
    published = [m for m in articles.iter_articles(state="published")]
    published.sort(key=lambda m: m.get("published_at") or "", reverse=True)
    for m in published:
        slug = m["slug"]
        fname = m.get("audio_filename") or "audio.mp3"
        ext = Path(fname).suffix.lstrip(".") or "mp3"
        body_snippet = articles.load_body(slug)[:500].strip()
        items.append({
            "slug": slug,
            "title": m.get("title") or slug,
            "author": m.get("author") or cfg["podcast_author"],
            "description": body_snippet or m.get("title") or "",
            "pub_date": _rfc822(m.get("published_at")),
            "audio_url": f"{base}/audio/{token}/{slug}.{ext}",
            "audio_bytes": m.get("audio_bytes") or 0,
            "duration_hms": _hms(m.get("duration_seconds")),
            "mime": _mime_for(fname),
            "source_url": m.get("source_url") or "",
        })

    template = _env.get_template("feed.xml.j2")
    return template.render(
        cfg=cfg,
        base_url=base,
        feed_url=feed_url,
        cover_url=cover_url,
        items=items,
    )
