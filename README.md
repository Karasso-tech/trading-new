# Trading New

A swing-trading helper you talk to on Telegram.

You send it a ticker. It reads the charts, checks the trade against a fixed
list of rules, and sends back one clear answer: buy now, wait, watch, or no
trade. It never sends an order to a broker. A person always does that part.

Everything here is written in plain words on purpose. The people who use this
need to trust it fast, in the morning, before the market opens.

---

## What it does

Three jobs, one for each stage of a trade:

| Stage | Question | Command |
|---|---|---|
| Before you buy | "Is this a good trade?" | `/screener AAPL` |
| Waiting to buy | "Did my price get hit yet?" | `/monitor AAPL` |
| After you own it | "What do I do now?" | `/playbook` |

A ticker is only ever in one stage at a time. The system remembers which one,
so tomorrow's answer builds on today's instead of starting over.

## How an answer gets made

1. You send a message to the bot on Telegram.
2. The bot writes the job into a small database and answers "got it".
3. A worker picks the job up, pulls the price bars from TradingView, and reads
   the rule files fresh off the disk.
4. It builds the answer, then checks its own answer against the rules again. If
   the numbers do not match the rules, it says so out loud instead of hiding it.
5. You get back a picture, a short message, and a full report as a PDF.

The rules are the important part, not the code. They live in
`CONSISTENCY_RULES.md` and every one of them was written after a real mistake.
Read that file before changing anything about how a decision is made.

## The commands

**Ideas**
- `/screener TICKER` — full check of a new idea
- `/pending` — the waiting list
- `/drop TICKER` — take an idea off the list

**Watching**
- `/monitor TICKER` — has the trigger price been hit
- `/monitorall` — the same for every waiting idea
- `/filled TICKER` — tell it you bought. When an idea has two setups with
  two different stops, it asks which one filled instead of assuming the first

**Owning**
- `/playbook` — what to do with everything you own
- `/positions` — where each position stands right now
- `/maxadd TICKER` — how many more shares the risk rules allow
- `/exit TICKER` — record a sale

**Account**
- `/equity`, `/setrisk`, `/withdraw` — your account size, your risk per trade,
  and money you plan to take out
- `/override TICKER heat <reason>` — permission to place one order past the
  portfolio heat cap. One ticker, one cap, twelve hours, and your reason is
  written down
- `/pnl` — up or down, with the buy-and-hold money and the trading money kept
  apart instead of blended into one figure
- `/journal`, `/list`, `/open`, `/add` — the record of what happened

## Jobs that run on their own

A few things run on a timer instead of waiting for you to ask. Each one writes
its own log and sends a short Telegram note when it finishes, so a job that
dies quietly cannot go unnoticed.

- a scan at the open, one midday, one after the close
- a nightly refresh of every waiting idea
- a nightly shadow score, to grade ideas nobody took
- a daily database backup
- a watchdog that restarts the Telegram listener if it stops

## Safety rules built in

- **It never places an order.** It writes the order for a human to send.
- **It never invents a number.** Missing data is reported as missing.
- **It never quietly changes a stop.** Status reports are advice only.
- **It grades its own report before sending.** A report whose sizing breaks the
  rules arrives with the warning attached, not cleaned up.
- **One person only.** The bot ignores every Telegram account except the one
  numeric user ID in your `.env`.

## Setting it up

You need Python 3.12 or newer, a Telegram bot of your own, and TradingView
Desktop for the price data.

```bash
pip install -r requirements.txt
playwright install chromium          # for the picture and the PDF
cp .env.example .env                 # then fill in your own values
```

`.env` holds your own Telegram bot token and your own numeric Telegram user ID.
It is never committed. `.env.example` explains each line.

The database makes itself, empty, the first time you run anything.

Two research scripts read filings from the SEC. The SEC asks every caller to
say who they are, so set a contact address before running those:

```bash
export SEC_CONTACT_EMAIL=you@example.com
```

## Running the tests

```bash
cd bot
python -m pytest -q
```

1,400 tests, all passing. Almost every rule in `CONSISTENCY_RULES.md` has a
test named after the mistake that produced it.

## What is in here

```
bot/          the bot, the rules engine, and the tests (42 test files)
backtest/     replays the live decision code over years of real price bars
research/     asks whether anything predicts a good entry. Mostly: no
reports/      where finished reports land
tradingview-mcp/   the connector that reads charts from TradingView Desktop
```

The documents worth reading, in order:

1. `HANDOFF_README.md` — start here with zero context
2. `STARTUP_PROTOCOL.md` — what to do after a reboot or a crash
3. `CLAUDE_CODE_INSTRUCTIONS.md` — how to work in this repo
4. `MASTER_SYSTEM_SPEC.md` — every command, in full
5. `CONSISTENCY_RULES.md` — the trading rules that must never drift
6. `SCREENER_v3.md`, `MONITOR_v2.md`, `STRATEGY_v3.md` — the three protocols

Parts of the trading documents are written in Hebrew, because that is the
language the reports are delivered in.

## About the backtest folder

`backtest/` replays the real decision code over downloaded price history. It is
there to kill ideas, not to sell them. Each experiment has a
`PREREGISTRATION_*.md` file written **before** the run, saying in advance what
would count as a pass and what would count as a fail. Most experiments failed,
and the notes say so.

Run `backtest/fetch_bars.py` first — the price bars are not in the repo,
because they are a large download that anyone can repeat.

## About the research folder

`research/` is one long attempt to answer a single question: of all the entries
the system can name, is there any way to tell in advance which one will work.

The answer is no, and the folder is mostly the record of finding that out. It
measures 9,473 real entries and 121,054 days of the whole market against 87
numbers -- momentum, volatility, volume, overhead supply, base quality, trend
shape, how a stock rides the index -- with walk-forward testing, purged
overlaps and a shuffled control on every run. Nothing predicts direction.
Nothing predicts the odds of reaching a target before a stop. A whole year was
sealed before the search started, opened once at the end, and the last claim
still standing failed on it.

Two measurement traps are documented there because both produced false
positives along the way and both are easy to walk into:

- A twenty-day answer measured on consecutive days shares nineteen of the same
  twenty days, which inflates every t-statistic several fold.
- A permutation test tells you whether luck could have found your rule given
  how much you searched. It is completely blind to a rule whose every trade
  happened inside one market episode. You need both checks; neither one covers
  for the other.

One thing did come out positive: replaying 49 exit rules over the same entries
says the exits are too early and the trailing stop too tight. That has not been
applied to anything. It has earned a pre-registered portfolio experiment, which
is a different and harder test than the one it passed.

`research/FINDINGS.md` is the write-up. Every script is read-only. The tables
they run on are rebuilt from price bars and are not in the repo.

## What is deliberately not here

- `.env` — real tokens. Yours goes in your own copy.
- `trading_new.db` — real positions and real trade history.
- Downloaded price bars and backtest result files — large, and regenerable.
- The owner's own trade record and account figures. Where a rule was written
  because of a real trade, the rule states the lesson in R-multiples or
  percentages, which carry the point without carrying anyone's balance.

## License

No license granted. Public so it can be read and learned from, not so it can be
used to trade someone else's money.

**This is not financial advice.** It is one person's tooling for their own
account. Every number it prints can be wrong. A human decides every trade.
