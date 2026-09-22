@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0..;%~dp0;%PYTHONPATH%"

if exist worker.py (
    python worker.py --config "%~dp0worker.json"
) else if exist darbie\worker.py (
    python -m darbie.worker --config "%~dp0darbie\worker.json"
) else (
    python -m darbie.worker --config "%~dp0worker.json"
)
pause
