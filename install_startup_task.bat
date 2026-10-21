@echo off
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM  Auto-start BacklogCast (and Henty, via start.bat) at logon, so
REM  queued lloydio links get narrated whenever the desktop is on.
REM  Episodes are hosted by lloydio/R2, so the feed stays up when
REM  the desktop is off -- no tunnel needed.
REM
REM  Uses a shortcut in your Startup folder (no admin needed;
REM  `schtasks /SC ONLOGON` requires an elevated prompt).
REM ============================================================

set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BacklogCast.lnk"
powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);" ^
  "$s.TargetPath = '%~dp0start.bat'; $s.WorkingDirectory = '%~dp0';" ^
  "$s.WindowStyle = 7; $s.Description = 'BacklogCast + Henty'; $s.Save()"
if exist "%LNK%" (
    echo Startup shortcut created -- BacklogCast launches at logon.
    echo Remove it with:  del "%LNK%"
) else (
    echo Failed to create the startup shortcut.
)
pause
