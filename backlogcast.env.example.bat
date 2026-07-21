@echo off
REM ===================================================================
REM  BacklogCast configuration
REM  On first run this is copied to backlogcast.env.bat (gitignored).
REM  Edit the values below; keep your Henty API key private.
REM ===================================================================

REM --- lloydio: where you share links; provides the podcast queue ---
set "LLOYDIO_BASE_URL=https://your-owner-lloydio-host"
REM Poll lloydio every N seconds for new podcast-tagged links (0 = manual only)
set "LLOYDIO_POLL_SECONDS=900"

REM --- Henty: local GPU TTS studio ---
set "HENTY_BASE_URL=http://127.0.0.1:5000"
set "HENTY_API_KEY=paste-your-Henty-API_KEY-here"
set "HENTY_BOOKS_DIR=C:\path\to\Henty\books"
set "HENTY_DIR=C:\path\to\Henty"
set "DEFAULT_VOICE=Haggard"

REM --- Public feed URL: your permanent Cloudflare Tunnel hostname ---
set "PUBLIC_BASE_URL=https://podcast.your-domain.com"

REM --- Optional TTS/ASR tuning ---
set "ASR_SIMILARITY_THRESHOLD=0.85"
set "ASR_MAX_RETRIES=4"
