@echo off
setlocal
cd /d "%~dp0"
title Remote Worker Setup Wizard

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

%PY% setup_worker.py
pause
