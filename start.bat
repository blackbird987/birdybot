@echo off
cd /d "%~dp0"

echo Killing existing Python bot instances...
taskkill /F /IM pythonw.exe >nul 2>&1
taskkill /F /IM python.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo Starting bot in background...
start /B pythonw -m bot
echo Bot started. You can close this window.
timeout /t 3 /nobreak >nul
