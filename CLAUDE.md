# Trading New — house rules for Claude

## Rule 1 — must-have, never optional: first-grader words

**Every reply must be written in simple words a first grader would understand.**
This is not a mode that gets turned on for big explanations. It applies to
every single answer, always:

- chat replies
- plan files
- markdown docs and guides
- Telegram reports
- review notes and audit findings
- commit-message *summaries* spoken to the owner (the commit text itself stays normal)

What that means in practice:

- Short sentences. One idea per sentence.
- Everyday words. "Grade" not "rubric evaluation". "Check again" not
  "re-validate". "Broken" not "non-deterministic failure mode".
- No rule numbers, no `file.py:120` line pointers, no internal code names in
  anything the owner reads. Those belong in code and commits, not in the
  explanation.
- If a special word is truly needed, explain it in the same sentence in
  everyday words. If it can't be explained that way, don't use it.
- When something is wrong, say it in this shape: (1) what's wrong, (2) why it
  matters to a trader, (3) what gets fixed, (4) what it looks like after.
- One problem at a time. No big dumps of findings.

The only places normal technical language is allowed: source code, commit
messages, pull-request bodies, and safety warnings before something that
can't be undone.

The owner has asked for this more than once, most recently on 2026-08-04.
Treat it as a hard requirement, the same as any other rule in this project.

## Where the real rules live

- `CLAUDE_CODE_INSTRUCTIONS.md` — how to build and run things here
- `CONSISTENCY_RULES.md` — the trading rules that must never drift
- `SCREENER_v3.md`, `MONITOR_v2.md`, `STRATEGY_v3.md` — the three protocols
- `HANDOFF_README.md` — start here if you have zero context
- `STARTUP_PROTOCOL.md` — what to do after a restart or crash

Read those fresh from disk. Never work from a summary of them.
