@echo off
setlocal
echo Stopping Darbie worker processes...
taskkill /F /FI "WINDOWTITLE eq DARBIE*" 2>nul
taskkill /F /IM python.exe /FI "IMAGENAME eq python.exe" /FI "MEMUSAGE gt 10000" 2>nul
echo Done.
pause
