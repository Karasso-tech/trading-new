"""A7: per-position sleeve classification (core/swing/unknown), gating whether a live
stop-order recommendation is allowed at all for that position.

Before this existed, a real /playbook run correctly flagged SPY/QQQ as "unclear if
Core or Swing," then immediately treated them as Swing anyway and produced live
stop-order logic for both -- directly contradicting STRATEGY_v3.md's own "if unclear,
ask instead of assume" rule. That rule was already written down; it just wasn't
enforced by anything checkable.

Migrated to persistence.py's SQLite-backed thesis table now that A6 has landed, per
A7's own original plan ("ship ahead of A6 with a flat JSON file, migrate into SQLite
once A6 lands, don't block A7 on A6"). This module is now a thin, stable-signature
wrapper so nothing that already imports get_sleeve/set_sleeve from here needs to change.
"""

from __future__ import annotations

from persistence import get_sleeve, set_sleeve  # noqa: F401 -- re-exported, see docstring

VALID_SLEEVES = ("core", "swing", "unknown")
