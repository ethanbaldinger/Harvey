@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0..;%~dp0;%PYTHONPATH%"

if exist worker.py (
    python worker.py --config "%~dp0worker.json"
) else if exist barbie\worker.py (
    python -m barbie.worker --config "%~dp0barbie\worker.json"
) else (
    python -m barbie.worker --config "%~dp0worker.json"
)
pause
