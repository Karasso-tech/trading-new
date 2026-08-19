"""Where a single run's leftovers live, and when they are thrown away
(2026-08-09).

The problem, counted on 2026-08-09: the project root held **259 files**, and
**231 of them** were per-run payloads -- 97 `_decision_*`, 34 `_monitorall_*`,
25 `_automonitor_*`, 29 `_position*`, 13 `_report_*`, 17 `scratch_*`. About
50 MB. Every one of them is regenerated on the next run and none is source.

Git already ignores all of it, so the repository was never polluted; the cost
was entirely to the human. When nine files in ten are noise, the one that
matters is invisible -- and the owner's own summary of this project was "there
are a lot of things that are relevant and things that are not and I can't see
which".

Two rules, and they are deliberately simple:

  1. A run writes its payload under `_runs/`, never the project root.
  2. Anything in `_runs/` older than KEEP_DAYS is deleted, automatically, by
     whichever scheduled job runs next.

Deleting on a schedule rather than at the end of each run is on purpose. A
payload's most valuable hour is the one right after a failure, when the run
that wrote it has just gone wrong and the file is the only evidence of what it
was thinking. A week is long enough to still be looking; a month is long enough
to be back where we started.

Logs get the same treatment for the same reason: `ack_listener.log` had reached
**38 MB**, which is a log nobody will ever open.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = PROJECT_ROOT / "_runs"

# One week. See the module docstring for why this is not "delete on exit".
KEEP_DAYS = 7

# Rotate a log once it passes this. 5 MB is roughly a month of normal traffic
# for the noisiest of them and still opens instantly in any editor.
LOG_MAX_BYTES = 5 * 1024 * 1024
# How many rotated generations to keep. Two is enough to cover "it broke
# overnight and rolled over while I was asleep" without unbounded growth.
LOG_KEEP_ROTATIONS = 2

# The prefixes a run payload can have. Used by the one-time tidy of the files
# already sitting in the root -- new runs write straight into RUNS_DIR.
RUN_FILE_PREFIXES = (
    "_decision_", "_monitorall_", "_automonitor_", "_position_status_",
    "_positions_status_", "_positionstatus_", "_positions_eod_", "_playbook_",
    "_report_", "scratch_", "_rescan_", "_shadow_backfill",
)


def ensure_runs_dir() -> Path:
    """RUNS_DIR, created if it is not there yet. Safe to call on every run."""
    RUNS_DIR.mkdir(exist_ok=True)
    return RUNS_DIR


def run_path(filename: str) -> Path:
    """Where a run payload with this name belongs."""
    return ensure_runs_dir() / filename


def _is_run_file(path: Path) -> bool:
    return path.is_file() and any(path.name.startswith(p) for p in RUN_FILE_PREFIXES)


def sweep(keep_days: int = KEEP_DAYS, dry_run: bool = False) -> list[Path]:
    """Delete run payloads older than keep_days. Returns what was (or would be)
    removed.

    Only ever looks inside RUNS_DIR, and only at files whose name matches a
    known run prefix. Both limits are deliberate: a sweep that can reach the
    project root is one bad path away from deleting source, and this runs
    unattended on a schedule where nobody is watching."""
    if not RUNS_DIR.exists():
        return []
    cutoff = time.time() - keep_days * 86400
    removed = []
    for path in RUNS_DIR.iterdir():
        if not _is_run_file(path):
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            if not dry_run:
                path.unlink()
            removed.append(path)
        except OSError:
            # A file held open by another process is skipped, not fatal -- the
            # next sweep gets it. Losing a cleanup pass costs disk space;
            # raising here would cost a scheduled job its actual work.
            continue
    return removed


def rotate_log(path: Path, max_bytes: int = LOG_MAX_BYTES,
                keep: int = LOG_KEEP_ROTATIONS) -> bool:
    """Roll `path` to `path.1` once it grows past max_bytes, shifting older
    generations along and dropping the oldest. Returns True if it rotated.

    Rotation rather than truncation: the moment a log gets too big is often the
    moment something has been going wrong repeatedly, and that is the worst
    possible time to throw the evidence away."""
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    try:
        oldest = path.with_suffix(path.suffix + f".{keep}")
        if oldest.exists():
            oldest.unlink()
        for n in range(keep - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{n}")
            if src.exists():
                src.replace(path.with_suffix(path.suffix + f".{n + 1}"))
        path.replace(path.with_suffix(path.suffix + ".1"))
        return True
    except OSError:
        # Windows will refuse to rename a file another process holds open --
        # the ack listener holds its own log for its whole life. A failed
        # rotation is a big log, not a broken bot.
        return False


def rotate_all_logs(log_dir: Path = PROJECT_ROOT) -> list[Path]:
    """Rotate every oversized *.log in the project root. Returns the rotated
    ones."""
    rotated = []
    for path in log_dir.glob("*.log"):
        if rotate_log(path):
            rotated.append(path)
    return rotated


def tidy_root(dry_run: bool = False) -> list[Path]:
    """Move run payloads that are sitting in the project root into RUNS_DIR.

    This is the one-time catch-up for the 231 files that accumulated before
    RUNS_DIR existed, and a safety net afterwards for anything that still
    writes to the old place. Moves rather than deletes -- the sweep above
    decides when something is old enough to lose, and it should be the only
    thing that ever decides that."""
    moved = []
    ensure_runs_dir()
    for path in PROJECT_ROOT.iterdir():
        if not _is_run_file(path):
            continue
        target = RUNS_DIR / path.name
        try:
            if not dry_run:
                if target.exists():
                    target.unlink()
                path.replace(target)
            moved.append(path)
        except OSError:
            continue
    return moved


def housekeeping(dry_run: bool = False) -> dict:
    """Everything above, in the order that makes sense, for a scheduled job to
    call once. Never raises: housekeeping failing must not fail the job it is
    attached to."""
    result = {"moved": [], "swept": [], "rotated": []}
    try:
        result["moved"] = tidy_root(dry_run=dry_run)
        result["swept"] = sweep(dry_run=dry_run)
        if not dry_run:
            result["rotated"] = rotate_all_logs()
    except Exception:
        pass
    return result


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="tidy the project root's run payloads")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = housekeeping(dry_run=args.dry_run)
    verb = "would move" if args.dry_run else "moved"
    print(f"{verb} {len(result['moved'])} run payloads into {RUNS_DIR.name}/")
    verb = "would delete" if args.dry_run else "deleted"
    print(f"{verb} {len(result['swept'])} payloads older than {KEEP_DAYS} days")
    print(f"rotated {len(result['rotated'])} oversized logs")


if __name__ == "__main__":
    _main()
