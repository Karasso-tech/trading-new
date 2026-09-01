@echo off
REM Midday position pulse. Task currently disabled by the user (2026-08-04) -- kept so re-enabling is one click.
REM
REM Everything goes through run_task.py (2026-08-04): it owns the log file and
REM sends the Telegram heartbeat. The old `>> foo.log 2>&1` redirect is gone on
REM purpose -- cmd held that handle unshared, so any script that also opened its
REM own log died with PermissionError (this killed refresh_pending every night).
cd /d "%~dp0.."
python "%~dp0run_task.py" position_status_midday
