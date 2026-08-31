@echo off
cd /d "%~dp0"
set TICKAVERAGER_SUPERVISOR=1

:runloop
.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8010
if errorlevel 43 goto :eof
if errorlevel 42 (
    echo   Restart requested - relaunching...
    timeout /t 2 >nul
    goto runloop
)
