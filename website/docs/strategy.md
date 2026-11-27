---
title: Strategy
---

# Strategy

The entry rules, as implemented in `bot/strategy.py`.

## The rule

A signal fires when **both** conditions agree on a completed candle:

```
buyST   = supertrend is bullish        # 14-period ATR at 3x
buyPSAR = close > psar                 # price above Parabolic SAR

longCondition  = buyST and buyPSAR
shortCondition = sellST and sellPSAR
```

**Both must agree: a 2-of-2 confirmation.** Agreement of either one alone is not
a signal. The two checks are independent rules — the SuperTrend direction and a
price-position check — so requiring both is what confirms the trade, and the
engine reports the count (`confirm BUY 2/2`) rather than only the boolean
outcome.

These are the **only two indicators**. There is no EMA, RSI, TSV, divergence
calculation, or diagnostic marker, and no vote across several SuperTrend
instances: the SuperTrend is fixed at 14-period ATR × 3 and is not configurable,
because a second instance would reintroduce exactly the vote this strategy
dropped. The *2* in "2 of 2" counts the rules, not SuperTrend instances.

:::note[The PSAR vote cannot abstain]
The sell side is `close <= psar`, the exact complement of `close > psar`, so one
of the two always holds. Writing them as a pair of strict comparisons
(`> ` and `<`) leaves a **tie voting neither**, and PSAR's two-bar clamp can pin
the SAR exactly onto the close.

When that happens the PSAR rule abstains while SuperTrend confirms, giving 1 of 2
and no signal on a bar that was really a short. That is not hypothetical: a GIFT
bar closed at 25279.00 with the SAR at 25279.00, and the bot stood aside.
:::

And it is **driven by a transition**, not by the state. A bar that merely still
agrees with the previous one is not a new signal, so it cannot open a trade:

| Position | Verdict | What happens |
|---|---|---|
| Flat | BULL / BEAR **becomes** true | **Enter** that side |
| Flat | BULL / BEAR already true | **Nothing** — the state is not a new signal |
| Positioned | agrees with the position | **Hold** |
| Positioned | opposes the position, on a fresh signal | **Flip** — close the leg, open the other side |
| Positioned | opposes the position, on an existing state | **Close only** — go flat and wait |
| Positioned | NO_SIGNAL **becomes** true | **Hold** — the pair disagreeing is not an exit |
| Positioned | NO_SIGNAL already true | **Hold** |

**The pair flipping against the position is the primary exit.** A position is
held until SuperTrend and PSAR turn the *other* way, and then closed in full. A
`NO_SIGNAL` bar says nothing about a position already on, so it is carried rather
than cut. There is no profit target.

Neither book carries a **stop-loss or a trailing stop**. The signal is the only
exit, so a single loss is not capped by a level and open profit can be given
back. See [why there is no trail](./multi-strategy.md#why-there-is-no-trail).

The exit is a **transition**, exactly like the entry. A run of opposite bars is
one exit, not one per bar — otherwise every bar of the new trend would be a no-op
square-off and the signal trail would bury the real decision.

Exiting on the flip rather than on `NO_SIGNAL` lowers the win rate and raises the
payoff ratio: the average win grows faster than the win rate falls. It is the
default because of that, not because it makes the strategy profitable — over
three years of real bars it does not. See [Backtesting](./backtesting.md) for the
numbers, including why the apparent edge does not survive out-of-sample.

### Exit rules

The rule is selectable with `STRATEGY_EXIT`:

| Value | Exit | Then |
|---|---|---|
| `flip` *(default)* | The pair turns the other way. A `NO_SIGNAL` bar is held. | **Reverses into the new direction on the same bar.** |
| `flat` | The pair first disagrees. This is the rule `flip` replaced. | Goes flat and waits for a transition. |

**The flip reverses on the same bar, and must.** The bar that flips the pair is
the only fresh transition into the new direction — the bar after it is that same
state persisting, so it is not a new signal. Deferring the reversal to "the next
fresh signal" would close the position and then sit flat for the whole of the new
trend, taking one trade out of every two swings. Measured over three years,
deferring it took 480 of 959 flips.

Close-then-open is the order the broker sees. Opening the new side first would
briefly carry both, which the order manager refuses everywhere else.

A run of bullish bars therefore produces exactly one entry, because only the bar
where the pair *starts* agreeing is a signal. That is what makes an exit final:
the signal in force when the position closed is never re-used, so the next trade
needs the conditions to form again from scratch.

A transition alone would lose a signal that closes while the NSE is shut, because
the executor drops the order and the bar still advances the previous verdict. So a
fresh direction is **latched** as an owed entry and honoured on the first bar that
can fill it. The latch is persisted, so a deferred entry survives a restart
without anyone intervening; see
[Execution](/execution#why-live-defers-instead-of-queueing).

On a **restart** nothing is opened on a state the process merely inherited: the
bar it boots into has `previous_signal == signal`, so it is not a transition. An
entry owed from *before* the restart is the exception — it was latched by a bar
this bot did observe, so it is a real signal rather than a boot-time guess.

The rule is applied to **both books and both modes**, and both run it inside the
NSE session only. GIFT NIFTY quotes outside it, but the GIFT 15m strategy must
not generate or execute a trade outside the permitted session, and neither may
the NIFTY 60m strategy. See
[Execution](/execution#two-strategies-one-instrument) for how the two books
differ.

## SuperTrend

One instance, fixed in code: a **14-period ATR with a 3× multiplier**
(`strategy.SUPERTREND_ATR_LENGTH` / `SUPERTREND_MULTIPLIER`). It is deliberately
not configurable — a second instance would reintroduce the vote this strategy
dropped, and a threshold over a vote count is exactly the failure mode that used
to disarm the bot silently.

The warm-up requirement follows from it: `ATR period + 1` = **15 closed bars**.

`STRATEGY_SUPERTRENDS` and `STRATEGY_ST_MIN_AGREE` are no longer read. Leaving
them in an old `.env` is harmless: they cannot add an instance or suppress a
signal.

## Parabolic SAR

Standard Wilder PSAR with `start=0.02`, `increment=0.02`, `maximum=0.2`.

Price above SAR is bullish; below is bearish. It is a single binary check, not a
vote.

## Why the indicators are computed locally

Both are pinned by tests against independently transcribed Pine references, and
both match **exactly on 1119 real GIFT Nifty bars**.

### SuperTrend

Comes from `pandas-ta-classic`, the maintained fork of `pandas-ta`. The original
package's last release needs numba, which caps it at Python 3.12 and cannot be
installed on this project's interpreter at all. Both forks produce identical
SuperTrend output — verified on a 300-bar series with zero direction differences.

### Parabolic SAR

Computed in `bot/strategy.py`, **not** taken from the library. `pandas-ta`'s
`psar` was measured against the canonical definition and diverged on roughly
**30% of bars** (and its `tv` flag has no effect). The local implementation
mirrors the published Pine reference exactly, including its initialization: bar
1 only seeds the trend and is never a reversal bar, and the loop starts at bar 2.

## Candle data quality

Indicators are recursive, so a hole or a truncated bar skews every later value
until it washes out. Measured: PSAR off by tens of points and SuperTrend by up
to ~14 points right after a gap, converging after roughly 50 bars.

The feed therefore never hands a bar to the strategy unless it observed it from
the open:

- A bar whose first tick arrived more than 10 s late (feed joined mid-bar) is
  marked `partial = 1` and excluded.
- A bar cut off by a shutdown is marked `partial = 2` and excluded.
- Kite history is authoritative and replaces tick-built bars when available.

The `partial` column on `gift_candles` records this, so a suspect bar is
identifiable after the fact rather than silently trusted.

## What the strategy does not have

:::note[The signal is the only exit]
The exit is the signal flipping, or a manual square-off. There is **no stop-loss
and no trailing stop**, so a single loss is not capped by a level and open profit
can be given back. See
[why there is no trail](./multi-strategy.md#why-there-is-no-trail). Performance is
expressed in rupees and raw index points, not in R-multiples.
:::

:::note[What this costs]
Measured on 853 real 15-minute GIFT Nifty bars, the signal exit produced 42
trades at a 38% win rate: average win 106 points, average loss 61 points. Gross
that is **+₹6,792**; after ₹40,539 of round-trip charges it is **−₹33,747**.

The old 100/30/5 trailing rule on the same bars produced 43 trades and
**−₹41,829** net. The signal exit is the better of the two, but neither is
profitable at one lot — the charge stack is the binding constraint, not the exit
rule. See [Performance](/performance) for the cost breakdown.
:::

## Contract rollover

The strategy trades the **front-month** Nifty future, and NSE monthly Nifty F&O
expires on the **last Tuesday** of the month, stopping at the 15:30 IST close.

The engine does not wait for that close. It switches to the next month
**`ROLLOVER_DAYS_BEFORE_EXPIRY` days early** (5 by default), so the last five
days of a contract's life already resolve to the next one:

```
Nov 18  →  NIFTY26NOVFUT     (6 days to expiry)
Nov 19  →  NIFTY26DECFUT     (5 days to expiry — the window opens)
Nov 24  →  NIFTY26DECFUT     (expiry day)
```

Rolling early is a deliberate trade-off. The front month is usually the more
liquid contract, so leaving it early costs some spread — but a position opened in
the last few days of a contract has to be closed or carried through an expiry,
and an expiry is a settlement event rather than a trade. Five days leaves room
for a signal to arrive and the position to be established in the contract that
will still exist next week.

The window only decides which contract the engine **names**. It does not move an
existing position on its own — that takes a signal, as below.

:::note[Both modes roll together]
`PAPER` derives the window from the expiry calendar (`future_symbol`). `LIVE`
resolves it against the **broker's own instrument dump** (`front_month_symbol`),
so it can only ever name a contract Kite actually lists — but it applies the same
`ROLLOVER_DAYS_BEFORE_EXPIRY` window, so the same signal opens the same contract
in either mode.

This was not always true. LIVE used to take the dump's first entry in expiry
order, which never rolls early: it rode the front month to the 15:30 close while
PAPER had already moved to next month. Two consequences followed, both now fixed —
the entry blocks below were unreachable in LIVE (by the time a contract stopped
trading the LIVE symbol had already followed the calendar past it), and a live
position could be carried into an expiry that PAPER would have rolled out of.

If the dump has not been refreshed since the rollover, `front_month_symbol`
returns empty and the entry is refused rather than sent for a contract the broker
does not know.
:::

### What happens to a position in the old contract

Inside the window the engine names next month's contract while a position may
still be held in the front month — which is still *tradeable*, just no longer the
one being traded. The next signal decides:

| Signal | What happens |
|---|---|
| **Flip** (opposite the position) | The old leg is closed and the new contract opened in one action, so the position follows the signal. The close is booked as a `ROLLOVER` trade. |
| **Agrees** with the position | Nothing. Rolling would pay two spreads to end up exactly where the position already is, so it is left in the contract it is in and runs to expiry. |
| **No signal** | Nothing. The position runs to expiry. |

A position in a **later** month than the one being traded is not a rollover at
all — that is double exposure, and it is refused in both modes so PAPER cannot
book a state LIVE would reject.

:::caution[The roll waits for a signal, so a quiet market can reach expiry]
Because only a *flip* moves the position, a run of agreeing signals — or no
signals at all — leaves the old leg in place until its contract stops trading.
That is the case the entry block below exists for.
:::

### What the engine does about it

- **Warns** once an hour inside the final session, in the log.
- **Flags** a position whose contract has expired, via
  `rollover_status(open_symbol=…)`. The check is driven by the contract actually
  held, not by the calendar — the calendar has already rolled, so a
  calendar-only test can never report an expiry.
- **Blocks new entries** while a leg is in a contract that has stopped trading
  (`entries_blocked`). Opening the next month beside it would be two contracts at
  once, which is the double exposure the rollover branch refuses anyway. The
  block clears when the leg is squared off.
- **Refuses** an entry whose symbol has expired, as a backstop. In normal
  operation this never fires, because the symbol and the calendar roll together —
  it exists for the case where they disagree (a stale contract from Kite's
  instrument dump, or a clock past the close without the symbol following).
- **Decides the overnight position on the morning's first in-session bar**: the
  09:00 GIFT candle with 15-minute bars, which closes at 09:15. The position is
  driven to the verdict: a flat book enters BULL/BEAR, an agreeing leg is held, an
  opposing leg is flipped, and a bar with no signal leaves the leg alone. A signal
  refused for arriving outside hours is not queued, so without this the decision
  would be made on the overnight state. See
  [Execution](/execution#why-live-defers-instead-of-queueing) for the detail.
- **Reaches the old leg** when squaring off. A `FLAT` carries the contract the
  engine trades, which inside the window is next month's, while the position may
  still be in the front month. A strict symbol filter would report "No open
  positions" and leave the exposure open, so the filter also matches a leg the
  engine has rolled away from.

It deliberately does **not** touch an expired position on its own — an expiry is
a settlement event, so it is a decision for a human. Blocking an exit would be far
worse than blocking an entry, which is why the entry block never applies to an
exit. The open reconcile is the one case where the bot closes a position by
itself, and that is a *directional* decision rather than an expiry: the
indicators have turned against the position, so it flips.

:::note[Two deadlines, and they are different things]
`expiry` is the contract the engine is *trading*. `calendar_expiry_date` is the
contract that is *literally still trading*, which is what "expiring today" and
the rollover window are measured against. Inside the window the two differ by a
month, and conflating them is what made an earlier version report a healthy
"48 days to expiry" for a contract that had already stopped trading.
:::

:::note[Why not `nearest_monthly_expiry` alone]
`nearest_monthly_expiry()` rolls forward at the close, so the contract it returns
is always still tradeable. A "has it expired" test against it is never true. The
deadline is the expiry of the contract that is *currently* front-month, which is
the previous month's once the close has passed.
:::

## Verifying a verdict

The engine logs one line per completed candle with everything needed to
reproduce it:

```
Indicator values source=NSEIX:GIFT NIFTY interval_seconds=900
candle_at=2026-11-06T23:15:00+05:30 history_bars=1166
ohlc={"close": 24976.0, "high": 24977.5, "low": 24961.5, "open": 24963.0}
indicators={"psar": 24926.5, "st_14_3": 24985.26,
            "supertrend_bullish": false, "supertrend_bearish": true}
signal=NO_SIGNAL
```

Feed that OHLC series into `strategy.evaluate()` and you get the same verdict.

## Next

- [Execution](/execution) — what happens to a signal
- [Testing](/testing) — how the indicators are pinned
