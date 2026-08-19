# Test written down BEFORE it was run — two stops, a normal one and an emergency one

Written 2026-08-19. Owner's idea, in his words: *"maybe we can have regular
stop and another emergency stop.. that avoids the luck"*.

## Where this came from

`PREREGISTRATION_CLOSE_STOP.md` (same day) tested making the stop count only on
a settled daily close. It was **rejected** — the averages looked large but the
four draws disagreed wildly, which is variance, not edge.

The follow-up diagnosis found the specific leak, and it is not vague:

| draw | trades losing worse than 1.2R — live rule | close-only stop |
|---|---|---|
| 42 | 4 | 39 |
| 7 | 9 | 40 |
| 13 | 8 | 53 |
| 99 | 6 | 51 |

Measured as R lost *beyond* the planned 1R, summed over all trades: the live
rule leaks −3.2R / −5.5R / −11.1R / −3.8R per draw. The close-only stop leaks
−25.0R / −35.4R / −45.1R / −27.5R. Roughly **22 to 34 extra bets' worth of
loss per draw**, entirely from positions held past the point the old rule would
have exited.

That is a mechanism, not a mood. The owner's proposal patches exactly it.

## The rule being tested

Two stops on every position instead of one:

* **Normal stop** — the stop level the system already computes today, unchanged
  in where it sits and unchanged in how it trails. Only its *trigger* changes:
  it fires on a settled daily **close** at or below it, and the position sells
  at the **next day's open** (what the live workflow can really achieve — it
  reads settled closes after the bell, the owner acts next morning). An
  intraday spike below it does nothing.
* **Emergency stop** — parked a fixed distance **below** the normal stop, and
  fires the way stops fire today: the instant price touches it, and at the open
  if the bar gaps below it. It rides along underneath the normal stop, so when
  the normal stop trails up, the emergency stop trails up with it.

Distance is measured in the ATR frozen at build time, the same ATR every other
rule in this system uses.

## Why 0.5 ATR is the primary setting, chosen now and not afterwards

From the live trades that started all of this, measured against each name's own
ATR, the intraday spikes that stopped the owner out went this far below the
stop before closing back above it:

* ASTS — about 0.13 ATR below
* BE — about 0.19 ATR below
* SOXX — about 0.47 ATR below

Real spikes lived inside half an ATR. So **0.5 ATR is the primary setting**:
wide enough to let every one of those spikes pass, tight enough that a genuine
collapse still gets caught near the planned loss. 0.25, 0.75 and 1.0 ATR are
run as a robustness check — to show whether the answer survives nearby settings
or only works at one lucky number.

**The primary setting is the answer.** If 0.5 ATR fails, the rule failed. A
different distance that happens to pass is not a rescue — it is picking the
answer after seeing the numbers, and it does not count.

## What counts as the answer, decided now

Scored at the primary setting (0.5 ATR, sell at next open). The rule wins only
if **all five** are true:

1. It makes more money than the live rule on the average of four stock draws
   (seeds 42, 7, 13, 99 — the same four every prior study used).
2. It wins on at least three of those four draws on its own.
3. The gain is bigger than 2% of the account. Smaller than that is noise.
4. It does not make the worst drop in account value bigger.
5. **New, and the point of the whole thing:** the leak actually closes. R lost
   beyond the planned 1R must come back down near the live rule's level on
   every draw. If the emergency stop does not stop the bleeding, then any money
   it makes is the same luck the last test failed on, and it fails here too.

Fail any of the five and the answer is no and nothing in the live system
changes.

## Everything else held still

* Stop levels, buffers, trailing method and timing: unchanged.
* Targets: unchanged, still fill intraday on the daily high.
* Entries, grades, gates, tranche sizes, six slots, 1% of equity: unchanged.
* One change from the live rule per run.
* The engine's default stays the live rule. The new switches are off unless a
  caller sets them.

## What is already known going in

Twelve ways of tightening the stop before target 1 were tested 2026-08-07 and
all lost money. The owner's 1.5-ATR-arm / 1.2-ATR-trail variant failed
2026-08-13. The close-only stop failed 2026-08-19. Every result so far says
this system does not respond well to changing its exits.

The honest reason to run this one anyway: it is the first version that changes
the exit in **two directions at once** — looser against spikes, and a hard
floor that the live rule does not have at all. None of the sixteen prior tests
had a floor.

## Runs

Primary and robustness settings, four draws each, 50 stocks, 5 years,
everything else equal:

Baseline reproduced as a regression check first: with the new switches off,
seed 42 returned the stored $106,570.66 / 24.78% drawdown / 128 trades exactly.

### The primary setting — emergency stop 0.5 ATR below the normal stop

| seed | live rule | rule | diff | live DD | new DD | leak | live leak |
|---|---|---|---|---|---|---|---|
| 42 | 106,571 | 110,522 | +3,951 | 24.78% | 23.74% | −19.9R | −3.2R |
| 7 | 159,182 | 142,980 | **−16,202** | 32.45% | 28.52% | −24.9R | −5.5R |
| 13 | 97,295 | 87,881 | **−9,414** | 32.74% | 28.45% | −33.4R | −11.1R |
| 99 | 160,034 | 164,547 | +4,512 | 19.09% | 20.82% | −26.5R | −3.8R |

Average **−4,288** (−4.29% of account), **2 of 4 draws won**, worst drawdown
**+1.73 pts** deeper.

| test | result |
|---|---|
| 1. more money on average | **FAIL** (−4,288) |
| 2. wins ≥3 of 4 draws | **FAIL** (2/4) |
| 3. gain >2% of account | **FAIL** |
| 4. drawdown not made worse | **FAIL** |
| 5. the leak closes | **FAIL** (−19.9R vs −3.2R) |

**The primary setting fails all five. The rule is rejected. Nothing in the live
system changes.**

### Robustness settings (side checks — these do not decide anything)

| floor | avg diff | draws won | worst DD change | leak, seed 42 | verdict |
|---|---|---|---|---|---|
| 0.25 ATR | +8,159 | 4/4 | +3.21 pts | −14.4R | REJECT (tests 4, 5) |
| **0.50 ATR (primary)** | **−4,288** | **2/4** | **+1.73 pts** | **−19.9R** | **REJECT (all five)** |
| 0.75 ATR | +4,310 | 3/4 | +1.72 pts | −19.9R | REJECT (tests 4, 5) |
| 1.00 ATR | +7,532 | 3/4 | +3.92 pts | −20.6R | REJECT (tests 4, 5) |

Every setting fails. No cherry-pick was available even if one had been allowed.

## Why it failed, and the finding worth keeping

**Test 5 is the one that matters, and it failed everywhere.** The emergency
stop was supposed to plug the leak. Measured as R lost beyond the planned 1R
(seed 42): live rule −3.2R, close-only stop with no floor −25.0R, and with the
floor −14.4R / −19.9R / −19.9R / −20.6R. At its best it recovered under half
the damage; at the primary setting, under a quarter.

The mechanism is now clear and it is not fixable by moving the floor. A floor
parked *below* the normal stop does not cap the loss at 1R — it guarantees
every trade that reaches it loses **1R plus the floor distance**. It trades an
occasional unlimited overshoot for a systematic extra loss on a large number of
ordinary trades. The leak responds to the parameter exactly as that predicts,
smoothly and monotonically: −14.4R → −19.9R → −19.9R → −20.6R as the floor
moves further away. The physics works.

**The money does not follow, and that is the whole result.** Average diff by
floor distance runs +8,159 → −4,288 → +4,310 → +7,532. It zigzags. A real
effect moves smoothly with its own parameter, the way the leak does. Money that
jumps down and back up as the floor slides outward is not responding to the
floor at all — it is noise, and the four draws are simply landing differently
each time.

That is the same verdict as the close-only test, now with a mechanism behind
it: **the money differences in all of these exit studies are variance, not
edge.** The pre-registered primary setting happening to be the *worst* of the
four is a clean demonstration of why the setting has to be named in advance.

## Standing conclusion across all exit studies

Seventeen variations have now been tested on this system: twelve early-trail
rules (2026-08-07), one owner trail variant (2026-08-13), close-only stops in
two fill flavours (2026-08-19), and this two-stop design at four distances
(2026-08-19). **None has beaten leaving the exit alone.** Tighter loses,
looser loses, and a floor under a looser stop loses. The exit is not where this
system's problem is, and further exit variations should not be run without a
new reason that is mechanical rather than another parameter to sweep.
