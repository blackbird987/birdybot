@echo off
:: Restart the Claude Bot
cd /d "%~dp0"
call stop_bot.bat
timeout /t 2 /nobreak >nul
call start_bot.bat
