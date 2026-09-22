@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0..;%~dp0;%PYTHONPATH%"

if exist setup_worker.py (
    python setup_worker.py
) else if exist darbie\setup_worker.py (
    python darbie\setup_worker.py
) else (
    python -m darbie.setup_worker
)
pause
