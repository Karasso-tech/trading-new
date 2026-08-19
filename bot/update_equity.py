"""Mechanical equity update for /playbook automation (added 2026-07-30 full-system
checkup). No judgment here -- reading the broker's total account value off the
screenshot is Category B and stays with whichever Claude session does it; this
script only persists a number it's given, exactly like download_photo.py only
downloads a file it's given.

Found real, 2026-07-30: deliver_playbook_report.py already auto-refreshed
account_equity_usd, but only at the very END of a /playbook run, at delivery
time -- AFTER every per-ticker `fetch_analysis_data.py` call earlier in that
same run had already read cash/heat/allocation math off the OLD, stale stored
equity. The fresh number the model just read off the screenshot never actually
reached that same run's own numbers, only the next run's. Calling this script
immediately after reading the screenshot's total (before the per-ticker loop)
closes that gap -- persistence.set_equity() runs once, at the start, so every
fetch_analysis_data.py call later in the same run already sees the fresh value.
deliver_playbook_report.py's own end-of-run refresh stays in place as a
harmless fallback in case this step is ever skipped.

Usage: python bot/update_equity.py AMOUNT
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import persistence


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: python bot/update_equity.py AMOUNT", file=sys.stderr)
        sys.exit(1)
    try:
        equity_usd = float(sys.argv[1].replace(",", "").replace("$", ""))
    except ValueError:
        print(f"FAILED: {sys.argv[1]!r} is not a valid number", file=sys.stderr)
        sys.exit(1)

    previous = persistence.get_account_settings().get("equity_usd")
    try:
        persistence.set_equity(equity_usd)
    except ValueError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"OK: equity updated {previous!r} -> {equity_usd:,.2f}")


if __name__ == "__main__":
    main()
