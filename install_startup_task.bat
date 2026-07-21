@echo off
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM  Optional: auto-start BacklogCast at logon (always-on feed).
REM  Pairs with the always-on cloudflared service so your phone
REM  can auto-download episodes any time.
REM ============================================================

schtasks /Create /F /SC ONLOGON /TN "BacklogCast" /TR "\"%~dp0start_backlogcast.bat\""
if errorlevel 1 (
    echo Failed to create the scheduled task.
) else (
    echo Scheduled task "BacklogCast" created -- it launches at logon.
    echo Remove it with:  schtasks /Delete /TN "BacklogCast" /F
)
pause
