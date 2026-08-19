"""Reads new messages for actual processing (Claude reading the protocol .md files,
fetching real data, applying judgment). Source of new messages is persistence.py's
A5 SQLite queue -- NOT Telegram's getUpdates directly.

Rebuilt 2026-07-09 alongside the rest of the Telegram delivery layer (event-driven
now, not cron-polled -- see persistence.py's module docstring). This script's own
job is unchanged from before: claim whatever's waiting and hand it to whoever's
running it (a live interactive session, or the automated `claude -p` round
triggered by bot/process_queue.py) to actually process.

Usage:
    python bot/check_telegram.py            -- claim + print new messages (marks them 'processing')
    python bot/check_telegram.py --peek      -- print pending/processing messages WITHOUT claiming new ones
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence


def get_new_messages(mark_seen: bool = True) -> list[dict]:
    """Returns updates ready for processing, in the same shape ack_listener.py
    originally queued (the full raw Telegram update dict). If mark_seen, reclaims
    any stale 'processing' rows first (a message stuck there past the timeout was a
    crash, not real ongoing work), then atomically claims every 'received' row --
    each returned message will not be claimed again by a concurrent caller.

    IMPORTANT for callers: claiming a message here only marks it 'processing', not
    'sent' -- call persistence.mark_sent(update_id, [...]) after Telegram confirms
    delivery, or persistence.mark_failed(update_id, error) if the run fails. A
    message left in 'processing' and never marked complete is exactly the case this
    module protects against (it gets reclaimed and retried, not silently lost)."""
    if mark_seen:
        persistence.reclaim_stale_processing()
        claimed = persistence.claim_next_messages()
    else:
        with persistence._db() as conn:  # peek-only, no state change
            rows = conn.execute(
                "SELECT * FROM messages WHERE status IN ('received', 'processing')"
            ).fetchall()
            claimed = [dict(r) for r in rows]
            import json
            for d in claimed:
                if d.get("raw_update"):
                    d["raw_update"] = json.loads(d["raw_update"])

    return [d["raw_update"] for d in claimed if d.get("raw_update")]


if __name__ == "__main__":
    mark_seen = "--peek" not in sys.argv
    updates = get_new_messages(mark_seen=mark_seen)
    print(f"{len(updates)} new update(s) found (mark_seen={mark_seen})")
    for u in updates:
        msg = u.get("message", {})
        has_photo = "photo" in msg
        print(f"update_id={u['update_id']} from={msg.get('from', {}).get('id')} "
              f"text={msg.get('text')!r} has_photo={has_photo} date={msg.get('date')}")
    if mark_seen and updates:
        print("\nNOTE: these are now 'processing' in trading_new.db -- call "
              "persistence.mark_sent(update_id, [...]) after Telegram delivery is "
              "confirmed, or persistence.mark_failed(update_id, error) on failure. "
              "Left as 'processing' past 10 minutes, they'll be automatically retried.")
