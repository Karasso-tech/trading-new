# Test written down BEFORE it was run — early trail at 1.5 ATR

Written 2026-08-13. Owner's idea, in his words: *"what happen if the stock
reach profit of 1.5 atr and above and then you trigger trail 1.2 atr"*.

## The question

Today the stop does not move at all until the first target sells. The owner
asks: once a trade is up 1.5 ATR from the entry price, start moving the stop up
behind the price, keeping it 1.2 ATR under the best price so far.

Does that make more money than leaving the stop alone?

## What counts as the answer, decided now

The rule wins only if **all four** of these are true:

1. It makes more money than doing nothing on the average of four stock draws
   (seeds 42, 7, 13, 99 — the same four the last study used).
2. It wins on at least three of those four draws on its own.
3. The gain is bigger than 2% of the account. Smaller than that is noise.
4. It does not make the worst drop in account value bigger.

If it fails any of the four, the answer is no and nothing in the live system
changes. This is written before the numbers exist so the answer cannot be
picked afterwards.

## Exactly how the rule works in the test

- ATR is the one frozen at entry, never a fresh one. Same as the live rule.
- Best price so far is measured on **daily closes** (a second version measures
  daily highs, run separately, so the two are never mixed).
- The trail turns on the first day the best price is 1.5 ATR or more above
  entry, and stays on.
- Once on, the stop goes to best-price minus 1.2 ATR. It only ever moves up.
- The stop is never placed at or above the market: it is capped at today's
  close minus 0.15 ATR. That is the same small buffer the live stop rule uses.
- Decided on today's close, in force from tomorrow. Same timing as live.
- After the first target sells, the normal live trail takes over. Because a
  stop can never move down, whatever the early trail already locked in stays.
  This keeps the test to **one** change, which is the mistake the 2026-08-07
  trail study made and had to redo.
- Everything else is untouched: same entries, same grades, same gates, same
  targets, same tranche sizes, six slots, 1% of equity per trade.

## What is already known and why this is being run anyway

On 2026-08-07 twelve ways of moving the stop up before the first target were
tested on 5,382 breakouts over 14 years. Every one lost money, and tighter was
always worse. Break-even at 2R lost about $191,000 on a $100,000 account with
1% risk. A 1.2 ATR trail is tighter than that.

Two honest reasons to run it anyway: the newer engine replays the live code on
real bars instead of a separate script, and the owner's exact trigger — wait for
1.5 ATR first, then trail — was not one of the twelve.

## Runs

Baseline and rule, four draws each, 50 stocks, 5 years, everything else equal:

    python backtest/sim_portfolio.py --seed S --n 50 --years 5
    python backtest/sim_portfolio.py --seed S --n 50 --years 5 \
        --early-arm-atr 1.5 --early-trail-atr 1.2 --tag et15

## Result — ran 2026-08-13. The rule failed. Nothing changes.

Money on a $100,000 account, five years, 50 stocks per draw:

| draw | leave stop alone | early trail | worst drop, alone | worst drop, trail |
|------|-----------------|-------------|-------------------|-------------------|
| 42   | +6.57%          | **+14.20%** | 24.8%             | 19.9%             |
| 7    | **+59.18%**     | +19.60%     | 32.5%             | 29.1%             |
| 13   | −2.70%          | **−0.71%**  | 32.7%             | 28.2%             |
| 99   | **+60.03%**     | +34.28%     | 19.1%             | 26.9%             |

Average: leave alone **+30.77%**, early trail **+16.84%**. Across the four
draws together that is **$55,711 less money**. Won 2 of the 4 draws, so it
fails test 1 and test 2 of the four written above. Answer is no.

The draw it won biggest is seed 42 — the same draw every earlier idea also
looked good on, and the same draw those ideas then failed on fresh stocks.

**Where the money goes.** Big winners, meaning trades that made 3 times the
risk or more:

| draw | big winners, alone | big winners, trail | best trade, alone | best trade, trail |
|------|--------------------|--------------------|-------------------|-------------------|
| 42   | 12                 | 8                  | 12.2R             | 4.5R              |
| 7    | 27                 | 12                 | 17.4R             | 9.7R              |
| 13   | 15                 | 8                  | 15.7R             | 9.6R              |
| 99   | 19                 | 11                 | 9.8R              | 7.1R              |

Small wins go the other way — from 3 to 7 of them per draw up to 118 to 143.
The rule turns a few huge winners into a pile of tiny ones. That is the same
finding the 2026-08-07 study made with twelve other trails, reached here by a
different route.

**The one thing it did do well:** the worst drop in account value got smaller
in three of the four draws. It is a calmer ride that ends with less money.

**Honest caveat.** Trades end sooner, so slots free up and the book takes about
2.3 times as many trades (roughly 165 becomes 380). Some of the loss is that
crowding, not the stop itself. That does not change the answer — the crowding
is real money in a real six-slot account — but it means the stop's own damage
is smaller than the headline gap.

Live trading code was not touched. The switch lives only in the backtest and is
off by default.
