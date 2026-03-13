@echo off
:: Start Claude Bot as a background process (no console window)
:: PID is saved to data\bot.pid for stop_bot.bat

cd /d "%~dp0\.."
set BOT_DIR=%cd%

:: Check if already running
if exist "data\bot.pid" (
    set /p PID=<data\bot.pid
    tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
    if not errorlevel 1 (
        echo Bot is already running [PID %PID%].
        pause
        exit /b 1
    )
    del "data\bot.pid"
)

:: Ensure data directory exists
if not exist "data" mkdir data
if not exist "data\logs" mkdir data\logs

:: Start bot with pythonw (no console window)
:: Use 'start' without /B so it spawns a detached process that survives this script
start "" C:\Python312\pythonw.exe -c "import os; os.chdir(r'%BOT_DIR%'); import runpy; runpy.run_module('bot', run_name='__main__')"

:: Wait a moment for process to start, then find PID
timeout /t 3 /nobreak >nul

:: Get the PID of the pythonw process running our bot
for /f "tokens=2" %%i in ('wmic process where "commandline like '%%pythonw%%bot%%'" get processid /value 2^>nul ^| find "="') do set PID=%%i

if defined PID (
    echo %PID%> data\bot.pid
    echo Bot started [PID %PID%].
    echo Logs: data\logs\bot.log
) else (
    echo Bot started but could not capture PID.
    echo Check data\logs\bot.log for status.
)

timeout /t 3
