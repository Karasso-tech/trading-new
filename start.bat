@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  Trading New -- Startup / Crash-Recovery
echo ============================================
echo Safe to run any time -- skips anything that is already running.
echo.

if not exist ".env" (
    echo No .env file found. Copy .env.example to .env and fill in your keys first.
    pause
    exit /b 1
)

echo [1/4] Checking Python dependencies...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo pip install failed -- see above.
    pause
    exit /b 1
)
playwright install chromium >nul 2>&1

echo [2/4] Checking TradingView (Chrome CDP on port 9222)...
curl -s -m 2 http://localhost:9222/json/version >nul 2>&1
if errorlevel 1 (
    echo   Not reachable -- launching it now, this can take ~10-20s...
    echo   (First run ever: log into TradingView in the window that opens.)
    start "" /min cmd /c "tradingview-mcp\scripts\launch_tv_debug.bat"
    timeout /t 3 /nobreak >nul
) else (
    echo   Already running -- OK.
)

echo [3/4] Checking Telegram listener (ack_listener.py)...
set "LISTENER_RUNNING="
set "OLDPID="
if exist "bot\.ack_listener.pid" (
    for /f %%p in (bot\.ack_listener.pid) do set "OLDPID=%%p"
    if defined OLDPID (
        tasklist /FI "PID eq !OLDPID!" | findstr /I "!OLDPID!" >nul
        if not errorlevel 1 set "LISTENER_RUNNING=1"
    )
)
if defined LISTENER_RUNNING (
    echo   Already running ^(PID !OLDPID!^) -- OK, not starting a second copy.
) else (
    echo   Not running -- starting it now.
    start "Trading New - ack_listener" /min python bot\ack_listener.py
    REM 2026-07-30 full-system checkup: this used to just print "Done" either
    REM way, even if the listener crashed immediately -- give it a moment,
    REM then actually check the PID file it's supposed to write on startup,
    REM same check used for OLDPID above.
    timeout /t 3 /nobreak >nul
    set "NEWPID="
    if exist "bot\.ack_listener.pid" (
        for /f %%p in (bot\.ack_listener.pid) do set "NEWPID=%%p"
    )
    set "NEWPID_RUNNING="
    if defined NEWPID (
        tasklist /FI "PID eq !NEWPID!" | findstr /I "!NEWPID!" >nul
        if not errorlevel 1 set "NEWPID_RUNNING=1"
    )
    if defined NEWPID_RUNNING (
        echo   Confirmed running ^(PID !NEWPID!^).
    ) else (
        echo   WARNING: listener does not appear to have started -- possible crash on startup ^(bad .env, etc^).
        echo   Try running "python bot\ack_listener.py" directly in a console to see the real error.
    )
)

echo [4/4] Scheduled tasks (watchdog, auto-monitor, position status, DB backup)...
echo   These run on their own via Windows Task Scheduler AS LONG AS YOU STAY
echo   LOGGED IN TO WINDOWS. If the PC just rebooted, logging in is the only
echo   manual step they need -- nothing else to start by hand.
echo.
echo Done. In Telegram, send /list or /positions to confirm the bot answers.
echo See STARTUP_PROTOCOL.md for the full crash-recovery checklist.
echo.
pause
