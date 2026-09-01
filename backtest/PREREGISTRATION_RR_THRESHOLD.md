# Test written down BEFORE it was run — the R:R ≥ 2.3 criterion

Written 2026-08-31. This is the first experiment in this folder that was NOT
proposed as an improvement. It was forced by a measurement that came out the
wrong way round.

## Where this came from

Stage 3 re-ran the four portfolio draws on the aligned engine, with trading
costs on, and with the five rubric criteria recorded per trade for the first
time. 608 deduplicated Breakout/Retest trades over five years.

The grade did not rank. That much was expected by then. What was not expected is
that the failure has a direction and a named cause.

**Winners pass the R:R criterion LESS often than losers do:**

| | passes R:R ≥ 2.3 |
|---|---:|
| the 181 winners | 88.4% |
| the 427 losers | 95.6% |
| gap | **−7.2 points, margin ±5.1** |

It is the only one of the five criteria whose winner/loser gap is larger than
its own margin. The other four cannot be told apart from noise.

**Where it acts is visible too — it is a hit-rate trade, and the trade loses:**

| | win rate | average win | average loss | mean R |
|---|---:|---:|---:|---:|
| passed R:R | 28.2% | +3.31R | −1.07R | +0.164 |
| failed R:R | **52.5%** | +1.62R | −1.00R | **+0.377** |

A far target really does pay more when it is reached. It is reached far less
often, and on this sample the exchange is a losing one.

**And the count of criteria passed runs backwards, monotonically:**

| criteria passed | trades | win rate | mean R |
|---:|---:|---:|---:|
| 3 of 5 | 150 | 37.3% | **+0.456** |
| 4 of 5 | 270 | 28.1% | +0.166 |
| 5 of 5 | 188 | 26.1% | **−0.025** |

An idea that satisfies every rule this system has is the worst idea in the book.

**One criterion is inert.** `target_atr` (target ≥ 1.5× ATR14) passed on 608 of
608 trades. It has never rejected anything. The grade is effectively four
criteria wearing the name of five, and that is worth knowing separately from
this test.

## What is being tested

The R:R minimum, and nothing else. Four arms:

| arm | `RR_MIN` |
|---|---|
| **live** | 2.3 — the rule as it stands, the control |
| **A** | 2.0 — matches rule 3's own entry gate, so the rubric stops being stricter than the gate |
| **B** | 1.8 |
| **C** | criterion removed — scored out of four, cutoffs shifted down one |

Everything else is held fixed: same four seeds (7, 13, 42, 99), same 50-ticker
draws, same 5 years, same 6-slot book, costs on, the aligned trailing stop, the
frozen ATR. Only the threshold moves.

**Deliberately NOT tested in the same run:** removing `target_atr`, changing the
grade cutoffs for their own sake, or touching rule 3's entry gate. Two changes at
once measure neither — the lesson `PREREGISTRATION_EARLY_TRAIL.md` had to learn
by voiding its own first run.

## What counts as a pass, decided now

An arm replaces the live threshold only if **all four** hold:

1. **Total R beats the control in at least 3 of the 4 draws.** Not the pooled
   average — four draws agreeing is evidence, one pooled number is an average of
   four things.
2. **The pooled improvement survives dropping each arm's single best trade.** In
   the first pass at this question, one +9.8R trade carried an entire bucket's
   positive total; without it the bucket lost money.
3. **Maximum drawdown does not worsen by more than 3 percentage points** against
   the control, pooled. A lower bar that simply takes more trades and rides more
   heat is not an improvement, it is more leverage wearing a rule change.
4. **The grade ordering improves, or at minimum stops running backwards.** The
   point of this criterion is to make the letter mean something. An arm that
   raises total R while leaving 5-of-5 the worst bucket has not fixed the thing
   that is broken.

If two arms pass, the **more conservative** one wins — the higher threshold, the
smaller change from what the system does today.

If no arm passes, `RR_MIN` stays at 2.3 and this file records that the criterion
is suspect and unimproved. That is a real outcome and it is written here in
advance so it cannot be quietly reinterpreted afterwards.

## What this cannot show, whatever it says

* **One setup type.** Breakout/Retest only. Pullback, Reclaim, Failed Breakdown
  and Gap-and-Hold have never been backtested, and rule 26 already limits its own
  finding the same way.
* **One five-year window**, ending in a period this account's own review found
  difficult. A threshold tuned on it is tuned on it.
* **Four draws of 50 tickers**, not the whole market.
* **The criterion is not the strategy.** Lowering the bar admits trades the
  system currently refuses; whether the owner wants those trades is a separate
  question from whether they backtest better.

## Result

Not yet run. Filled in below, whatever it says.
