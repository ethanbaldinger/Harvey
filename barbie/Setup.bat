@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0..;%~dp0;%PYTHONPATH%"

if exist setup_worker.py (
    python setup_worker.py
) else if exist barbie\setup_worker.py (
    python barbie\setup_worker.py
) else (
    python -B -m barbie.setup_worker
)
pause
