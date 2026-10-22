"""FastAPI app: UI static files, JSON API, RSS feed, audio streaming."""
from __future__ import annotations

import base64
import logging
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import articles, extractor, feed, henty, lloydio, tts
from .config import (
    admin_password, create_token, lloydio_poll_seconds, load_config, load_tokens,
    public_base_url, revoke_token, save_config, token_valid,
)
from .paths import COVER_PATH, DEFAULT_PASSWORD, ensure_data_tree, article_dir

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
                if secs > 0:
                    res = lloydio.sync()
                    if res.get("created") or res.get("requeued"):
                        tts.worker.kick()
            except Exception:
                log.exception("lloydio poll failed")
            self._stop.wait(timeout=secs if secs and secs >= 30 else 60)


poller = _LloydioPoller()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_data_tree()
    if admin_password() == DEFAULT_PASSWORD:
        log.warning(
            "admin password is the built-in default; this box is reachable "
            "publicly, so change data/password.txt or set BACKLOGCAST_PASSWORD."
        )
    tts.worker.start()
    poller.start()
    log.info("backlogcast ready")
    try:
        yield
    finally:
        tts.worker.stop()
        poller.stop()


app = FastAPI(title="BacklogCast", lifespan=lifespan)


# --- auth ------------------------------------------------------------------
# Podcast clients can't log in, so the feed, its audio enclosures, and the
# cover art stay public (those URLs carry their own secret per-feed token).
# Everything else — the admin UI, its static assets, and the whole /api
# surface — requires the admin password via HTTP Basic auth.
_PUBLIC_PREFIXES = ("/feed/", "/audio/")
_PUBLIC_PATHS = {"/cover.jpg"}


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)


@app.middleware("http")
async def require_admin_auth(request: Request, call_next):
    if _is_public_path(request.url.path):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            _user, _, supplied = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            supplied = ""
        expected = admin_password()
        if supplied and secrets.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            return await call_next(request)
    return Response(
        "Authentication required.",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="BacklogCast"'},
    )


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


_henty_seen: tuple[float, bool] = (0.0, False)


def _henty_up() -> bool:
    """henty.is_available, cached briefly: the board polls every few seconds."""
    global _henty_seen
    if time.monotonic() - _henty_seen[0] > 15:
        _henty_seen = (time.monotonic(), henty.is_available())
    return _henty_seen[1]


# --- the board ------------------------------------------------------------
#
# The UI is one kanban board: every article sits in exactly one column, and the
# worker's two queues (extraction, narration) are the columns that carry live
# progress. The rows are trimmed on purpose -- a full meta carries
# chunk_reports, which is far too big to poll every couple of seconds.

BOARD_COLUMNS: tuple[dict[str, Any], ...] = (
    {"key": "intake", "label": "Text queue", "states": ("extracting", "queued")},
    {"key": "text", "label": "Text ready",
     "states": ("text_ready", "book_built", "needs_review")},
    {"key": "narration", "label": "Narration queue", "states": ("synthesizing", "approved")},
    {"key": "ready", "label": "Ready to publish", "states": ("stitched",)},
    {"key": "published", "label": "Published", "states": ("published",)},
    # Catch-all, so a state nobody mapped can never vanish off the board.
    {"key": "attention", "label": "Needs attention", "states": ("failed", "removed", "cancelled")},
)

# Columns the worker takes in order: running job first, then next-up first.
QUEUE_COLUMNS = {"intake": "fetched_at", "narration": "approved_at"}
RUNNING_STATES = {"extracting", "synthesizing"}

# Everything else: newest first, by the date that column is about.
_RECENCY_KEY = {"text": "fetched_at", "ready": "fetched_at",
                "published": "published_at", "attention": "fetched_at"}

_MAPPED_STATES = {s for c in BOARD_COLUMNS[:-1] for s in c["states"]}

_BOARD_FIELDS = (
    "slug", "title", "author", "publication", "state", "source_url", "error",
    "retry_after", "extract_attempts", "synth_attempts", "n_chunks", "narrate",
    "fetched_at", "approved_at", "published_at", "duration_seconds",
    "audio_filename", "review_reason", "removed_at",
)


def _board_row(m: dict[str, Any], queue_pos: int | None = None) -> dict[str, Any]:
    row = {k: m.get(k) for k in _BOARD_FIELDS}
    row["running"] = m.get("state") in RUNNING_STATES
    row["due"] = tts.due(m)
    row["queue_pos"] = queue_pos
    return row


def _column_items(key: str, metas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if key in QUEUE_COLUMNS:
        # Mirrors TTSWorker._run: jobs in retry backoff wait their turn.
        order_by = QUEUE_COLUMNS[key]
        rows = [_board_row(m) for m in metas if m.get("state") in RUNNING_STATES]
        waiting = sorted((m for m in metas if m.get("state") not in RUNNING_STATES),
                         key=lambda m: (not tts.due(m), m.get(order_by) or ""))
        return rows + [_board_row(m, queue_pos=i) for i, m in enumerate(waiting, 1)]
    order_by = _RECENCY_KEY[key]
    metas = sorted(metas, key=lambda m: m.get(order_by) or "", reverse=True)
    return [_board_row(m) for m in metas]


@app.get("/api/board")
def board() -> dict[str, Any]:
    """Every article bucketed into its column, plus what the worker is doing
    right now. The only endpoint the board polls."""
    all_ = list(articles.iter_articles())
    henty_up = _henty_up()
    columns = []
    for col in BOARD_COLUMNS:
        if col["key"] == "attention":  # the catch-all
            metas = [m for m in all_ if m.get("state") not in _MAPPED_STATES]
        else:
            metas = [m for m in all_ if m.get("state") in col["states"]]
        columns.append({
            "key": col["key"],
            "label": col["label"],
            "items": _column_items(col["key"], metas),
        })
    by_key = {c["key"]: c["items"] for c in columns}
    # Why narration is sitting still, when it is.
    if any(not r["running"] for r in by_key["narration"]):
        note = None
        if not henty_up:
            note = "Henty is offline — narration waits until it's up."
        elif any(r["running"] or r["due"] for r in by_key["intake"]):
            note = "Text extraction goes first."
        if note:
            next(c for c in columns if c["key"] == "narration")["note"] = note
    return {
        "now": articles.utcnow_iso(),
        "active": tts.current_progress(),
        "henty_up": henty_up,
        "columns": columns,
    }


@app.get("/api/articles/{slug}")
def get_article(slug: str) -> dict[str, Any]:
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    return _article_payload(slug, include_body=True)


@app.post("/api/articles")
def add_article(body: AddArticleIn) -> dict[str, Any]:
    if body.url:
        # Empty body: the worker fetches (HTML or PDF), drafts, and runs the
        # Claude cleanup — the same path lloydio-ingested jobs take.
        meta = articles.new_article(
            title=body.title or body.url,
            source_url=body.url,
            body="",
            raw_html=None,
            extraction_method="pending",
            state="queued",
        )
        tts.worker.kick()
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
    if res.get("created") or res.get("requeued"):
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
    has_saved = articles.raw_html_path(slug).exists() or articles.source_pdf_path(slug).exists()
    if not meta.get("source_url") and not has_saved:
        raise HTTPException(400, "no source url and no saved source to re-extract from")
    # Clear the body; the worker re-extracts from the saved source (or re-fetches).
    articles.update_article_text(slug, title=meta.get("title"), author=meta.get("author"), body="")
    articles.set_state(slug, "queued", error=None, review_reason=None)
    tts.worker.kick()
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
    # A manual retry gets a fresh set of automatic attempts; no text yet means
    # extraction is what failed.
    reset = dict(error=None, retry_after=None, extract_attempts=0, synth_attempts=0)
    if articles.load_body(slug).strip():
        articles.set_state(slug, "approved", approved_at=articles.utcnow_iso(), **reset)
    else:
        articles.set_state(slug, "queued", **reset)
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


@app.post("/api/articles/{slug}/delete-audio")
def delete_audio(slug: str) -> dict[str, Any]:
    """Drop the audio (episode + every Henty take), keep the article text."""
    if not articles.meta_path(slug).exists():
        raise HTTPException(404, "not found")
    if articles.load_meta(slug).get("state") == "synthesizing":
        raise HTTPException(400, "currently synthesizing; wait for it to finish")
    _meta, freed = articles.delete_audio(slug)
    payload = _article_payload(slug)
    payload["freed_bytes"] = freed
    return payload


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


# Without a Cache-Control header a browser invents its own freshness window
# (roughly a tenth of the file's age), so a changed UI can sit unseen behind a
# stale cache. The shell is therefore never stored, and it stamps each asset
# URL with the newest mtime in static/ so a changed asset gets a fresh URL.
_ASSET_RE = re.compile(r"(/static/[A-Za-z0-9_.\-]+)")


def _asset_version() -> str:
    newest = max((p.stat().st_mtime for p in STATIC_DIR.iterdir() if p.is_file()), default=0.0)
    return format(int(newest), "x")


@app.get("/")
def index() -> Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    version = _asset_version()
    html = _ASSET_RE.sub(lambda m: f"{m.group(1)}?v={version}", html)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.exception_handler(HTTPException)
async def http_err(_req: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
