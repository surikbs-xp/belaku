---
title: Strategy
---

# Strategy

The entry rules, as implemented in `bot/strategy.py`.

## The rule

A signal fires when **both** conditions agree on a completed candle:

```
buyST   = supertrendBullCount >= 2      # 2 of 3 SuperTrends bullish
buyPSAR = close > psar                  # price above Parabolic SAR

longCondition  = buyST and buyPSAR
shortCondition = sellST and sellPSAR
```

And only on a **transition** — the first closed bar where the condition becomes
true. A run of bullish bars produces exactly one entry.

## Triple SuperTrend

Three SuperTrend instances vote. Each is an ATR period and a multiplier:

| Instance | ATR period | Multiplier |
|---|---|---|
| 1 | 11 | 1 |
| 2 | 12 | 2 |
| 3 | 13 | 3 |

Two of three must agree. Configured by `STRATEGY_SUPERTRENDS` as
`atr_length:multiplier` pairs:

```bash
STRATEGY_SUPERTRENDS=11:1,12:2,13:3
STRATEGY_ST_MIN_AGREE=2
```

The warm-up requirement follows automatically: `longest ATR period + 1` = **14
bars**. Change the periods and the requirement moves with them.

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

:::warning[No stop-loss, no profit target]
Exits are an opposite signal or a manual square-off. There is no risk
denominator, so there is no R-multiple — performance is expressed in rupees
and raw index points.
:::


This is a property of the strategy, not an omission. Size positions accordingly.

## Verifying a verdict

The engine logs one line per completed candle with everything needed to
reproduce it:

```
Indicator values source=NSEIX:GIFT NIFTY interval_seconds=300
candle_at=2026-11-06T23:25:00+05:30 history_bars=1166
ohlc={"close": 24976.0, "high": 24977.5, "low": 24961.5, "open": 24963.0}
indicators={"psar": 24926.5, "st_11_1": 24985.26, "st_12_2": 25014.33,
            "st_13_3": 25043.42, "supertrend_bull": 0, "supertrend_bear": 3}
signal=NEUTRAL
```

Feed that OHLC series into `strategy.evaluate()` and you get the same verdict.

## Next

- [Execution](/execution) — what happens to a signal
- [Testing](/testing) — how the indicators are pinned
