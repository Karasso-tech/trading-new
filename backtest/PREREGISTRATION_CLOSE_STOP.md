# Test written down BEFORE it was run — stop only counts on a daily close

Written 2026-08-19. Owner's question, after five stops fired in two days
(2026-08-17 and 2026-08-18) and three of them fired while the trade was ahead.

## What started this

On 2026-08-18 SPY fell 0.68% and QQQ fell 1.69% — an ordinary down day. The
owner's own holdings fell far harder: BE −9.97%, NBIS −7.60%, ASTS −5.72%,
SOXX −4.96%, and NOW −5.08% the day before. Five stops fired.

Two of the five — ASTS and BE — traded below the stop during the day and then
**closed back above it**. Two more, GLD and SOXX, closed below by less than a
quarter of a percent. Only NOW closed clearly below its stop.

That exposed a real mismatch in the live rules, not a feeling:

* **Buying** requires a *settled daily close* above the trigger. A touch during
  the day is not enough.
* **Selling** happens the moment price touches the stop at any second of the
  day. The day is never allowed to settle.

The backtest engine mirrors this exactly (`sim_breakout.py`: entries fire on
`day.close > trigger`, stops fire on `bar["low"] <= stop`), so the question can
be measured on real history instead of argued.

## The question

If a stop only counted when the stock **closed** the day below it, would the
system have made more money over five years?

## What counts as the answer, decided now

The change wins only if **all four** of these are true:

1. It makes more money than the live rule on the average of four stock draws
   (seeds 42, 7, 13, 99 — the same four draws every prior study used).
2. It wins on at least three of those four draws on its own.
3. The gain is bigger than 2% of the account. Smaller than that is noise.
4. It does not make the worst drop in account value bigger.

If it fails any of the four, the answer is no and nothing in the live system
changes. Written before the numbers exist so the answer cannot be picked
afterwards.

## Exactly how the rule works in the test

Two versions are run, because the fill price is a genuine open question and
guessing it after the fact would be picking the answer:

* **Version A — fill at that close.** The day closes at or below the stop; the
  whole remaining position sells at that closing price. This is the single
  cleanest change: only the *trigger* moves from intraday to close, the fill
  still happens the moment the trigger is met.
* **Version B — fill at the next open.** The day closes at or below the stop;
  the position sells at the following day's opening price. This matches how the
  live system actually behaves — it reads settled closes after the bell and the
  owner acts the next morning. It is the more honest of the two and the more
  punishing, because an overnight gap down is eaten in full.

Both versions:

* **Gap-downs are no longer a free exit.** Today the engine sells at the open
  when a bar opens below the stop. Under a close-only stop that protection is
  gone by definition — the position holds all day and can close far lower. This
  is the main cost of the change and is deliberately not softened.
* **Targets are untouched.** They still fill intraday on the daily high,
  exactly as the live rule does. Only the stop's trigger changes.
* **The trailing stop is untouched.** It still moves only after target 1 sells,
  by the same method, on the same timing.
* Stop levels, buffers, entries, grades, gates, tranche sizes, six slots and
  1% of equity per trade are all identical to the live rule. **One change per
  version**, which is the mistake the 2026-08-07 trail study made and had to
  redo.

## What is already known and why this is being run anyway

Twelve ways of moving the stop up before target 1 were tested on
2026-08-07 and every one lost money; tighter was always worse. The owner's own
1.5-ATR-arm / 1.2-ATR-trail variant was tested on 2026-08-13 and also failed,
costing about $55,711 over four draws.

Those all made the stop **tighter**. This test makes it **looser** — the
opposite direction — and none of the twelve covered it. That is why it is worth
the run, and it is also the reason to expect the gap-down cost to be real:
every prior result in this project has pointed at giving trades more room, not
less.

## Runs

Baseline and both versions, four draws each, 50 stocks, 5 years, everything
else equal:

Baseline reproduced first as a regression check: with the new flag off, seed 42
returned the stored $106,570.66 / 24.78% drawdown / 128 trades exactly, so any
difference below comes from the rule and not from the engine edit.

### Version A — sell at that close

| seed | baseline | rule | diff | base DD | new DD |
|---|---|---|---|---|---|
| 42 | 106,571 | 103,521 | −3,049 | 24.78% | 27.70% |
| 7 | 159,182 | 152,779 | −6,403 | 32.45% | 26.74% |
| 13 | 97,295 | 94,048 | −3,248 | 32.74% | 30.69% |
| 99 | 160,034 | 197,855 | **+37,820** | 19.09% | 19.53% |

Average **+6,280** (+6.28% of account), **1 of 4 draws won**, worst drawdown
**+2.92 pts** deeper on seed 42.

### Version B — sell at the next open

| seed | baseline | rule | diff | base DD | new DD |
|---|---|---|---|---|---|
| 42 | 106,571 | 102,240 | −4,330 | 24.78% | 26.49% |
| 7 | 159,182 | 177,696 | +18,514 | 32.45% | 24.27% |
| 13 | 97,295 | 82,856 | **−14,439** | 32.74% | 37.70% |
| 99 | 160,034 | 211,197 | **+51,163** | 19.09% | 23.90% |

Average **+12,727** (+12.73% of account), **2 of 4 draws won**, worst drawdown
**+4.96 pts** deeper on seed 13.

## Verdict — both versions REJECTED

Scored against the four tests fixed before the runs:

| test | A | B |
|---|---|---|
| 1. more money on average | PASS | PASS |
| 2. wins ≥3 of 4 draws | **FAIL** (1/4) | **FAIL** (2/4) |
| 3. gain >2% of account | PASS | PASS |
| 4. drawdown not made worse | **FAIL** | **FAIL** |

Both fail tests 2 and 4. **Nothing in the live system changes.**

The averages are positive and large, and both are the wrong number to read
here. Version B's four draws are −4,330 / +18,514 / −14,439 / +51,163 — the
spread between draws is several times the average itself, and one draw (99)
supplies more than the whole average on its own. That is the signature of
variance, not edge. Checked directly: seed 99's gain is not a single lucky
trade (TRGP +21,598 and KLAC +18,776 both grew), but seed 13 *lost* its largest
baseline winner entirely (UAL +15,122 → gone). Letting trades breathe cuts both
ways, and four draws cannot tell which way it cuts on average.

## What DID reproduce cleanly, on all four draws

The rule does mechanically what it was designed to do:

| seed | trades base → A → B | win rate base → A → B |
|---|---|---|
| 42 | 128 → 102 → 106 | 28.9% → 36.3% → 35.8% |
| 7 | 175 → 138 → 135 | 31.4% → 34.8% → 37.8% |
| 13 | 162 → 142 → 161 | 25.3% → 31.7% → 30.4% |
| 99 | 167 → 140 → 140 | 34.1% → 40.7% → 37.9% |

Fewer trades and a higher win rate on **every** draw, both versions — roughly
20% of stop-outs were intraday spikes on bars that closed back above the stop,
and removing them is real, not noise. The money simply did not follow, because
the trades that survive a spike include the ones that go on to break down
properly, and the gap-down protection lost along the way is paid in full.

## If this is revisited

Do not re-run with more seeds and read the same numbers again — four draws that
disagree, re-drawn until they agree, is picking the answer after the fact. A
follow-up needs its own pre-registration, its draw count fixed in advance, and
the same four tests decided before any of it runs.
