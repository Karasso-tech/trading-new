@echo off
REM Late fallback for the post-close /monitorall sweep (2026-08-08).
REM
REM The real post-close sweep is no longer on a clock at all: refresh_pending.py
REM chains it once its rebuilds have landed, so the last list of the night is a
REM scan of the refreshed theses instead of the pre-refresh numbers. This job
REM only covers the night the refresh dies before it gets there -- it checks
REM whether a /monitorall already ran after today's close and no-ops if so.
REM
REM Everything goes through run_task.py (2026-08-04): it owns the log file and
REM sends the Telegram heartbeat. The old `>> foo.log 2>&1` redirect is gone on
REM purpose -- cmd held that handle unshared, so any script that also opened its
REM own log died with PermissionError (this killed refresh_pending every night).
cd /d "%~dp0.."
python "%~dp0run_task.py" automonitor_close_late
