@echo off
setlocal
cd /d "%~dp0"
title Remote Worker Daemon

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [ERROR] Python was not found on this computer.
        echo Please install Python 3.10+ from https://www.python.org/downloads/
        echo Make sure to check "Add Python to PATH" during installation.
        pause
        exit /b 1
    )
    set "PY=py"
) else (
    set "PY=python"
)

if not exist worker.json (
    echo [INFO] First-time setup required. Launching setup wizard...
    %PY% setup_worker.py
    if not exist worker.json (
        echo [ERROR] Setup incomplete. Exiting.
        pause
        exit /b 1
    )
)

echo ====================================================================
echo Starting Merriam-Webster Worker Daemon...
echo Hard 1,000 daily call limit and 950 hit Hoover tripwire are active.
echo Press Ctrl+C at any time to pause or exit.
echo ====================================================================
echo.

%PY% worker.py --config worker.json
pause
