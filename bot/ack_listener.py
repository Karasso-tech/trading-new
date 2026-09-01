"""Always-on Telegram listener -- the ONLY thing that ever calls Telegram's getUpdates.

Runs as a standalone, detached OS process (started once, independent of any Claude Code
session) so it keeps polling even while Claude isn't actively running. Its job: ack a
message instantly, durably queue it, and (2026-07-09 rebuild) immediately trigger
bot/process_queue.py to actually act on it -- event-driven, never a fixed-interval
timer (see persistence.py's module docstring for why a timer was tried and abandoned:
financially non-viable at any interval, and only fires while the harness is idle
anyway). process_queue.py auto-handles the deterministic subset directly in plain
Python (/list, /open, /pending, /drop, /exit, /journal, /filled, /add) and the judgment-requiring
subset via a scoped claude -p pipeline (/screener, /monitor, /monitorall, /playbook --
portfolio screenshots) -- all commands are automated as of 2026-07-09's full-automation
pass, per the user's explicit authorization for unattended thesis generation.
/monitorall (2026-07-11) is also fired on a timer -- see trigger_auto_monitor.py, invoked
by a Windows Task Scheduler job every 2 hours, gated on real NYSE market hours -- via a
synthetic queued message, same code path as a user typing it manually.

A5 note: this used to append to a JSONL file that check_telegram.py deleted lines from
on READ, not on completion -- a crash between reading and finishing an analysis
permanently lost that request. Now every message is a durable SQLite row that only
reaches a terminal state (sent/failed) once the work is actually done, and a message
stuck mid-processing past a timeout is reclaimed and retried, not lost.

Why this owns getUpdates exclusively: Telegram's getUpdates offset is server-side and
shared across every caller using this bot token -- calling it with offset=N tells
Telegram it can forget every update below N, permanently, regardless of which local
script made the call. Two independent pollers hitting the same token would race and
cause missed messages. So this is the single consumer of the live Telegram queue.
"""

import logging
import logging.handlers
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from telegram_send import get_updates, send_text, escape_html
import env_config
import persistence
import run_files

BOT_DIR = Path(__file__).resolve().parent
OFFSET_FILE = BOT_DIR / ".telegram_offset"
LOG_FILE = BOT_DIR.parent / "ack_listener.log"
HEARTBEAT_FILE = BOT_DIR / ".ack_listener_heartbeat"
PID_FILE = BOT_DIR / ".ack_listener.pid"
POLL_SECONDS = 3

# Rotating, not a plain file (2026-08-09). This log had reached 38 MB -- by far
# the largest thing in the project, and a log nobody will ever open is a log
# that stops being evidence. It has to rotate IN-PROCESS: this listener runs for
# days and holds the handle the whole time, so Windows refuses to let any
# outside sweeper rename the file (run_files.rotate_log tries and returns False,
# on purpose -- a failed rotation is a big log, not a broken bot). Only the
# writer itself can do it, which is what RotatingFileHandler is for.
#
# Size and generation count come from run_files so there is one number, not two
# that drift apart.
_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE, maxBytes=run_files.LOG_MAX_BYTES,
    backupCount=run_files.LOG_KEEP_ROTATIONS, encoding="utf-8",
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger("ack_listener")


def _load_allowed_user_id() -> str:
    value = env_config.get_env_value("TELEGRAM_ALLOWED_USER_ID")
    if not value:
        raise RuntimeError("TELEGRAM_ALLOWED_USER_ID missing from .env")
    return value


ALLOWED_USER_ID = _load_allowed_user_id()


def _is_authorized(update: dict) -> bool:
    """A1: before this check existed, ANY Telegram user who found the bot could have
    their message acked and queued for processing as if they were the owner -- there
    was zero reference to TELEGRAM_ALLOWED_USER_ID anywhere in this file. Both the
    sender's user id AND the chat id must match; an unauthorized update is ignored
    silently (no ack, no queue) so a stranger gets no signal the bot exists at all."""
    msg = update.get("message", {})
    from_id = str(msg.get("from", {}).get("id", ""))
    chat_id = str(msg.get("chat", {}).get("id", ""))
    return from_id == ALLOWED_USER_ID and chat_id == ALLOWED_USER_ID


def _load_offset() -> int | None:
    if OFFSET_FILE.exists():
        text = OFFSET_FILE.read_text(encoding="utf-8").strip()
        return int(text) if text else None
    return None


def _save_offset(update_id: int) -> None:
    OFFSET_FILE.write_text(str(update_id), encoding="utf-8")


HELP_TEXT = """<b>📈 Trading New</b>

<b>מה אפשר לשלוח:</b>

🔍 <b>/screener TICKER</b> (למשל <code>/screener XLV</code>)
   → ניתוח מלא: דוח + תרשים + PDF -- מענה אוטומטי מיידי

📡 <b>/monitor TICKER</b>
   → בדיקת תזה קיימת מול מחיר חי -- מענה אוטומטי מיידי

🔔 <b>/monitorall</b>
   → סורק את כל טיקרי ה-Pending בבת אחת, מסכם רק מה שפעיל -- רץ גם אוטומטית כל 2 שעות בזמן המסחר, וגם ידני על פי דרישה

📊 <b>תמונת תיק</b> (screenshot, בלי טקסט)
   → ניתוח לכל הפוזיציות הפתוחות -- מענה אוטומטי מיידי

📋 <b>/pending</b>
   → כל התזות שממתינות לטריגר, עם דגלים (גיל / שינוי מצב שוק) -- מענה אוטומטי מיידי

🗑️ <b>/drop TICKER reason</b> (למשל <code>/drop XLV לא רלוונטי יותר</code>)
   → מסיר תזה מ-/pending -- מענה אוטומטי מיידי

✅ <b>/filled TICKER price qty starter|full</b> (למשל <code>/filled XLV 45.20 100 starter</code>)
   → רושם כניסה בפועל -- מענה אוטומטי מיידי (חובה לציין starter/full)

➕ <b>/add TICKER price qty</b> (למשל <code>/add XLV 46.10 50</code>)
   → מוסיף כמות לפוזיציה פתוחה קיימת (starter → full) -- מענה אוטומטי מיידי

🧮 <b>/maxadd TICKER</b> (למשל <code>/maxadd XLV</code>)
   → כמה מניות אפשר עוד להוסיף לפוזיציה קיימת בלי לחרוג מתקרת סיכון/חשיפת תיק/סקטור, ובודק אם היעד השמור עדיין תקף -- מענה אוטומטי מיידי

🚪 <b>/exit TICKER price qty</b> (למשל <code>/exit XLV 52.10 100</code>)
   → רושם יציאה בפועל -- מענה אוטומטי מיידי

📓 <b>/journal</b>
   → סיכום כל הטריידים שנסגרו -- מענה אוטומטי מיידי

📃 <b>/list</b>
   → רשימה מהירה: כל הטיקרים ב-Pending + סטטוס, בלי תמונה -- מענה אוטומטי מיידי

📂 <b>/open</b>
   → כל הפוזיציות הפתוחות: כניסה, כמות, סטופ, ימי החזקה -- מענה אוטומטי מיידי

📊 <b>/positions</b>
   → סטטוס קצר (משפט-שניים) לכל פוזיציה פתוחה: איפה עומדים ומה לעשות -- רץ גם אוטומטית פעמיים ביום (אמצע יום + סוף יום), וגם ידני על פי דרישה

💰 <b>/equity 150000</b>
   → מעדכן את שווי החשבון הנוכחי -- משמש לחישוב גודל פוזיציה, חשיפת תיק כוללת ואיזון תיק -- מענה אוטומטי מיידי

🎯 <b>/setrisk 1%</b>
   → קובע כמה אחוז מההון מסתכנים בכל טרייד חדש -- מענה אוטומטי מיידי

💸 <b>/withdraw 17500</b>
   → מסמן משיכה ממתינה שטרם השתקפה בחשבון -- ינוכה משווי החשבון עד שתתבצע בפועל (שלח /withdraw 0 לאיפוס) -- מענה אוטומטי מיידי"""

NOT_YET_WIRED = ("/close", "/reset")


def _instant_reply_for(text: str) -> str | None:
    """Commands that need no fetching/judgment at all -- answered directly, right
    here, never queued. Queueing these would leave the user waiting for a "report"
    that will never arrive, since nothing downstream processes them."""
    stripped = text.strip().lower()
    if not stripped:
        return None
    if stripped in ("/start", "/help"):
        return HELP_TEXT
    if stripped.split()[0] in NOT_YET_WIRED:
        return f"⚠️ <code>{escape_html(text)}</code> עדיין לא מחובר. שלח /help לרשימת הפקודות הזמינות."
    return None


def _ack_text_for(update: dict) -> str:
    msg = update.get("message", {})
    if "photo" in msg:
        return "✅ <b>קיבלתי את התמונה</b> -- מנתח את התיק עכשיו, אשלח בקרוב 📊"
    raw_text = msg.get("text", "")
    stripped = raw_text.strip().lower()
    if stripped == "/pending" or stripped.startswith("/pending "):
        # /pending fetches SPY/QQQ once then scans every pending ticker -- meaningfully
        # slower than a single-ticker command, so it gets its own distinct ack.
        return ("📋 <b>קיבלתי: /pending</b>\n"
                "⏳ בודק את כל התזות הממתינות מול SPY/QQQ -- אשלח את הדוח בעוד רגע.")
    text = escape_html(raw_text)
    return f"✅ קיבלתי: <code>{text}</code>\n⏳ מתחיל לעבוד, אשלח בקרוב..."


def _enqueue(update: dict) -> None:
    msg = update.get("message", {})
    message_type = "photo" if "photo" in msg else "text"
    persistence.enqueue_message(
        update_id=update["update_id"],
        from_id=str(msg.get("from", {}).get("id", "")),
        chat_id=str(msg.get("chat", {}).get("id", "")),
        message_type=message_type,
        message_text=msg.get("text"),
        raw_update=update,
    )


def _trigger_processing() -> None:
    """Event-driven, never a timer: fire-and-forget spawn of process_queue.py right
    after a real message is durably queued. Cheap to call every time -- if another
    instance is already draining the queue, process_queue.py's own lock makes this
    a near-instant no-op, not a second concurrent run."""
    try:
        subprocess.Popen(
            ["python", str(BOT_DIR / "process_queue.py")],
            cwd=str(BOT_DIR.parent), shell=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        logger.exception("failed to spawn process_queue.py -- message stays queued for manual pickup")


def _write_heartbeat() -> None:
    """Written at the TOP of every loop iteration (2026-07-14), before the
    network call that can hang -- ack_listener_watchdog.py checks this file's
    mtime to tell "genuinely idle, no new Telegram messages" (which also
    produces long stretches of log silence, so the log alone isn't a reliable
    signal) apart from "actually stuck inside a hung call." Found real,
    2026-07-13: a getUpdates() call with no client-side timeout hung forever
    after a network blip -- caught and fixed at the source (telegram_send.py
    now passes timeout= on every request), but this heartbeat is the general
    safety net in case anything else ever hangs this loop for a different
    reason in the future."""
    try:
        HEARTBEAT_FILE.write_text(str(int(time.time())))
    except Exception:
        pass  # never let heartbeat bookkeeping itself take the listener down


def run_forever() -> None:
    import os
    PID_FILE.write_text(str(os.getpid()))
    logger.info("ack_listener starting, offset=%s", _load_offset())
    while True:
        _write_heartbeat()
        try:
            last_offset = _load_offset()
            fetch_offset = (last_offset + 1) if last_offset is not None else None
            updates = get_updates(offset=fetch_offset)
            for u in updates:
                if "message" not in u:
                    _save_offset(u["update_id"])
                    continue
                if not _is_authorized(u):
                    msg = u.get("message", {})
                    logger.warning(
                        "REJECTED unauthorized update_id=%s from_id=%s chat_id=%s date=%s "
                        "(no ack, no queue, message content not logged)",
                        u["update_id"], msg.get("from", {}).get("id"),
                        msg.get("chat", {}).get("id"), msg.get("date"),
                    )
                    _save_offset(u["update_id"])
                    continue
                msg = u.get("message", {})
                text = msg.get("text", "") if "photo" not in msg else None
                try:
                    instant = _instant_reply_for(text) if text else None
                    if instant is not None:
                        send_text(instant)
                        logger.info("instant-replied update_id=%s (no queue)", u["update_id"])
                    else:
                        # 2026-07-30 full-system checkup: enqueue BEFORE acking, not
                        # after. Found real: if _enqueue ever raised for a reason
                        # OTHER than the already-handled duplicate update_id (e.g. a
                        # transient SQLite lock), the old ack-then-enqueue order
                        # meant the ack (which had ALREADY sent successfully) got
                        # re-sent again on every retry too, every 3s, forever --
                        # the user's phone would fill with the identical "got it"
                        # message on a loop. persistence.enqueue_message() is
                        # idempotent on duplicate update_id (INSERT OR IGNORE-style,
                        # returns False, never raises), so retrying it here is safe;
                        # only a genuinely failed ack send now gets retried, which
                        # is the correct behavior, not spam of an already-delivered one.
                        _enqueue(u)
                        send_text(_ack_text_for(u))
                        _trigger_processing()
                        logger.info("queued+acked+triggered update_id=%s", u["update_id"])
                except Exception:
                    logger.exception("failed to ack/queue update_id=%s -- will retry next poll", u["update_id"])
                    break  # don't advance past a message we failed to queue/ack
                _save_offset(u["update_id"])
        except Exception:
            logger.exception("poll cycle failed, continuing")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_forever()
