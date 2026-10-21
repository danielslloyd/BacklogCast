@echo off
REM ===================================================================
REM  BacklogCast configuration -- TEMPLATE
REM  On first run start.bat copies this to backlogcast.env.bat
REM  (gitignored). Put real values there, never in this file.
REM ===================================================================

REM --- lloydio: where you share links; provides the podcast queue ---
set "LLOYDIO_BASE_URL=https://your-owner-lloydio-host"
REM Poll lloydio every N seconds for new podcast-tagged links (0 = manual only)
set "LLOYDIO_POLL_SECONDS=900"
REM Write-back: finished episodes are POSTed to lloydio's /api/episode (mp3 -> R2,
REM text -> src/content/articles). Same value as EPISODE_TOKEN in the Cloudflare
REM Pages project. Env-only secret, never stored in data\config.json.
set "LLOYDIO_EPISODE_TOKEN=paste-your-EPISODE_TOKEN-here"

REM --- Claude extraction: headless `claude -p` cleans each page/PDF into the
REM     reading text. always | fallback (PDFs + weak drafts only) | off ---
set "CLAUDE_EXTRACT=always"
set "CLAUDE_MODEL=sonnet"
REM Optional: full path to the claude CLI if it isn't on PATH for the logon task
REM set "CLAUDE_BIN=C:\Users\you\AppData\Roaming\npm\claude.cmd"

REM --- Henty: local GPU TTS studio ---
REM HENTY_API_KEY is the value of API_KEY in Henty's .env. It is read from
REM the environment ONLY and is never persisted to data\config.json.
set "HENTY_BASE_URL=http://127.0.0.1:5000"
set "HENTY_API_KEY=paste-your-Henty-API_KEY-here"
REM Where BacklogCast writes book.json. Use its OWN folder, NOT Henty's books
REM library -- Henty never scans this dir (it gets absolute paths via --folder
REM and /api/project/load), so keeping them separate avoids cluttering your
REM book library with podcast articles. MUST be absolute: the importer runs
REM with cwd=HENTY_DIR and calls os.path.abspath() on this.
set "HENTY_BOOKS_DIR=C:\path\to\BacklogCast\data\books"
set "HENTY_DIR=C:\path\to\Henty"
set "DEFAULT_VOICE=Haggard"

REM --- Public feed URL: your permanent Cloudflare Tunnel hostname ---
set "PUBLIC_BASE_URL=https://podcast.your-domain.com"

REM --- Local port BacklogCast listens on. Your Cloudflare Tunnel's public
REM     hostname must point at 127.0.0.1 on THIS exact port. ---
set "BACKLOGCAST_PORT=9157"

REM --- Optional TTS/ASR tuning ---
set "ASR_SIMILARITY_THRESHOLD=0.85"
set "ASR_MAX_RETRIES=4"
