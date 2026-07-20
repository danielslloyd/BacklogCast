# BacklogCast

Local orchestrator that turns links you share to **lloydio** into a private
podcast, narrated by **Henty** (Chatterbox TTS) with an automatic transcription
feedback loop, and served from a self-hosted RSS feed your podcast app
subscribes to.

```
lloydio (hosted)                 BacklogCast (this app, local)              Henty (local GPU)
  capture (podcast tag) ──queue──▶  ingest → clean MD → book.json ──HTTP──▶  generate + Whisper ASR loop
  /api/podcast-queue.json           drive ASR loop, stitch → mp3   ◀────────  set best take, stitch
                                    serve /feed/<token>.xml + /audio ─tunnel─▶ 📱 podcast app
```

This app is a single FastAPI process: ingest, JSON API, a background worker that
drives Henty, the RSS feed, and audio streaming — all on one port. It has **no
in-process TTS**; Henty does all synthesis.

## The three systems

- **lloydio** (Astro, hosted) owns **capture** and **the feed's source of new
  work**. You share a URL from your phone (PWA share target / bookmarklet /
  extension); it lands in lloydio tagged `podcast` and appears in
  `GET /api/podcast-queue.json`. BacklogCast polls that queue.
- **Henty** (Flask + Chatterbox/Chatterbox-Turbo, **GPU-only**) does **TTS**. It
  imports a `book.json` (chapters→chunks), synthesizes per chunk, and ships a
  built-in Whisper verifier (`/api/project/transcribe-take`).
- **BacklogCast** (this repo) is the **local orchestrator + feed host** that
  wires them together and adds the ASR retry loop.

## Pipeline & states

A job moves through: `queued → extracting → book_built → approved →
synthesizing → stitched → published` (`needs_review` gates low-confidence
extractions; `failed` on error).

1. **Ingest** (`app/lloydio.py`): poll `/api/podcast-queue.json`, dedupe by URL,
   create a `queued` job.
2. **Clean MD → book.json** (`app/extractor.py` + `app/bookjson.py`): fetch,
   extract (trafilatura→readability), **strip all markdown**, and split into
   sentence-aware **≤480-char** chunks in Henty's `blocks` format. This is the
   fix for garbled speech — Henty has no sanitizer, so stray `#`/`*`/`_`/links
   reaching Chatterbox is what produced gibberish.
3. **TTS + ASR loop** (`app/henty.py`): for each chunk, generate → transcribe →
   keep the best-scoring take, regenerating while Whisper similarity is below
   `ASR_SIMILARITY_THRESHOLD` (or the take is truncated), then stitch per chapter.
4. **Publish** (`app/tts.py`): combine chapters → `data/articles/<slug>/audio.mp3`,
   mark `published`. The local feed serves it.

## Install

Requires Python 3.10+ and `ffmpeg` on PATH (WAV→MP3). Henty runs **separately**
on a CUDA box; this app only needs to reach its HTTP API.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or requirements-dev.txt to run tests
python run.py
```

Open <http://127.0.0.1:8000>. The app binds to `127.0.0.1`; expose the feed with
a tunnel (below) — do not bind `0.0.0.0`.

## Configuration

Environment variables (each also settable in the Settings tab except the two
secrets, which are env-only):

| Variable                    | Purpose                                                        |
|-----------------------------|----------------------------------------------------------------|
| `BACKLOGCAST_HOST`/`PORT`   | bind host/port (default `127.0.0.1:8000`)                      |
| `BACKLOGCAST_DATA`          | data dir (default `./data`)                                    |
| `PUBLIC_BASE_URL`           | public URL used in the feed (your tunnel host)                |
| `LLOYDIO_BASE_URL`          | lloydio origin to poll for the podcast queue                  |
| `HENTY_BASE_URL`            | Henty API base (default `http://127.0.0.1:5000`)              |
| `HENTY_API_KEY`             | Henty `X-API-Key` (**secret, env-only**)                      |
| `HENTY_BOOKS_DIR`           | folder Henty scans for `book.json` projects                   |
| `HENTY_DIR`                 | Henty repo dir, to run `batch_import_books.py` (co-located)   |
| `DEFAULT_VOICE`             | Henty voice sample name (default `Haggard`)                   |
| `ASR_SIMILARITY_THRESHOLD`  | keep regenerating a chunk below this (default `0.85`)         |
| `ASR_MAX_RETRIES`           | max extra tries per chunk (default `4`)                       |

Config-only keys: `auto_approve`/`auto_publish` (default on) and
`lloydio_poll_seconds` (background queue poll; `0` disables).

## Where to run it (feed reachability)

Podcast apps (Pocket Casts) fetch the feed **from their own servers**, then the
phone downloads the audio — so the feed and `/audio` must be reachable from the
public internet via a tunnel:

- **Cloudflare Tunnel** — `cloudflared tunnel --url http://127.0.0.1:8000`
- **Tailscale Funnel** — `tailscale funnel 8000`
- **ngrok** — `ngrok http 8000`

Set `PUBLIC_BASE_URL` to the tunnel host so enclosure URLs are absolute.

**For reliable auto-download**, keep the (lightweight) feed server always-on
behind a persistent tunnel and let GPU synthesis run only when the Henty box is
up — the feed only serves already-made mp3s. If everything runs a few hours a
day, auto-download is best-effort; a manual pull-to-refresh always works while
the server is up.

## Subscribing

Settings → Feed tokens → copy a feed URL (`PUBLIC_BASE_URL/feed/<token>.xml`).
In **Pocket Casts**: profile → Files → URL, or "Add by URL" on desktop. Revoking
a token immediately blocks feed + audio for it. Privacy is obscurity-only (an
unguessable token); front the tunnel with access control if you want more.

## Henty integration notes

- Book import is Henty's offline CLI `batch_import_books.py` (no import API). The
  worker writes `HENTY_BOOKS_DIR/<slug>/book.json`, runs the importer (needs
  `HENTY_DIR`), then `POST /api/project/load`. If the orchestrator is not
  co-located with Henty, run the importer yourself and the loader will pick it up.
- The ASR loop uses only Henty's existing endpoints (`generate-chunk-audio`,
  `transcribe-take`, `set-chunk-best-take`, `stitch-best-takes`).
- **Optional Henty hardening PRs** (quality-of-life, not required — the upstream
  sanitizer here is the real fix): forward `seed`/`temperature` into
  `model.generate()` so retries are controllable/reproducible; give
  `batch_import_books.py` a `--folder` argument so one article imports at a time.

## Data layout

```
data/
  config.json
  tokens.json
  cover.jpg                         (optional channel artwork)
  articles/
    2026-05-26-the-bitter-lesson/
      meta.json                     (state, chunk_reports, audio info)
      raw.html
      article.md                    (extracted markdown)
      book.json                     (Henty input; here if HENTY_BOOKS_DIR unset)
      audio.mp3
```

## Development & tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests cover the parts that don't need a GPU/Henty/lloydio: the markdown
sanitizer + chunker (the garbling fix), the lloydio queue client, the Henty
client + ASR retry loop (mocked transport), and pipeline/state wiring. The
server isn't started with `reload=True`; restart it after backend changes.

## End-to-end verification (on the Henty box)

1. Point `LLOYDIO_BASE_URL`, `HENTY_BASE_URL`, `HENTY_API_KEY`,
   `HENTY_BOOKS_DIR`, `HENTY_DIR` at your real services.
2. Share a short article to lloydio (podcast checkbox), then
   `POST /api/ingest/lloydio` (or enable `lloydio_poll_seconds`).
3. Watch the job reach `stitched`; check `meta.json` `chunk_reports` — all
   `ok: true`, no `truncated`. Listen to `audio.mp3`; the garbling should be gone.
4. Start a tunnel, set `PUBLIC_BASE_URL`, validate `feed/<token>.xml`
   (castfeedvalidator.com), and subscribe in Pocket Casts.
