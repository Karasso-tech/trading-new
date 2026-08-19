"""Minimal Telegram send helpers for live, direct analysis delivery (no bot process,
no API/CLI model calls -- this only talks to the Telegram Bot API's own HTTP endpoints
to deliver a report that was already produced elsewhere). Reads credentials from
.env directly since this is meant to be run standalone.

Rebuilt 2026-07-09 after the original was deleted along with the rest of the
Telegram delivery layer (see persistence.py's module docstring and
CLAUDE_CODE_INSTRUCTIONS.md's top-of-file note for why). This file's actual send
mechanics were never the source of the reliability problems -- the problem was in
how/when processing got triggered, not in how messages got sent -- so this is a
faithful rebuild, not a redesign.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

import env_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Found in review: a /screener run can cost real money and run up to 25 minutes,
# and used to have exactly one delivery attempt at the very end -- a transient
# network blip (timeout, connection reset, a Telegram 5xx) during that final send
# threw away the whole completed analysis, requiring a full costly re-run just to
# get it delivered. Retries only transient failures; a clean 4xx (bad token,
# bad chat_id) is a real error the caller needs to see immediately, never retried.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2, 5)  # sleep before attempt 2 and attempt 3; none after the last


_TOKEN_URL_RE = re.compile(r"(/bot)[^/]+(/)")


def _scrub_token(exc: Exception) -> None:
    """Mutates exc's own message in place so str(exc) never contains the live
    bot token -- every URL this module builds embeds it as /bot{token}/...
    Found in the 2026-07-30 full-system checkup: an uncaught HTTPError/
    RequestException here would otherwise leak the token in plain text into
    whatever swallows str(e) (e.g. persistence.mark_failed(update_id, str(e)),
    which can end up in process_queue.log). Mutates in place (not a new
    exception) so isinstance checks and exception type stay exactly what
    callers/tests already expect."""
    if exc.args and isinstance(exc.args[0], str):
        exc.args = (_TOKEN_URL_RE.sub(r"\1***\2", exc.args[0]),) + exc.args[1:]


def _request_with_retry(method: Callable[..., "requests.Response"], url: str, **kwargs) -> "requests.Response":
    last_exc: Exception = RuntimeError("unreachable")
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = method(url, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.exceptions.HTTPError as e:
            _scrub_token(e)
            status = e.response.status_code if e.response is not None else None
            # 2026-07-30 full-system checkup: 429 (Telegram's rate-limit response)
            # used to be treated exactly like a real client error (bad token, bad
            # chat_id) and raised immediately, never retried -- but Telegram's own
            # 429 explicitly means "you're sending too fast, try again shortly,"
            # the definition of transient. A burst of sends (a big batch report)
            # could just fail outright instead of backing off. Any OTHER genuine
            # 4xx still skips the retry -- only 429 is treated as transient.
            if status is not None and 400 <= status < 500 and status != 429:
                raise  # real client error -- retrying won't help, surface it now
            last_exc = e
        except requests.exceptions.RequestException as e:
            _scrub_token(e)
            last_exc = e
        if attempt < _MAX_ATTEMPTS - 1:
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
    raise last_exc

_HEBREW_RANGE = range(0x0590, 0x0600)
_RLM = "‏"  # Right-to-Left Mark -- invisible, forces bidi paragraph direction


def _fix_rtl_bidi(text: str) -> str:
    """Telegram's plain-text messages have no CSS to lean on (unlike the widget/PDF,
    which already fix this with unicode-bidi:plaintext) -- a line mixing Hebrew prose
    with English tickers/numbers can visually reorder on-device even though it reads
    correctly when copy-pasted elsewhere. Each newline-separated line is its own bidi
    paragraph, and the Unicode bidi algorithm picks that paragraph's base direction from
    its first *strong* character -- an emoji or a Latin ticker at the start of an
    otherwise-Hebrew line biases the whole line LTR. Prepending U+200F (Right-to-Left
    Mark, invisible) to any line containing Hebrew forces that line's base direction to
    RTL regardless of what its first character is."""
    lines = text.split("\n")
    fixed = [
        (_RLM + line) if any(ord(c) in _HEBREW_RANGE for c in line) else line
        for line in lines
    ]
    return "\n".join(fixed)


def escape_html(text: str) -> str:
    """Escape for Telegram's HTML parse_mode -- only &, <, > are special. Use this on
    any raw/dynamic value (ticker names, user-sent text) interpolated into a message
    that otherwise contains literal <b>/<i>/<code> tags you wrote by hand."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _load_credentials() -> tuple[str, str]:
    token = env_config.get_env_value("TELEGRAM_BOT_TOKEN")
    chat_id = env_config.get_env_value("TELEGRAM_ALLOWED_USER_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_ID missing from .env")
    return token, chat_id


_TELEGRAM_TEXT_LIMIT = 4000  # Telegram's real hard cap is 4096; margin for the RLM
                              # characters _fix_rtl_bidi prepends per Hebrew line


def _split_for_telegram(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Splits on newline boundaries, never mid-line, staying under `limit` per
    chunk. Found real, 2026-07-14: /pending with 12 tickers produced a 5757-char
    message and failed outright with zero visibility ("Bad Request: message is
    too long") -- every send_text caller had this same latent risk, not just
    /pending, so the fix belongs here once, not copy-pasted into every handler
    that might someday build a long message. A single line longer than `limit`
    on its own (pathological, not seen in practice) is hard-sliced as a last
    resort rather than left to fail against the real API."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        line_len = len(line) + 1  # +1 for the newline that joins it back
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_text(text: str, parse_mode: Optional[str] = "HTML") -> dict:
    """Default is Telegram's HTML mode, not legacy Markdown -- Markdown's _italic_
    pairing broke (and infinite-retry-looped) on protocol doc names like SCREENER_v3
    that contain a bare underscore. HTML only needs &/</> escaped (see escape_html())
    and supports <b>/<i>/<code> unambiguously -- write messages with those tags
    directly for bold section headers, no parsing ambiguity possible.

    Transparently splits into multiple messages if `text` exceeds Telegram's
    length cap (see _split_for_telegram) -- every existing caller already treats
    the return value as "the" response (checking .get("ok") and
    .get("result", {}).get("message_id")), so this returns the LAST chunk's
    response to preserve that exact contract with zero caller changes. If any
    chunk fails, stops immediately and returns that failure -- never sends
    remaining chunks after one already failed."""
    token, chat_id = _load_credentials()
    resp_json: dict = {}
    for chunk in _split_for_telegram(text):
        payload = {"chat_id": chat_id, "text": _fix_rtl_bidi(chunk)}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = _request_with_retry(requests.post, f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=15)
        resp_json = resp.json()
        if not resp_json.get("ok"):
            return resp_json
    return resp_json


# Telegram Bot API's real hard limits for a bot-uploaded (not URL-referenced) file.
# 2026-07-30 full-system checkup: neither send_photo nor send_document checked
# this before attempting a send -- a too-large PNG/PDF just failed against the
# real API with no advance warning, and could fail AFTER an earlier attachment
# in the same report had already sent, leaving a confusing partial delivery.
_MAX_PHOTO_BYTES = 10 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


def send_photo(png_bytes: bytes, caption: str = "") -> dict:
    """A Hebrew caption printed to a Windows PowerShell console may look mangled
    (cp1255 console codepage, not UTF-8) -- that's a display artifact only. Confirmed
    live by writing the raw JSON response to a UTF-8 file: the caption Telegram
    actually stores is correct."""
    if len(png_bytes) > _MAX_PHOTO_BYTES:
        return {"ok": False, "description": (
            f"photo too large for Telegram ({len(png_bytes):,} bytes > "
            f"{_MAX_PHOTO_BYTES:,} byte limit) -- not sent"
        )}
    token, chat_id = _load_credentials()
    resp = _request_with_retry(
        requests.post,
        f"https://api.telegram.org/bot{token}/sendPhoto",
        data={"chat_id": chat_id, "caption": _fix_rtl_bidi(caption)[:1024]},
        files={"photo": ("report.png", png_bytes, "image/png")},
        timeout=30,
    )
    return resp.json()


def send_document(file_bytes: bytes, filename: str, caption: str = "", mime_type: str = "application/pdf") -> dict:
    """Sends the full report as a real attachment the user can open from their phone --
    a local file path (e.g. reports/xyz.md) is useless from a phone with no access to
    this machine's filesystem. Defaults to PDF (bot/report_pdf.py renders the .md report
    into a properly RTL-rendered PDF with correct Hebrew encoding) -- pass
    mime_type="text/plain" explicitly if sending a raw .txt fallback instead."""
    if len(file_bytes) > _MAX_DOCUMENT_BYTES:
        return {"ok": False, "description": (
            f"document too large for Telegram ({len(file_bytes):,} bytes > "
            f"{_MAX_DOCUMENT_BYTES:,} byte limit) -- not sent"
        )}
    token, chat_id = _load_credentials()
    resp = _request_with_retry(
        requests.post,
        f"https://api.telegram.org/bot{token}/sendDocument",
        data={"chat_id": chat_id, "caption": _fix_rtl_bidi(caption)[:1024]},
        files={"document": (filename, file_bytes, mime_type)},
        timeout=30,
    )
    return resp.json()


def send_failure_alert(context: str, error: str) -> None:
    """Last-resort 'something broke' ping (2026-07-30 full-system checkup, item 1):
    every deliver_*.py script used to fail its real report send and then just log
    to a file nobody watches -- the user would see nothing at all, indistinguishable
    from the bot being dead. This is called from inside an already-failing except
    block, so it must never itself raise: any problem sending THIS message (bad
    token, network down, whatever) is swallowed and only printed, never re-raised.
    Deliberately plain text, no HTML parse_mode -- a broken tag in a hand-built
    error string must never be the reason even the alert fails to send."""
    try:
        token, chat_id = _load_credentials()
        text = f"⚠️ Report failed: {context}\n{error}"[:_TELEGRAM_TEXT_LIMIT]
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        print(f"send_failure_alert itself failed ({e}) -- report failure for {context!r} went fully silent", file=sys.stderr)


def get_updates(offset: Optional[int] = None, timeout: int = 0) -> list[dict]:
    """Short-poll (timeout=0, immediate return) -- ack_listener.py owns its own
    3-second sleep loop between calls rather than relying on Telegram's long-polling
    wait, so a fresh message is never blocked behind an open long-poll request.

    `timeout=15` on the requests.get() call itself (2026-07-14, found real: a
    ConnectionResetError got caught and logged by ack_listener.py's per-cycle
    try/except as designed, but the VERY NEXT poll then hung forever inside this
    call with no client-side socket timeout set -- requests has no default one --
    silently wedging the always-on listener for 9+ hours with no further log
    output, no crash, nothing for the outer loop to catch. This is a completely
    different thing from the `timeout` PARAMETER above, which is Telegram's own
    server-side long-poll wait sent as a query param, not a client socket
    timeout -- both are needed."""
    token, chat_id = _load_credentials()
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = _request_with_retry(requests.get, f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=15)
    return resp.json().get("result", [])
