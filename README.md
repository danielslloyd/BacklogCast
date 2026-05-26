# BacklogCast

Turn a personal reading backlog into a private podcast feed. Local web app, no
build step, files on disk. Pipeline:

```
URL → cleaned Markdown → preview/edit → TTS (Chatterbox) → RSS feed
```

The whole thing is a single FastAPI process: UI, JSON API, background TTS
worker, RSS feed, and audio streaming all on one port.

## Install

Requires Python 3.10+ and (for TTS) a working [Chatterbox](https://github.com/resemble-ai/chatterbox) install in the same environment. The app
also benefits from `ffmpeg` on PATH (for WAV → MP3 transcoding).

```bash
git clone https://github.com/danielslloyd/backlogcast.git
cd backlogcast
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
# Install Chatterbox however the project recommends; example:
pip install chatterbox-tts
```

## Run

```bash
python run.py
```

Open <http://127.0.0.1:8000>. The app binds to `127.0.0.1` by default, which is
the right choice — the UI has no auth. If you want to expose the feed to
friends, put a tunnel in front (see below); do **not** add `0.0.0.0` and call it
a day.

Environment variables:

| Variable                     | Purpose                                                                    |
|------------------------------|----------------------------------------------------------------------------|
| `BACKLOGCAST_HOST`           | bind host, default `127.0.0.1`                                             |
| `BACKLOGCAST_PORT`           | bind port, default `8000`                                                  |
| `BACKLOGCAST_DATA`           | data dir, default `./data`                                                 |
| `PUBLIC_BASE_URL`            | public URL used in the RSS feed (e.g. `https://my.tunnel.example`)         |
| `CHATTERBOX_DEVICE`          | `cuda` (default) or `cpu`                                                  |
| `BACKLOGCAST_CHUNK_CHARS`    | TTS chunk target size in characters, default `1200`                        |

`PUBLIC_BASE_URL` can also be set in the Settings tab.

## Data layout

Everything lives under `./data/`:

```
data/
  config.json
  tokens.json
  cover.jpg                       (optional, channel artwork; drop one in here)
  articles/
    2026-05-26-the-bitter-lesson/
      meta.json
      raw.html
      article.md
      audio.mp3
```

`tokens.json` is bootstrapped with one `default` token on first run. Manage
tokens from the Settings tab. Revoking a token immediately blocks both feed
and audio access for it.

## Exposing the feed to friends

The app has no built-in auth. Expose the port through a tunnel that does its
own auth or rate-limiting:

- **Cloudflare Tunnel** — `cloudflared tunnel --url http://127.0.0.1:8000`
- **Tailscale Funnel** — `tailscale funnel 8000`
- **ngrok** — `ngrok http 8000`

Set `PUBLIC_BASE_URL` (env var or Settings) to the public hostname so the RSS
feed emits absolute URLs that podcast apps can fetch.

## Subscribing

1. Go to **Settings** → **Feed tokens**, copy a feed URL.
2. In **Pocket Casts**: profile → Files → URL → paste; or use "Add by URL"
   from the desktop site. In **Apple Podcasts (desktop)**: File → "Subscribe to
   a Show…" → paste URL. iOS doesn't expose a manual-URL flow; use Pocket
   Casts or [Apple's subscribe link helper](https://podcasters.apple.com/support/828-podcast-link).

## Chatterbox integration

`app/tts.py` tries two invocation paths, in order:

1. **Python import** — `from chatterbox.tts import ChatterboxTTS`. If the
   import works, the worker synthesizes in-process.
2. **CLI subprocess** — falls back to `chatterbox --text-file <f> --output <f>`
   if the binary is on `PATH`.

If neither works the article is marked `failed` with an error like
"Chatterbox not available". Install Chatterbox into the same venv, or put its
CLI on `PATH`, and use the "retry" button on the article.

Long articles are chunked on paragraph boundaries (target 1200 chars; tune via
`BACKLOGCAST_CHUNK_CHARS`). Chunks are synthesized as WAV, concatenated with
the stdlib `wave` module, and transcoded to MP3 with `ffmpeg` if available.
Without `ffmpeg` the feed serves WAV (works in podcast apps but huge).

## Validating the feed

Use [Cast Feed Validator](https://castfeedvalidator.com/) (or the Apple
Podcasts submission flow) on your `PUBLIC_BASE_URL/feed/<token>.xml` before
sharing with friends. The XML template covers the required iTunes tags
(`itunes:author`, `itunes:owner`, `itunes:category`, `itunes:explicit`,
per-item `itunes:duration`, etc.). If validation fails, the most common cause
is a missing `PUBLIC_BASE_URL` so the enclosure URLs end up pointing at
`127.0.0.1`.

## Implementation notes

- States: `fetched → needs_review → approved → synthesizing → ready → published`
  (plus `failed`). Transitions happen on explicit UI actions, except
  `synthesizing → ready|failed` which is driven by the worker.
- Storage is plain files. No database. Listing/filtering scans the
  `data/articles/*/meta.json` set; that's fine for hundreds of articles.
- Audio queue is just a scan for `state == "approved"`, sorted by
  `approved_at`. No separate queue file. The worker is woken up by a kick on
  approve/retry; otherwise it polls every 5s.
- No retries on TTS failure — explicit "retry" button only. Failure mode is
  surfaced in the article's `error` field and the preview pane.
- Feed contains only `published` items, newest first. Unpublishing flips back
  to `ready` without deleting the audio.

## Development

```bash
pip install -r requirements.txt
python run.py
```

Then edit files; the UI is static so just refresh the browser. The server
isn't started with `reload=True` (avoids weird interactions with the TTS
worker thread); restart it after backend changes.
