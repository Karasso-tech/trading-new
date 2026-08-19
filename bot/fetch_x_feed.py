"""Category A data-gathering: X/Twitter account feed via Apify (2026-07-22).

Polls a fixed, hand-curated list of X accounts (see X_ACCOUNTS -- vetted by the
user from a video recommendation, Galit Ben Naim deliberately excluded as
unrelated to US-equities swing setups) via the apidojo/tweet-scraper Apify
actor, extracts cashtags from each tweet's text, and stores new tweets
(deduped by tweet id, see persistence.record_x_post) into the x_posts table.

This is a mechanical fetch-and-store step only -- it does not decide
relevance beyond cashtag extraction, and it is never itself a decision input.
See CONSISTENCY_RULES.md's x_posts guardrail: this data may only inform a
disclosed regime-override reason or monitor note, exactly like the existing
"breaking news" allowance -- never bypass a stop/exit rule directly.

Meant to run on a schedule (bot/run_x_feed.bat via Task Scheduler, every
15-30 min), not on demand -- unlike fetch_analysis_data.py/fetch_monitor_data.py
this has no ticker argument, it just polls the fixed account list each time.

Idea-sourcing alert (2026-07-22, explicit user request): after storing, also
Telegrams the user any ticker mentioned that isn't already tracked
(persistence.get_new_candidate_tickers()) so they can decide whether to run a
real /screener on it themselves. This step still makes no trade judgment at
all -- it only ever names a ticker, the same "surface it, never decide"
posture as the rest of this file; CONSISTENCY_RULES.md's x_posts guardrail
applies here too.

Sentiment filter (2026-07-22, explicit user request: "show me only the
positive"): whether a tweet's news reads positive/negative/neutral for the
stock is a genuine judgment call, not a mechanical fact -- so it's classified
via `claude -p`, the SAME AI-judgment path every other Category B decision in
this project uses (see process_queue.py's _run_claude_screener and
requirements.txt's "no direct Anthropic API/SDK calls" note -- this project
deliberately retired its old per-token-metered API bot in favor of `claude -p`
against the user's existing Claude subscription). Only positive-classified
candidates are ever Telegrammed or marked alerted; negative/neutral ones are
left unmarked so they're simply re-considered on a later poll if still within
get_new_candidate_tickers()'s freshness window -- never a hard "never show
this ticker again."

Usage: python bot/fetch_x_feed.py [--max-items N]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import env_config
import persistence
import telegram_send

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ACTOR_ID = "61RPP7dywgiy0JPD0"  # apidojo/tweet-scraper ("Tweet Scraper V2 - X / Twitter Scraper"),
                                  # confirmed live against the user's Apify account 2026-07-22 --
                                  # takes {twitterHandles: [...], maxItems, sort}, returns tweet
                                  # objects with id/fullText/author.userName/createdAt/url.
_RUN_SYNC_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/run-sync-get-dataset-items"

# Hand-curated, reviewable constant (2026-07-22) -- same posture as
# sector_map.py's TICKER_SECTOR_GROUP, not a dynamically editable list.
X_ACCOUNTS = [
    "StockMKTNewz", "LiveSquawk", "wallstengine", "AIStockSavvy", "DeItaone",
    "eWhispers", "charliebilello", "RyanDetrick", "KobeissiLetter", "Bluekurtic",
    "MikeZaccardi", "LizAnnSonders", "KevRGordon", "NickTimiraos", "EricBalchunas",
    "DanielTNiles", "SawyerMerritt", "elonmusk", "jimcramer",
]

# ~3 tweets/account on average across 19 accounts -- tuned for a 15-30 min poll
# cadence (this project's chosen Task Scheduler interval), not a full backfill.
DEFAULT_MAX_ITEMS = 60

CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")
# Twitter's classic createdAt format, confirmed live 2026-07-22, e.g.
# "Wed Jul 22 03:43:01 +0000 2026".
_TWITTER_DATE_FMT = "%a %b %d %H:%M:%S %z %Y"


def _extract_tickers(text: str) -> list[str]:
    return sorted({m.upper() for m in CASHTAG_RE.findall(text or "")})


def _parse_posted_at(created_at: str) -> str:
    """Twitter's fixed-format date string -> this project's ISO-8601 UTC
    convention (matches persistence._now()'s own format, so posted_at and
    fetched_at sort/compare the same way)."""
    return datetime.strptime(created_at, _TWITTER_DATE_FMT).astimezone(timezone.utc).isoformat()


def _fetch_tweets(max_items: int) -> list[dict]:
    token = env_config.get_env_value("APIFY_TOKEN")
    if not token:
        print("APIFY_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    try:
        resp = requests.post(
            _RUN_SYNC_URL,
            params={"token": token},
            json={"twitterHandles": X_ACCOUNTS, "maxItems": max_items, "sort": "Latest"},
            timeout=180,  # cold actor start + scrape time -- this actor runs 30-80 tweets/sec
                          # once warm, but a cold container start can take tens of seconds
        )
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        # Found in the 2026-07-30 full-system checkup: the token is sent as a URL
        # query param, so an uncaught exception's default message (and the full
        # traceback, since run_x_feed.bat redirects stderr to x_feed.log) would
        # write it to disk in plain text. Scrub it before this ever gets printed.
        raise RuntimeError(f"Apify request failed: {str(e).replace(token, '***')}") from None
    return resp.json()


_SNIPPET_MAX_CHARS = 200  # keeps the alert message scannable -- the full tweet is always
                            # one tap away via the included url, this is just enough to
                            # judge at a glance whether it's worth a real /screener run

_SENTIMENT_TIMEOUT_SECONDS = 180  # pure text classification, no tool use -- generous
                                    # headroom over a normal response, nowhere near
                                    # /screener's 1500s (which waits on live data fetches)


def _classify_sentiment(candidates: list[dict]) -> dict[str, str]:
    """Category B judgment call ("is this news positive/negative/neutral for
    the stock"), via claude -p -- see this module's own docstring for why that
    path, not the Anthropic API SDK. No --allowed-tools needed: pure text in,
    text out, no file/data access required.

    Returns {TICKER: "positive"|"negative"|"neutral"}. On any failure (timeout,
    non-zero exit, unparseable output) returns {} -- caller treats a missing
    entry as "not positive," the same "don't guess" posture as everywhere else
    in this project; the candidate simply isn't alerted this cycle and stays
    eligible for re-evaluation on the next one."""
    if not candidates:
        return {}
    lines = [f"- ${c['ticker']} (via @{c['account']}): {c['text']}" for c in candidates]
    prompt = (
        "Classify each ticker's tweet below as positive, negative, or neutral news for "
        "that stock -- whether the news itself reads bullish or bearish, not a trade "
        "recommendation. Base each classification only on the tweet text given, nothing else.\n\n"
        + "\n".join(lines) +
        "\n\nRespond with ONLY a JSON object, no other text before or after, shaped exactly like: "
        '{"classifications": [{"ticker": "NVDA", "sentiment": "positive"}, ...]} '
        "-- exactly one entry per ticker listed above, sentiment one of positive/negative/neutral."
    )
    cmd = ["claude", "-p", "--output-format", "json"]
    try:
        proc = subprocess.Popen(
            cmd, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", shell=True,
            stdin=subprocess.PIPE, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        combined, _ = proc.communicate(input=prompt, timeout=_SENTIMENT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # Real process-tree kill, not subprocess's own timeout-kill -- shell=True spawns
        # cmd.exe as an intermediary and subprocess's kill only reaches that wrapper,
        # leaving the actual claude.exe/node.exe orphaned (same issue process_queue.py's
        # _run_claude_screener documents and works around the identical way).
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        print("sentiment classification timed out -- skipping this cycle", file=sys.stderr)
        return {}
    try:
        data = json.loads(combined)
        result = json.loads(data["result"])
        return {c["ticker"].upper(): c["sentiment"] for c in result["classifications"]}
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        print(f"sentiment classification: failed to parse claude -p output ({e}): {combined[-1000:]}",
              file=sys.stderr)
        return {}


def _format_candidates_message(candidates: list[dict]) -> str:
    lines = ["<b>New potential trade ideas from X feed</b>", ""]
    for c in candidates:
        posted = datetime.fromisoformat(c["posted_at"]).strftime("%Y-%m-%d %H:%M UTC")
        snippet = " ".join(c["text"].split())  # collapse newlines/whitespace for a compact line
        if len(snippet) > _SNIPPET_MAX_CHARS:
            snippet = snippet[:_SNIPPET_MAX_CHARS].rstrip() + "..."
        lines.append(f"<b>${telegram_send.escape_html(c['ticker'])}</b> — via @{telegram_send.escape_html(c['account'])} ({posted})")
        lines.append(telegram_send.escape_html(snippet))
        lines.append(c["url"])
        lines.append("")
    return "\n".join(lines).rstrip()


def _alert_new_candidates() -> int:
    """Sends one Telegram message naming every new candidate ticker whose news
    classifies as positive (persistence.get_new_candidate_tickers() +
    _classify_sentiment()), then marks only those as alerted -- but only if
    Telegram actually confirmed the send (resp.get("ok")); a failed send
    leaves them unmarked so the next scheduled run picks them up again instead
    of silently dropping a candidate the user never actually saw.
    Negative/neutral candidates are deliberately left unmarked too (see this
    module's docstring) -- they're simply not in this message, not rejected
    forever. Returns the number alerted."""
    candidates = persistence.get_new_candidate_tickers(hours=24)
    if not candidates:
        return 0
    sentiments = _classify_sentiment(candidates)
    positive = [c for c in candidates if sentiments.get(c["ticker"].upper()) == "positive"]
    if not positive:
        return 0
    resp = telegram_send.send_text(_format_candidates_message(positive))
    if not resp.get("ok"):
        print(f"candidate alert Telegram send failed: {resp}", file=sys.stderr)
        return 0
    for c in positive:
        persistence.record_candidate_alerted(
            ticker=c["ticker"], account=c["account"], posted_at=c["posted_at"],
            url=c["url"], text=c["text"],
        )
    return len(positive)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    args = parser.parse_args()

    tweets = _fetch_tweets(args.max_items)
    stored = 0
    for t in tweets:
        post_id = t.get("id")
        created_at = t.get("createdAt")
        url = t.get("url") or t.get("twitterUrl")
        author = (t.get("author") or {}).get("userName")
        if not post_id or not created_at or not url or not author:
            continue  # malformed dataset item -- skip, don't guess a value
        text = t.get("fullText") or t.get("text") or ""
        persistence.record_x_post(
            post_id=post_id,
            account=author,
            posted_at=_parse_posted_at(created_at),
            text=text,
            url=url,
            tickers=_extract_tickers(text),
        )
        stored += 1
    print(f"fetched={len(tweets)} stored_or_deduped={stored}")

    alerted = _alert_new_candidates()
    if alerted:
        print(f"alerted_new_candidates={alerted}")


if __name__ == "__main__":
    main()
