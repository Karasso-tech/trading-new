# Test written down BEFORE it was run — does the setup×regime pattern hold on data it was not found in?

Written 2026-08-31, after the signal study and before either split was run.

## The finding this is testing

`signal_study.py` measured every entry point on its own — 503 tickers, five
years, 9,131 finished trades, no slot competition, no cash limit, none of the
live gates applied. Split by setup shape and by the market regime at the moment
the trigger fired:

| regime at fire | Breakout | Reclaim | Failed Breakdown |
|---|---:|---:|---:|
| healthy_uptrend | **−0.128** (589) | +0.039 (506) | **−0.113** (751) |
| risk_on | +0.136 (724) | −0.022 (869) | +0.010 (1018) |
| pullback_in_uptrend | −0.025 (359) | +0.174 (409) | **+0.416** (621) |
| neutral_choppy | +0.157 (741) | **+0.562** (956) | +0.113 (1052) |
| risk_off | +0.724 (91) | −0.251 (44) | +0.128 (63) |

Two things stand out and one of them is already discounted:

* **`healthy_uptrend` is negative for two of the three shapes**, across six
  years (negative in 2021, 2022, 2024, 2026), with no single trade carrying it.
  This is a wider version of what rule 26 already records for Breakout alone.
* **`Reclaim` in `neutral_choppy` is +0.562R over 956 trades** — the strongest
  cell in the table, and the same setup in `risk_on` returns −0.022R.
* **`risk_off` is NOT part of the claim.** It looked best in aggregate and falls
  apart on inspection: 203 trades, the three shapes contradict each other, and
  38% of the group's profit is one trade. It is named here so it cannot later be
  quietly folded in.

## Why this needs a test at all

The pattern was FOUND in this data. Re-testing it on the same data is not a
test, it is an echo — the cells were chosen because they were extreme, and
extremes regress. Anything selected this way will "confirm" on its own sample.

So the rule has to be derived on one part of the data and judged on a part that
had no say in choosing it.

**Honest limitation, stated because it cannot be removed:** the full-sample
table above has already been seen. A genuinely blind test is no longer
available. What follows is weaker than that and stronger than nothing — the
rule is derived MECHANICALLY from the training half, by a procedure fixed
below, rather than by transcribing what the full sample showed. If the
procedure picks different cells than the full sample did, that is itself
informative and is reported.

## The two splits

Both are run. They fail and pass independently.

* **By time** — derive on trades fired 2021-08 through 2024-07, judge on
  2024-08 onward. Answers "does it survive a later market".
* **By ticker** — derive on the tickers at even index in the sorted list, judge
  on the odd ones. Answers "does it survive different companies in the same
  market".

## How the rule is derived, fixed now

On the TRAINING half only:

1. Group finished trades by (setup shape, regime at fire).
2. Keep only cells with **n ≥ 100** in that half. Smaller cells are not scored
   and their trades are always taken — a cell too thin to judge is not evidence
   of anything, and this is what disqualifies `risk_off` automatically.
3. **Block any qualifying cell whose training mean R is below 0.**

No thresholds are tuned, no cells are hand-picked, and the cutoff is zero
because "loses money" needs no calibration.

## What counts as a pass, decided now

On the HELD-OUT half, compare taking every trade against taking every trade
except the blocked cells. The rule passes a split only if **all three** hold:

1. **Mean R improves.**
2. **Total R improves** — a filter that raises the average by refusing almost
   everything has not helped, it has just traded less.
3. **It still refuses at least 10% of the held-out trades.** A rule that blocks
   almost nothing cannot be credited with an improvement.

**The finding is established only if BOTH splits pass.** One of two is a coin.

If only the time split passes, the honest reading is that the pattern is about
this period. If only the ticker split passes, it is about these companies.
Either way, no rule changes.

## What happens on a pass

Nothing automatic. A pass earns the right to propose a rule change and to test
it inside the portfolio backtest, where slots and cash decide whether refusing
a trade actually leaves room for a better one. This study cannot answer that
question and must not be read as if it had.

## What this cannot show

* One five-year window, and the S&P list is today's membership, so companies
  that fell out are missing. That biases the absolute numbers upward and the
  comparisons between cells far less.
* The regime is `regime_formula`'s own classification. If that classifier is
  wrong, every row here inherits it.
* Nothing about position sizing, heat, or whether refusing these trades frees
  capital for better ones.

## Result

Run 2026-08-31. Both splits passed — and the finding was then
**OVERTURNED** on sealed data. See the section below before reading any of this
as support for a rule change.

| split | held-out trades | take everything | apply the rule | refused | verdict |
|---|---:|---:|---:|---:|---|
| by time | 3,261 | +0.019 | +0.096 | 27% at −0.190 | PASS |
| by ticker | 4,562 | +0.136 | +0.207 | 25% at −0.072 | PASS |

The mechanical procedure blocked the cells the full sample had pointed at,
without being told to, on both halves.

### Confirmed a second time, on a different question

The table above scores R, which is the exit engine's output. A separate study
(`research/`, 2026-08-31) asked the exit-independent question instead — from
the entry, did price reach +2R before the stop, with each idea measured against
its own stop distance and no trailing, partials or time exit involved. Same
answer, more sharply:

| setup × regime at fire | reached +2R first | n | first half | second half |
|---|---:|---:|---:|---:|
| Reclaim · neutral_choppy | **47.1%** | 773 | 45.8% | 51.0% |
| Failed Breakdown · neutral_choppy | 36.0% | 929 | 37.1% | 31.1% |
| everything | 34.2% | 9,195 | — | — |
| Breakout · healthy_uptrend | 30.2% | 467 | 27.5% | 32.3% |
| Breakout · pullback_in_uptrend | **24.7%** | 300 | 21.1% | 27.8% |

A flat 2R-target, 1R-stop rule would earn +0.413R in the best cell and −0.260R
in the worst, against +0.025R overall. Reclaim beats Breakout by +4.9 points
pooled (±2.8), and by +5.4 and +4.3 on the two company halves separately.

**The limit, stated plainly:** the gap is not present in every year. It is large
in 2022 (+15.8) and 2025 (+10.4) and absent in 2021, 2023, 2024 and 2026 — and
those are the years with more choppy days, which is the same statement as the
regime column, not a second effect. It is also absent in `risk_on` and reverses
in `risk_off`. So the honest claim is about the CELL, never about the setup
shape on its own.

### OVERTURNED, 2026-08-31, on a year that was sealed before the search began

The claim above is withdrawn. It did not survive.

`research/build_v2.py` sealed everything from 2025-06-09 before any of the new
work started — no threshold, no model and no table touched it. `sealed_test.py`
opened it once, against three conditions written before the run. **All three
failed**, on 1,621 entries the claim had never seen:

| condition | searchable years | sealed year | |
|---|---:|---:|---|
| Reclaim · neutral_choppy beats the base | 48.2% (n742) | **22.5%** (n40) | FAIL |
| Breakout · pullback_in_uptrend trails it | 23.1% (n260) | **36.2%** (n47) | FAIL |
| Reclaim beats Breakout overall | +6.4 points | **−1.3 points** | FAIL |
| base rate | 33.7% | 35.6% | |

Every one reversed sign. The first two cells are thin (±12.9 and ±13.7) and on
their own would only be "not confirmed"; the third is not thin — 428 against
458 entries — and it flipped.

**Why the two earlier splits were not enough, which is the lesson worth keeping.**
Both splits were computed on data whose full table had already been seen. The
by-time split learned on 2021–2024 and judged on 2024–2026, but the cell list
came from a table built on all of it. The by-ticker split shares its calendar
with itself entirely. Neither is a genuinely unseen sample, and the honest
limitation section above said so and was still not weighted heavily enough. A
sealed period set aside BEFORE the search is the only version of this that
works, and it is the standard every future claim in this project has to meet.

### What this still does not license

A pass earns a proposal, not a change. Whether refusing these cells actually
helps depends on whether the refused slot gets filled by something better, and
only the portfolio backtest can answer that. Nothing here has been applied to
the live rules.
