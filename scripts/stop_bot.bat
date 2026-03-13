@echo off
:: Stop the Claude Bot background process

cd /d "%~dp0\.."

if not exist "data\bot.pid" (
    echo No PID file found. Trying to find bot process...
    wmic process where "commandline like '%%pythonw%%-m bot%%'" get processid 2>nul

    for /f "tokens=2" %%i in ('wmic process where "commandline like "%%pythonw%%-m bot%%"" get processid /value 2^>nul ^| find "="') do (
        echo Killing process %%i...
        taskkill /F /PID %%i >nul 2>nul
        echo Bot stopped.
        goto :done
    )
    echo Bot does not appear to be running.
    goto :done
)

set /p PID=<data\bot.pid

:: Check if process exists
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if errorlevel 1 (
    echo Bot is not running (stale PID %PID%).
    del "data\bot.pid"
    goto :done
)

echo Stopping bot (PID %PID%)...
taskkill /F /PID %PID% >nul 2>nul
del "data\bot.pid"
echo Bot stopped.

:done
timeout /t 2
