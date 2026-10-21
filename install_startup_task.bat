@echo off
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM  Auto-start BacklogCast (and Henty, via start.bat) at logon, so
REM  queued lloydio links get narrated whenever the desktop is on.
REM  Episodes are hosted by lloydio/R2, so the feed stays up when
REM  the desktop is off -- no tunnel needed.
REM ============================================================

schtasks /Create /F /SC ONLOGON /TN "BacklogCast" /TR "\"%~dp0start.bat\""
if errorlevel 1 (
    echo Failed to create the scheduled task.
) else (
    echo Scheduled task "BacklogCast" created -- it launches at logon.
    echo Remove it with:  schtasks /Delete /TN "BacklogCast" /F
)
pause
