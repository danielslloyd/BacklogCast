# BacklogCast — Claude Development Notes

## Python Environment — Defers to Henty

BacklogCast shares the system Python 3.11 environment with Henty
(`C:\Users\danie\Desktop\Git\Henty`). **Henty takes precedence.**

### Rules for this project

- **Never install torch, torchvision, torchaudio, numpy, scipy, or
  chatterbox-tts** from BacklogCast's startup scripts or requirements.txt.
  Those packages are installed and versioned by Henty; touching them risks
  breaking Henty's TTS pipeline.
- **Never create a `.venv`** for BacklogCast. Both apps share system Python 3.11
  at `C:\Users\danie\AppData\Local\Programs\Python\Python311\python.exe`.
- BacklogCast's `requirements.txt` must stay limited to web-layer deps
  (FastAPI, uvicorn, trafilatura, etc.) that don't conflict with Henty.
- If a dependency update is needed, check it against Henty's locked versions
  first (see `C:\Users\danie\Desktop\Git\Henty\DEPENDENCIES.md`).

### Henty's locked versions (as of 2026-05-28)

| Package         | Version           |
|-----------------|-------------------|
| Python          | 3.11.0            |
| torch           | 2.12.0+cu130      |
| torchaudio      | 2.11.0+cu130      |
| torchvision     | 0.27.0+cu130      |
| numpy           | 2.2.6 (must be <2.3) |
| scipy           | 1.16.3            |
| chatterbox-tts  | 0.1.7             |
| CUDA            | 13.0              |

## Running the app

```
start.bat          # the only launcher -- double-click, or run from cmd
```

`start.bat` is the single entrypoint. It:
1. Loads `backlogcast.env.bat` (gitignored; copied from
   `backlogcast.env.example.bat` on first run) — API keys, paths, port,
   public URL
2. Frees `BACKLOGCAST_PORT` (default 9157), then installs web deps only via
   `py -3.11 -m pip install -r requirements.txt`
3. Starts Henty (GPU TTS) if `HENTY_DIR` is set and it isn't already answering
4. Opens the UI and starts the server via `py -3.11 run.py`

Secrets belong in `backlogcast.env.bat`, never in `data\config.json`.
`HENTY_API_KEY` in particular is read from the environment only — its value is
Henty's own `API_KEY` (see `Henty\.env`).

`install_startup_task.bat` optionally registers `start.bat` to run at logon.

Password is stored in `data\password.txt` (created on first run, default
`BacklogCast2024`). Override with `BACKLOGCAST_PASSWORD` env var. The admin UI
and every `/api/*` route require it via HTTP Basic auth (enforced by
`require_admin_auth` middleware in `app/main.py`). The podcast surface —
`/feed/{token}.xml`, `/audio/...`, and `/cover.jpg` — stays public so podcast
clients can fetch it; those URLs are guarded by the per-feed token instead.

## Architecture

- `app/main.py` — FastAPI app, auth middleware, all HTTP endpoints
- `app/tts.py` — Chatterbox TTS integration, background worker
- `app/articles.py` — article storage (slug-based flat files under `data/`)
- `app/extractor.py` — URL fetch + HTML extraction (trafilatura / readability)
- `app/feed.py` — RSS/podcast feed generation
- `app/config.py` — config + API token management
- `static/` — single-page UI (vanilla JS)
- `run.py` — uvicorn entrypoint
