@echo off
setlocal enableextensions enabledelayedexpansion
cd /d "%~dp0"

REM ============================================================
REM  BacklogCast launcher (Windows / Henty box)
REM  Double-click to run. This is the ONLY launcher.
REM
REM  BacklogCast uses Henty's system Python 3.11 environment.
REM  Do NOT create a .venv and do NOT install torch, numpy, or
REM  chatterbox here -- those packages are owned by Henty.
REM ============================================================

REM --- 1) local config: API keys, paths, port, public URL ---
if not exist "backlogcast.env.bat" (
    echo First run: creating backlogcast.env.bat from the template.
    copy /y "backlogcast.env.example.bat" "backlogcast.env.bat" >nul
    echo.
    echo   Fill in backlogcast.env.bat ^(Henty API key, paths, PUBLIC_BASE_URL^),
    echo   then run this script again.
    start "" notepad "backlogcast.env.bat"
    pause
    exit /b 1
)
call "backlogcast.env.bat"

REM --- 2) defaults for anything the env file didn't set ---
if "%BACKLOGCAST_PORT%"=="" set BACKLOGCAST_PORT=9157
if "%HENTY_BASE_URL%"=="" set HENTY_BASE_URL=http://127.0.0.1:5000
if "%CHATTERBOX_DEVICE%"=="" set CHATTERBOX_DEVICE=cuda

REM --- 3) admin password: guards the UI + /api/*. The podcast feed,
REM        audio, and cover stay public (gated by their own token). ---
if "%BACKLOGCAST_PASSWORD%"=="" (
    if exist "data\password.txt" (
        set /p BACKLOGCAST_PASSWORD=<data\password.txt
    )
    if "!BACKLOGCAST_PASSWORD!"=="" (
        if not exist "data" mkdir data
        echo BacklogCast2024>data\password.txt
        set BACKLOGCAST_PASSWORD=BacklogCast2024
        echo [BacklogCast] Default password set. Edit data\password.txt to change it.
    )
)

REM --- 4) verify Python 3.11 (must come from Henty's install) ---
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python 3.11 not found.
    echo BacklogCast requires Henty to be installed first.
    echo See: C:\Users\danie\Desktop\Git\Henty
    pause
    exit /b 1
)

REM --- 5) free the port, then install web deps only ---
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort %BACKLOGCAST_PORT% -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

py -3.11 -m pip install -r requirements.txt -q
if errorlevel 1 ( echo ERROR: Web deps install failed. & pause & exit /b 1 )

REM --- 6) make sure Henty (GPU TTS) is up, if configured ---
if defined HENTY_DIR call :ensure_henty

REM --- 7) launch ---
echo.
echo ==========================================
echo   Local UI : http://127.0.0.1:%BACKLOGCAST_PORT%
echo   Public   : %PUBLIC_BASE_URL%
echo   Password : %BACKLOGCAST_PASSWORD%
echo ==========================================
echo.
start "" http://127.0.0.1:%BACKLOGCAST_PORT%/
py -3.11 run.py

echo.
echo Server stopped.
pause
goto :eof

:ensure_henty
curl -s -o nul --max-time 3 %HENTY_BASE_URL%/api/status
if not errorlevel 1 goto :eof
echo Starting Henty (GPU) ...
start "Henty" /d "%HENTY_DIR%" cmd /c start_henty_fresh.bat
echo Waiting for Henty on %HENTY_BASE_URL% ...
:wait_henty
timeout /t 3 /nobreak >nul
curl -s -o nul --max-time 3 %HENTY_BASE_URL%/api/status
if errorlevel 1 goto wait_henty
echo Henty is up.
goto :eof
