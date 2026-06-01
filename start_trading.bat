@echo off
:: start_trading.bat — Windows trading session launcher
:: Starts Tailscale, FastAPI bridge, Bookmap, and TWS/IBKR
:: Run as Administrator for best results.

setlocal EnableDelayedExpansion
title FlowDesk — Trading Session Launcher

:: ===========================================================================
:: CONFIGURATION
:: ===========================================================================
set BOOKMAP_PATH=C:\Program Files\Bookmap\Bookmap.exe
set TWS_PATH=C:\Jts\tws.exe
set PYTHON_VENV=C:\trading\venv\Scripts\python.exe
set SERVER_SCRIPT=C:\trading\bookmap_server.py
set TAILSCALE_EXE=C:\Program Files\Tailscale\tailscale.exe
set LOG_DIR=C:\trading\logs

:: ===========================================================================

echo.
echo  =====================================================
echo   TRADING SESSION LAUNCHER
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
echo [2/4] Starting FastAPI bridge server on port 8766...
tasklist /FI "WINDOWTITLE eq BookmapServer" 2>NUL | find /I "cmd.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    echo      Bridge server already running.
) else (
    start "BookmapServer" /MIN cmd /k ^
        ""%PYTHON_VENV%" -m uvicorn bookmap_server:app ^
        --host 0.0.0.0 ^
        --port 8766 ^
        --app-dir "C:\trading" ^
        >> "%LOG_DIR%\bookmap_server.log" 2>&1"
    timeout /t 3 /nobreak >NUL
    :: Quick health check
    curl -s http://localhost:8766/health >NUL 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo      Bridge server  [OK]  http://localhost:8766
    ) else (
        echo      Bridge server  [WARN] Not responding yet — check %LOG_DIR%\bookmap_server.log
    )
)

:: ---------------------------------------------------------------------------
:: 3. Bookmap
:: ---------------------------------------------------------------------------
echo.
echo [3/4] Starting Bookmap...
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
echo [4/4] Starting TWS...
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
:: Summary
:: ---------------------------------------------------------------------------
echo.
echo  =====================================================
echo   STATUS SUMMARY
echo  =====================================================
echo   Tailscale IP  : !TS_IP!
echo   Bridge server : http://localhost:8766/health
echo   Bridge server : http://!TS_IP!:8766/health
echo   Logs          : %LOG_DIR%\bookmap_server.log
echo  =====================================================
echo.
echo  All services started. Press any key to close this window.
echo  (Services will continue running in the background.)
echo.
pause
endlocal
