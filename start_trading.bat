@echo off
:: start_trading.bat â€” Windows trading session launcher
:: Starts Tailscale, FastAPI bridge, Bookmap, and TWS/IBKR
:: Run as Administrator for best results.

setlocal EnableDelayedExpansion
title FlowDesk â€” Trading Session Launcher

:: ===========================================================================
:: CONFIGURATION
:: ===========================================================================
set BOOKMAP_PATH=C:\Program Files\Bookmap\Bookmap.exe
set TWS_PATH=C:\Jts\tws.exe
set PYTHON_VENV=C:\trading\venv\Scripts\python.exe
set SERVER_SCRIPT=C:\trading\bookmap_server.py
set TAILSCALE_EXE=C:\Program Files\Tailscale\tailscale.exe
set CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
set DASHBOARD_PATH=C:\trading\flowdesk.html
set TAILSCALE_IP=127.0.0.1
set LOG_DIR=C:\trading\logs

:: ===========================================================================

echo.
echo  =====================================================
echo   FlowDesk v1.0 â€” Trading Session Launcher
echo  =====================================================
echo.

:: --- Ensure log directory exists ---
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

:: ---------------------------------------------------------------------------
:: 1. Tailscale
:: ---------------------------------------------------------------------------
echo [1/4] Checking Tailscale...
tasklist /FI "IMAGENAME eq tailscale.exe" 2>NUL | find /I "tailscale.exe" >NUL
if %ERRORLEVEL% NEQ 0 (
    echo      Starting Tailscale...
    start "" "%TAILSCALE_EXE%" up
    timeout /t 5 /nobreak >NUL
) else (
    echo      Tailscale already running.
)

:: Show Tailscale IP
for /f "tokens=*" %%i in ('"%TAILSCALE_EXE%" ip -4 2^>NUL') do set TS_IP=%%i
if defined TS_IP (
    echo      Tailscale IP: !TS_IP!
) else (
    echo      WARNING: Could not read Tailscale IP. Check Tailscale is logged in.
)

:: ---------------------------------------------------------------------------
:: 2. FastAPI Bridge Server
:: ---------------------------------------------------------------------------
echo.
echo [2/5] Starting FlowDesk server on port 8766...
tasklist /FI "WINDOWTITLE eq FlowDesk Server" 2>NUL | find /I "cmd.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    echo      Server already running.
) else (
    start "FlowDesk Server" /MIN cmd /k ^
        ""%PYTHON_VENV%" -m uvicorn bookmap_server:app ^
        --host 0.0.0.0 ^
        --port 8766 ^
        --app-dir "C:\trading" ^
        >> "%LOG_DIR%\flowdesk_server.log" 2>&1"
    timeout /t 3 /nobreak >NUL
    :: /ping connectivity check (lightweight, no HTML parsing needed)
    curl -s http://localhost:8766/ping >NUL 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo      FlowDesk Server  [LIVE]  http://localhost:8766
    ) else (
        echo      FlowDesk Server  [WARN]  Not responding yet â€” check %LOG_DIR%\flowdesk_server.log
    )
)

:: ---------------------------------------------------------------------------
:: 3. Bookmap
:: ---------------------------------------------------------------------------
echo.
echo [3/5] Starting Bookmap...
tasklist /FI "IMAGENAME eq Bookmap.exe" 2>NUL | find /I "Bookmap.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    echo      Bookmap already running.
) else (
    if exist "%BOOKMAP_PATH%" (
        start "" "%BOOKMAP_PATH%"
        echo      Bookmap launched.
    ) else (
        echo      WARNING: Bookmap not found at %BOOKMAP_PATH%
        echo      Edit BOOKMAP_PATH in this script to match your installation.
    )
)

:: ---------------------------------------------------------------------------
:: 4. TWS / IBKR
:: ---------------------------------------------------------------------------
echo.
echo [4/5] Starting TWS...
tasklist /FI "IMAGENAME eq tws.exe" 2>NUL | find /I "tws.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    echo      TWS already running.
) else (
    if exist "%TWS_PATH%" (
        start "" "%TWS_PATH%"
        echo      TWS launched.
    ) else (
        echo      WARNING: TWS not found at %TWS_PATH%
        echo      Edit TWS_PATH in this script to match your installation.
    )
)

:: ---------------------------------------------------------------------------
:: 5. Open FlowDesk dashboard in Chrome
:: ---------------------------------------------------------------------------
echo.
echo [5/5] Opening FlowDesk dashboard...
if exist "%CHROME_PATH%" (
    start "" "%CHROME_PATH%" "file:///%DASHBOARD_PATH:\=/%"
    echo      Dashboard opened in Chrome.
) else (
    echo      WARNING: Chrome not found at %CHROME_PATH%
    echo      Edit CHROME_PATH or open %DASHBOARD_PATH% manually.
)

:: ---------------------------------------------------------------------------
:: Summary
:: ---------------------------------------------------------------------------
echo.
echo  =====================================================
echo   FlowDesk v1.0 â€” STARTUP SUMMARY
echo  =====================================================
echo   Tailscale IP  : !TS_IP!
echo   Server LIVE   : http://localhost:8766/ping
echo   Health page   : http://!TS_IP!:8766/health
echo   Dashboard     : %DASHBOARD_PATH%
echo   Logs          : %LOG_DIR%\
echo  =====================================================
echo.
echo  All services started. Press any key to close this window.
echo  (All background services continue running.)
echo.
pause
endlocal

