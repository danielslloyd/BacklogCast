"""FastAPI app: UI static files, JSON API, RSS feed, audio streaming."""
from __future__ import annotations

import logging
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import articles, extractor, feed, lloydio, tts
from .config import (
    create_token, lloydio_poll_seconds, load_config, load_tokens, public_base_url,
    revoke_token, save_config, token_valid,
)
from .paths import COVER_PATH, ensure_data_tree, article_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backlogcast")

STATIC_DIR = Path(__file__).parent.parent / "static"


class _LloydioPoller:
    """Background thread that periodically ingests lloydio's queue (if enabled)."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="lloydio-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            secs = 0
            try:
                secs = lloydio_poll_seconds()
                if secs > 0 and lloydio.sync().get("created"):
                    tts.worker.kick()
            except Exception:
                log.exception("lloydio poll failed")
            self._stop.wait(timeout=secs if secs and secs >= 30 else 60)


poller = _LloydioPoller()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_tree()
    tts.worker.start()
    poller.start()
    log.info("backlogcast ready")
    try:
        yield
    finally:
        tts.worker.stop()
        poller.stop()


app = FastAPI(title="BacklogCast", lifespan=lifespan)


# --- request models -------------------------------------------------------

class AddArticleIn(BaseModel):
    url: str | None = None
    title: str | None = None
    text: str | None = None


class UpdateArticleIn(BaseModel):
    title: str | None = None
    author: str | None = None
    body: str | None = None


class ConfigIn(BaseModel):
    podcast_title: str | None = None
    podcast_description: str | None = None
    podcast_language: str | None = None
    podcast_author: str | None = None
    owner_name: str | None = None
    owner_email: str | None = None
    category: str | None = None
    explicit: bool | None = None
    public_base_url: str | None = None
    wpm: int | None = None
    # integration settings (HENTY_API_KEY stays env-only, never persisted)
    lloydio_base_url: str | None = None
    henty_base_url: str | None = None
    henty_books_dir: str | None = None
    default_voice: str | None = None
    asr_similarity_threshold: float | None = None
    asr_max_retries: int | None = None
    auto_approve: bool | None = None
    auto_publish: bool | None = None
    lloydio_poll_seconds: int | None = None


class TokenIn(BaseModel):
    name: str


# --- helpers --------------------------------------------------------------

def _request_host(req: Request) -> str:
    scheme = req.headers.get("x-forwarded-proto") or req.url.scheme
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or "127.0.0.1:8000"
    return f"{scheme}://{host}"


def _article_payload(slug: str, include_body: bool = False) -> dict[str, Any]:
    meta = articles.load_meta(slug)
    if include_body:
        meta["body"] = articles.load_body(slug)
        cfg = load_config()
        wpm = max(1, int(cfg.get("wpm", 160)))
        word_count = len(re.findall(r"\w+", meta["body"]))
        meta["word_count"] = word_count
        meta["estimated_seconds"] = int(word_count / wpm * 60)
    return meta


# --- article endpoints ----------------------------------------------------

@app.get("/api/articles")
def list_articles(state: str | None = None) -> dict[str, Any]:
    items = list(articles.iter_articles(state=state))
    items.sort(key=lambda m: m.get("fetched_at") or "", reverse=True)
    return {"items": items}


@app.get("/api/articles/{slug}")
def get_article(slug: str) -> dict[str, Any]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    return _article_payload(slug, include_body=True)


@app.post("/api/articles")
def add_article(body: AddArticleIn) -> dict[str, Any]:
    if body.url:
        try:
            html = extractor.fetch_url(body.url)
        except Exception as e:
            raise HTTPException(400, f"fetch failed: {e}")
        result = extractor.extract(html, url=body.url)
        title = result["title"] or body.title or body.url
        meta = articles.new_article(
            title=title,
            source_url=body.url,
            body=result["body"],
            raw_html=html,
            extraction_method=result["method"],
            author=result["author"],
            state="needs_review" if result["confidence_low"] else "queued",
        )
        tts.worker.kick()  # worker builds book.json (body already extracted)
        return _article_payload(meta["slug"], include_body=True)
    if body.text:
        title = (body.title or body.text.strip().split("\n", 1)[0] or "Pasted").strip()[:120]
        cleaned = extractor.tts_clean(body.text)
        meta = articles.new_article(
            title=title,
            source_url=None,
            body=cleaned,
            raw_html=None,
            extraction_method="manual",
            author="",
            state="queued",
        )
        tts.worker.kick()
        return _article_payload(meta["slug"], include_body=True)
    raise HTTPException(400, "provide url or text")


@app.post("/api/ingest/lloydio")
def ingest_lloydio() -> dict[str, Any]:
    """Poll lloydio's queue now and create local jobs for new podcast items."""
    try:
        res = lloydio.sync()
    except Exception as e:
        raise HTTPException(400, f"lloydio sync failed: {e}")
    if res.get("created"):
        tts.worker.kick()
    return res


@app.put("/api/articles/{slug}")
def update_article(slug: str, body: UpdateArticleIn) -> dict[str, Any]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    articles.update_article_text(
        slug, title=body.title, author=body.author, body=body.body
    )
    return _article_payload(slug, include_body=True)


@app.post("/api/articles/{slug}/reextract")
def reextract(slug: str) -> dict[str, Any]:
    meta = articles.load_meta(slug)
    src = meta.get("source_url")
    raw = articles.raw_html_path(slug)
    html: str | None = None
    if raw.exists():
        html = raw.read_text(encoding="utf-8")
    elif src:
        try:
            html = extractor.fetch_url(src)
            raw.write_text(html, encoding="utf-8")
        except Exception as e:
            raise HTTPException(400, f"re-fetch failed: {e}")
    else:
        raise HTTPException(400, "no source url and no raw html to re-extract from")
    result = extractor.extract(html, url=src)
    articles.update_article_text(
        slug,
        title=result["title"] or meta.get("title"),
        author=result["author"] or meta.get("author"),
        body=result["body"],
    )
    articles.set_state(
        slug,
        "needs_review" if result["confidence_low"] else "queued",
        extraction_method=result["method"],
    )
    if not result["confidence_low"]:
        tts.worker.kick()  # rebuild book.json from the re-extracted text
    return _article_payload(slug, include_body=True)


@app.post("/api/articles/{slug}/approve")
def approve(slug: str) -> dict[str, Any]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    articles.set_state(slug, "approved", approved_at=articles.utcnow_iso(), error=None)
    tts.worker.kick()
    return _article_payload(slug)


@app.post("/api/articles/{slug}/retry")
def retry(slug: str) -> dict[str, Any]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    articles.set_state(slug, "approved", error=None, approved_at=articles.utcnow_iso())
    tts.worker.kick()
    return _article_payload(slug)


@app.post("/api/articles/{slug}/publish")
def publish(slug: str) -> dict[str, Any]:
    meta = articles.load_meta(slug)
    if meta.get("state") not in articles.PUBLISHABLE:
        raise HTTPException(400, "article is not ready")
    articles.set_state(slug, "published", published_at=articles.utcnow_iso())
    return _article_payload(slug)


@app.post("/api/articles/{slug}/unpublish")
def unpublish(slug: str) -> dict[str, Any]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    articles.set_state(slug, "stitched", published_at=None)
    return _article_payload(slug)


@app.delete("/api/articles/{slug}")
def delete(slug: str) -> dict[str, str]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    articles.delete_article(slug)
    return {"deleted": slug}


# --- config & tokens ------------------------------------------------------

@app.get("/api/config")
def get_config() -> dict[str, Any]:
    return load_config()


@app.put("/api/config")
def update_config(body: ConfigIn) -> dict[str, Any]:
    payload = {k: v for k, v in body.model_dump().items() if v is not None}
    return save_config(payload)


@app.get("/api/tokens")
def list_tokens_endpoint() -> dict[str, Any]:
    return {"tokens": [{"name": n, "token": t} for n, t in load_tokens().items()]}


@app.post("/api/tokens")
def create_token_endpoint(body: TokenIn) -> dict[str, str]:
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "name required")
    return {"name": name, "token": create_token(name)}


@app.delete("/api/tokens/{token}")
def revoke_token_endpoint(token: str) -> dict[str, bool]:
    return {"revoked": revoke_token(token)}


# --- feed & audio ---------------------------------------------------------

@app.get("/feed/{token}.xml")
def get_feed(token: str, request: Request) -> Response:
    if not token_valid(token):
        raise HTTPException(404, "not found")
    body = feed.render_feed(token, request_host=_request_host(request))
    return Response(content=body, media_type="application/rss+xml; charset=utf-8")


_AUDIO_RE = re.compile(r"^(?P<slug>[A-Za-z0-9_\-]+)\.(?P<ext>mp3|wav|ogg|m4a|opus)$")


@app.get("/audio/{token}/{filename}")
def get_audio(token: str, filename: str, request: Request) -> Response:
    if not token_valid(token):
        raise HTTPException(404, "not found")
    m = _AUDIO_RE.match(filename)
    if not m:
        raise HTTPException(400, "bad filename")
    slug = m.group("slug")
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    meta = articles.load_meta(slug)
    if meta.get("state") != "published":
        raise HTTPException(404, "not published")
    audio = articles.audio_path(slug)
    if not audio:
        raise HTTPException(404, "no audio")
    return FileResponse(
        str(audio),
        media_type=feed._mime_for(audio.name),
        filename=audio.name,
    )


@app.get("/cover.jpg")
def cover() -> Response:
    if COVER_PATH.exists():
        return FileResponse(str(COVER_PATH), media_type="image/jpeg")
    raise HTTPException(404, "no cover")


# --- static UI ------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> Response:
    return FileResponse(str(STATIC_DIR / "index.html"), media_type="text/html")


@app.exception_handler(HTTPException)
async def http_err(_req: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
