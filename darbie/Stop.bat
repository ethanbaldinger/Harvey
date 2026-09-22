@echo off
setlocal
cd /d "%~dp0"
echo ====================================================================
echo Stopping Remote Worker Daemon...
echo ====================================================================

taskkill /FI "WINDOWTITLE eq Remote Worker Daemon*" /F >nul 2>nul
echo Done. If the worker window is still open, simply close it.
timeout /t 3 >nul
