"""A8: every report must be classified as exactly one of two output classes before
being sent to Telegram -- never sent as an implicit middle ground.

- ANALYSIS_ONLY: sendable even when information is incomplete (unknown sleeve,
  unverified history coverage, etc.) -- but must carry a visible, distinct
  "not an order" banner in both the text message and the widget.
- ACTIONABLE: only when every gate below is true. This mirrors what the protocol
  files already do correctly for a Pending setup (show the full hypothetical plan,
  just don't present a live trigger) -- same split, made an explicit checkpoint
  function instead of a scattered set of inline judgment calls that can slip.

Adaptation note: the original review that specified this (BUGFIX_AND_HARDENING_
INSTRUCTIONS.md, item A8) listed 7 conditions written with the old playbooks.py/
validator.py architecture in mind ("structured output parsed successfully",
"validator passes"). That architecture is retired (see _archive_old_api_bot/) --
there is no separate structured-JSON-parsing step or validate_structured() call in
the current direct-analysis workflow, Claude's own judgment IS the analysis step.
The conditions below keep everything from that list that still maps onto this
architecture (sender auth, data timestamp, sleeve, source provenance) and drop the
two that named retired components. Telegram-delivery-confirmed is deliberately NOT
a pre-send gate condition here -- it can only be known after attempting the send, so
it's a post-send record-keeping step, not something that can block classification
before the send has happened.

Hardening Pass item 5 (2026-07-12): added a fifth condition, earnings_verified.
Rubric criterion 6 (SCREENER_v3.md) and STRATEGY_v3.md's event check both require
a real earnings date. Without an explicit gate, an unattended run either pulls a
date from model memory (stale, unverifiable, dangerous to act on) or silently
skips the check. This condition defaults to False -- an earnings date is verified
ONLY when it came from data actually provided in that run (user-supplied, or a
fetch tool that actually exists), never inferred, never assumed absent. False
forces analysis_only, same as every other condition here.

2026-07-26: the TODO above ("no earnings calendar exists in this system yet") is
closed -- tv_data.py's TVClient.get_earnings_date()/get_economic_calendar() call
tradingview-mcp's calendar_get_earnings/calendar_get_economic tools (TradingView's
own public scanner + economic-calendar endpoints, no chart tab needed), and
fetch_analysis_data.py folds the results into every run's `earnings`/
`economic_calendar_upcoming` output fields. The gate's own default and posture are
UNCHANGED by this -- earnings_verified still must be explicitly set True from that
real field (never assumed), still defaults False. What changed is that a True
value is now actually achievable in the normal case instead of only when a user
happens to paste a date in chat.

Hardening Pass item 6 (2026-07-12): added a sixth condition, data_fresh. A single
data path (TradingView in Chrome -> CDP -> vendored MCP) with no cross-check means one
stale fetch poisons every downstream number. tv_data.py's assert_data_fresh() (real
NYSE-calendar based, not weekday arithmetic) is the source of this boolean -- pass
its result straight through, never re-derive it here. UNLIKE earnings_verified,
this defaults to True: it's a later addition and existing/simpler call sites that
never fetch live bars (a pure DB read, a manual data-timestamp-recorded case
already covered by that condition) must not be silently downgraded just because
they don't pass it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

ANALYSIS_ONLY_BANNER_HE = "⚠️ ניתוח בלבד — לא פקודה מוכנה"
ANALYSIS_ONLY_BANNER_EN = "ANALYSIS ONLY — NOT AN ORDER"


_USER_FACING_HE = {
    "unauthorized": "השולח לא מזוהה",
    "no_timestamp": "לא נרשם תאריך נתונים ברור",
    "sleeve_unknown": "לא סוכם עדיין אם זו החזקה פסיבית או פוזיציה פעילה",
    "no_provenance": "לפחות רמה אחת (סטופ/יעד) לא מצוטטת ממקור אמיתי",
    "earnings_unverified": "תאריך דוחות לא אומת ממקור נתונים אמיתי",
    "data_not_fresh": "הנתונים אינם עדכניים",
}


@dataclass
class GateResult:
    classification: str  # "actionable" | "analysis_only"
    reasons: list[str] = field(default_factory=list)  # internal/debug only -- rule IDs, logs, code review
    user_facing_reasons_he: list[str] = field(default_factory=list)  # what actually goes in a report/widget

    @property
    def is_actionable(self) -> bool:
        return self.classification == "actionable"


def classify_output(
    *,
    sender_authorized: bool,
    data_timestamp_recorded: bool,
    sleeve_known: bool,
    source_provenance_ok: bool,
    earnings_verified: bool = False,
    data_fresh: bool = True,
    stale_bar_date: Optional[str] = None,
) -> GateResult:
    """Call this once per report, right before deciding how to send it. If any
    reason is present, the report must still be sent (per A8 -- the person can learn
    from incomplete analysis) but ONLY as analysis_only, with ANALYSIS_ONLY_BANNER_*
    shown prominently and distinctly from a real order in both the text and the
    widget (widget_template.html's `analysis_only: true` field renders this).

    IMPORTANT: use `.user_facing_reasons_he` in anything the user actually reads --
    `.reasons` is internal (rule IDs like "A7", "B1") for logs/debugging only. An
    earlier version of this put `.reasons` directly into a real report, which is
    exactly the internal-jargon-leaking-into-user-text mistake this project already
    got corrected on once for /help's wording -- don't repeat it here.

    `earnings_verified` (item 5, Hardening Pass): pass True ONLY when the earnings
    date actually came from data provided in this run (user-supplied, or a fetch
    tool that genuinely exists) -- never from Claude's own memory, and never
    inferred from "no news found." Defaults to False, same "never assume the safe
    case" posture as sleeve_known's own default."""
    reasons, user_facing = [], []
    if not sender_authorized:
        reasons.append("sender not authorized (A1)")
        user_facing.append(_USER_FACING_HE["unauthorized"])
    if not data_timestamp_recorded:
        reasons.append("data timestamp/coverage not recorded (B1)")
        user_facing.append(_USER_FACING_HE["no_timestamp"])
    if not sleeve_known:
        reasons.append('sleeve is "unknown" -- STRATEGY_v3.md requires asking, not assuming (A7)')
        user_facing.append(_USER_FACING_HE["sleeve_unknown"])
    if not source_provenance_ok:
        reasons.append("a stop/target/trigger shown lacks a cited real source (rule 1)")
        user_facing.append(_USER_FACING_HE["no_provenance"])
    if not earnings_verified:
        reasons.append("earnings date not verified from a real data source (item 5, Hardening Pass)")
        user_facing.append(_USER_FACING_HE["earnings_unverified"])
    if not data_fresh:
        reasons.append("fetched data is not fresh vs. the last completed NYSE session (item 6, Hardening Pass)")
        suffix = f" — הבר האחרון: {stale_bar_date}" if stale_bar_date else ""
        user_facing.append(_USER_FACING_HE["data_not_fresh"] + suffix)

    if reasons:
        return GateResult("analysis_only", reasons, user_facing)
    return GateResult("actionable", [], [])
