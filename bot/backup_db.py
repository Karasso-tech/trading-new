"""Daily off-machine backup of trading_new.db (2026-07-16).

Found in review: the entire trade ledger (thesis/positions/exits/closing_summaries)
lives in one SQLite file on one machine with no backup anywhere -- a bad disk, an
accidental delete, or a botched migration loses it permanently with no recovery
path. This is the fix: a plain daily snapshot into a Dropbox-synced folder, which
gets a copy off this machine automatically without needing a new account or
service.

Uses sqlite3's own backup() API rather than a raw file copy -- copying the file
directly while some other process might be mid-write (ack_listener.py is always
running) risks grabbing a torn/inconsistent snapshot; backup() takes a proper
consistent copy the same way the `.backup` CLI command does, safe to run against
a live, in-use database.

Usage: python bot/backup_db.py
  - Snapshots to BACKUP_DIR as trading_new_YYYYMMDD_HHMMSS.db
  - Deletes any snapshot in BACKUP_DIR older than RETENTION_DAYS afterward, so
    the folder doesn't grow forever
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "trading_new.db"
BACKUP_DIR = Path(r"C:\Users\USER\Dropbox\Trading Backup")
LOG_FILE = PROJECT_ROOT / "backup_db.log"
RETENTION_DAYS = 30


def _log(msg: str) -> None:
    """Prints only. Used to also append to LOG_FILE itself, which crashed every
    scheduled run (2026-08-04): the .bat launching this script already held that
    same file open via `>> backup_db.log`, and a Windows cmd redirect doesn't
    share the handle -- so this open() raised PermissionError on the line right
    after the copy had already succeeded. Result: a working backup that reported
    itself as a failed task, every night. bot/run_task.py is now the single
    owner of the log file and captures this stdout into it."""
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}")


def backup_once() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_path = BACKUP_DIR / f"trading_new_{stamp}.db"

    src = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()

    return dest_path


def prune_old_backups(retention_days: int = RETENTION_DAYS) -> int:
    cutoff = datetime.now().timestamp() - retention_days * 86400
    removed = 0
    for f in BACKUP_DIR.glob("trading_new_*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed


def main() -> None:
    try:
        dest_path = backup_once()
    except Exception as e:
        _log(f"FAILED: {e}")
        raise
    removed = prune_old_backups()
    _log(f"OK: backed up to {dest_path} ({dest_path.stat().st_size} bytes), pruned {removed} old backup(s)")
    print(f"Backed up to {dest_path}")


if __name__ == "__main__":
    main()
