@echo off
title TickAverager - Alpaca  (close this window to stop the dashboard)
cd /d "%~dp0"

REM --- already running? just bring the dashboard up, don't start a second one
netstat -ano | findstr "127.0.0.1:8010" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   Dashboard is already running - opening it.
    start "" "http://127.0.0.1:8010"
    timeout /t 2 >nul
    exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   ERROR: no .venv found in "%CD%"
    echo   The bot does not appear to be installed here.
    echo.
    pause
    exit /b 1
)

echo.
echo   ================================================
echo     TickAverager Fleet  -  Alpaca paper account
echo     Dashboard:  http://127.0.0.1:8010
echo   ================================================
echo.
echo   Every ladder always starts STOPPED.
echo   In the dashboard open a ticker, press "Start",
echo   then "Arm live orders" - per ticker - before
echo   anything trades. Arming one arms only that one.
echo.
echo   Closing this window shuts the dashboard down.
echo   Take-profits resting at Alpaca stay live either way.
echo.

REM --- open the browser once the server has had a moment to bind
start "" /min powershell -NoProfile -Command "Start-Sleep 4; Start-Process 'http://127.0.0.1:8010'"

REM --- tells the app a supervisor is watching, which is what enables the
REM     dashboard's Restart button. Without it the button refuses to run,
REM     rather than exiting and leaving nothing to bring the server back.
set TICKAVERAGER_SUPERVISOR=1

:runloop
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8010

REM  "if errorlevel N" means "exit code >= N", so 43 is checked first to
REM  isolate exactly 42 -- the code /api/restart exits with.
if errorlevel 43 goto stopped
if errorlevel 42 (
    echo.
    echo   Restart requested from the dashboard - relaunching...
    echo.
    timeout /t 2 >nul
    goto runloop
)

:stopped
echo.
echo   Dashboard stopped.
pause
