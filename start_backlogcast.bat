@echo off
setlocal enableextensions
cd /d "%~dp0"

REM ============================================================
REM  BacklogCast one-click launcher (Windows / Henty box)
REM  Double-click to run. First run installs the venv + deps.
REM ============================================================

REM --- 1) local config (API keys, URLs) ---
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

REM --- 2) first-run setup: venv + dependencies ---
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    call ".venv\Scripts\activate.bat"
    echo Installing dependencies...
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call ".venv\Scripts\activate.bat"
)

REM --- 3) make sure Henty (GPU) is up, if configured ---
if defined HENTY_DIR call :ensure_henty

REM --- 4) launch BacklogCast ---
echo.
echo Starting BacklogCast ... UI at http://127.0.0.1:8000/
start "" http://127.0.0.1:8000/
python run.py
goto :eof

:ensure_henty
curl -s -o nul --max-time 3 http://127.0.0.1:5000/api/status
if not errorlevel 1 goto :eof
echo Starting Henty (GPU) ...
start "Henty" /d "%HENTY_DIR%" cmd /c start_henty_fresh.bat
echo Waiting for Henty on port 5000 ...
:wait_henty
timeout /t 3 /nobreak >nul
curl -s -o nul --max-time 3 http://127.0.0.1:5000/api/status
if errorlevel 1 goto wait_henty
echo Henty is up.
goto :eof
