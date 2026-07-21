@echo off
setlocal enableextensions

REM ============================================================
REM  One-time: install cloudflared + connect a permanent tunnel
REM  so https://podcast.<your-domain> reaches this box on :8000.
REM ============================================================

REM --- self-elevate (installing a Windows service needs admin) ---
net session >nul 2>&1
if errorlevel 1 (
    echo Requesting administrator privileges...
    powershell -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)

echo.
echo === Step 1: install cloudflared (if missing) ===
where cloudflared >nul 2>&1
if errorlevel 1 (
    winget install --id Cloudflare.cloudflared --accept-source-agreements --accept-package-agreements
) else (
    echo cloudflared already installed.
)

echo.
echo === Step 2: create the tunnel in the Cloudflare dashboard ===
echo   1. Zero Trust ^> Networks ^> Tunnels ^> Create a tunnel ^> Cloudflared
echo   2. Name it "backlogcast", choose Windows, and COPY the token (starts with eyJ...).
echo.
set /p CF_TOKEN="Paste the tunnel token here: "
if "%CF_TOKEN%"=="" (
    echo No token entered. Re-run this script when you have it.
    pause
    exit /b 1
)

echo.
echo === Step 3: install cloudflared as an always-on service ===
cloudflared.exe service install %CF_TOKEN%

echo.
echo === Step 4 (back in the dashboard): add the Public Hostname ===
echo   Tunnel ^> Public Hostname ^> Add a public hostname:
echo     Subdomain: podcast     Domain: ^<your-domain^>
echo     Type: HTTP   URL: 127.0.0.1:8000
echo   DNS is auto-created. Do NOT attach a Cloudflare Access policy to this
echo   hostname -- the podcast feed must stay publicly fetchable.
echo.
echo Finally, set in backlogcast.env.bat:
echo     PUBLIC_BASE_URL=https://podcast.^<your-domain^>
echo.
echo Manage the service later:  sc query cloudflared ^| net stop cloudflared ^| net start cloudflared
pause
