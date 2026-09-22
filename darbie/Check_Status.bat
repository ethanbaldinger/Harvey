@echo off
setlocal
cd /d "%~dp0"
title Remote Worker Status

where python >nul 2>nul
if %errorlevel% neq 0 (
    where py >nul 2>nul
    if %errorlevel% neq 0 (
        echo [ERROR] Python not found.
        pause
        exit /b 1
    )
    set "PY=py"
) else (
    set "PY=python"
)

%PY% worker.py --status
pause
