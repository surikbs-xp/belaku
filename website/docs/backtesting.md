---
sidebar_position: 4
---

# Backtesting

The engine's own ledger is a short record of what the strategy did. A few dozen
trades in one market regime is not evidence, and a configuration chosen because
it looked good over that window is a guess with a chart attached. This page
records what a multi-year replay of the *live* rule actually found, and how to
reproduce it.

## Running it

`scripts/backtest.py` pulls closed bars from Kite, replays the strategy, and
charges every round trip the real cost. It uses `strategy.evaluate()` rather than
a reimplementation, so it cannot drift from the bot.

```bash
cd bot
../.venv/bin/python ../scripts/backtest.py --years 3
../.venv/bin/python ../scripts/backtest.py --years 3 --walk-forward
../.venv/bin/python ../scripts/backtest.py --years 3 --exit flat --session nse
../.venv/bin/python ../scripts/backtest.py --years 3 --cache /tmp/gift.json
```

Signals are computed with `strategy.evaluate`, which is `O(n²)` over the series —
a 3-year 60-minute run takes a couple of minutes. `--cache` saves the fetch, not
the signal computation.

| Flag | Effect |
|---|---|
| `--years N` | How much history to pull (Kite serves ~3 years at 60 minutes) |
| `--interval` | `60minute` (default), `30minute`, `15minute`, `day` |
| `--exit` | `flip` (shipped default) or `flat` |
| `--trend-ma N` | Only enter when price is on the trend's side of this SMA |
| `--filter-adx N` | Only enter when ADX(14) exceeds N — the live `FILTER_ADX` gate |
| `--filter-ma N` | Only enter when close is above this SMA — the live `FILTER_MA` gate |
| `--session nse` | Restrict entries to 09:15–15:30 IST |
| `--eod` | Flatten before the daily close |
| `--max-hold N` | Time exit after N bars |
| `--walk-forward` | Fit a grid on each year, trade the next |

## What it fills on

Bars come from one instrument and so do the fills. The default is the GIFT Nifty
index, which is what the code's GIFT index book trades, and it is the only
continuous series spanning years — NSE futures expire monthly and Kite serves no
history for the expired ones.

Trading the future instead adds the spot/future basis, whose **change** is a
~40 pt/bar random walk at 60 minutes. That is large enough to swamp a 60-minute
edge. It does not bias the result in either direction, but it does mean a
futures-filled backtest needs a futures series that does not exist yet.

## Costs are the constraint

`estimate_charges` at 25,000 gives ₹965.22 per round trip on one lot of 65 —
**14.85 points**. STT is 84% of it and scales with turnover, so a larger lot does
not amortise it. This single number decides whether the strategy is viable, and
it is larger than the average *win* of most configurations tested.

## The result, over three years

GIFT Nifty, 60-minute, 2023-11-20 → 2026-11-18, 16,158 bars, 463 trades:

| Configuration | n | win | gross/trade | PF | net |
|---|---|---|---|---|---|
| **flip, all hours** (shipped default) | 463 | 35.4% | −2.21 pt | 0.98 | −₹513,294 |
| flip, NSE session | 328 | 35.1% | +1.08 pt | 1.01 | −₹293,485 |
| flat, all hours | 831 | 43.2% | −1.10 pt | 0.99 | −₹828,293 |
| flat, NSE session | 446 | 44.6% | +0.14 pt | 1.00 | −₹426,426 |
| flip + 200MA, NSE | 196 | 38.8% | +17.49 pt | 1.23 | **+₹33,702** |
| daily bars, flip | 30 | 26.7% | — | 0.20 | −₹951,989 |

Gross profit factor is **0.98–1.00** for the shipped rule. The strategy takes
money in the market and hands essentially all of it back to the broker: gross
+₹222,885 against ₹189,183 of charges on the 200MA variant, and a net loss on
every unfiltered one.

### The one positive row does not survive scrutiny

`flip + 200MA + NSE` is the only configuration that clears costs, and it fails
every robustness test:

- **Bootstrap** 95% CI on net/trade: **[−₹2,087, +₹2,546]**, P(profitable) 54.4%.
- **Per year**: 2024 −₹13,853, 2025 −₹128,090, 2026 +₹160,669. One year pays for
  the others; the trend is not monotonic.
- **Walk-forward**: fitting the same grid on each year and trading the next gives
  **−₹52,764 / −₹148,530 / +₹9,807** — two of three years lose.
- **Random entries** beat it in 22.7% of 300 runs (p≈0.23). Indistinguishable
  from luck.
- **Buy and hold** the same span made **+5,417 points = ₹352,105** with no
  charges, so the strategy underperforms doing nothing.
- Long side +₹137,962, short side −₹104,260: the "edge" is a long bias in a
  market that rose.

### The nine-trade result was luck

An earlier check on the futures overlap produced 55.6% win and +₹210,214 over
**nine trades**. On 3 years of the same rule: 35.4% win and −₹513,294. Nine
trades is not a sample; it is an anecdote. The bootstrap interval on it included
zero, which was the signal that it could not be trusted.

## The session gate

The signal series is restricted to bars that overlap the 09:15–15:30 NSE
session. GIFT Nifty quotes ~20 hours a day; the NFO future trades 6¼. A bar that
closes outside the session can only produce a signal that has to be latched until
the next open, and by then the market has usually moved.

Measured on the same three years of 60-minute GIFT bars (16,226 bars, of which
**5,448 — 33.6% — are in session**):

| | Fresh signals | Share |
|---|---|---|
| In session | 480 | 49.7% |
| **Out of session (latched)** | **486** | **50.3%** |

The drift between the signal bar's close and the first in-session fill:

| Metric | Value |
|---|---|
| Median | **60.0 pt** |
| Mean | 83.6 pt |
| p90 | 177.5 pt |
| Max | 717.5 pt |

Against the 15-minute book's 50-point stop, **56.6% of latched entries were
already past the stop before the position existed**.

Gating the series changes the result materially. Shipped stop/trail settings,
60-minute book, 10 points of adverse slippage on every stop fill:

| Series | n | win | PF | net |
|---|---|---|---|---|
| All hours | 614 | 44.6% | 1.18 | −₹54,088 |
| **Session-gated** | 281 | **50.9%** | **1.31** | **+₹242,793** |

The gate is worth more than any stop or trail tuning tested, and it is a
structural fix rather than a fitted parameter: it removes trades the execution
venue could never have taken at the price the signal assumed.

## Bar alignment

The gate decides *which* bars the strategy sees. Alignment decides *what window
each bar covers*, and it matters just as much: the indicators are only
reconcilable against a chart if the bot buckets the same ticks into the same
windows.

Kite floors intraday bars to the **UTC epoch**. A 60-minute series therefore
comes back at 09:30, 10:30, 11:30 IST — six bars a day. A chart anchors the same
interval to the **09:15 session open** and draws 09:15–10:15, 10:15–11:15 — seven
bars a day. Same ticks, different windows, so SuperTrend and PSAR sat at levels
the chart never showed. No amount of correct arithmetic fixes a wrong window.

Both paths are now anchored to the session open:

| Path | Before | After |
|---|---|---|
| `gift-15m` (tick-built) | 08:30, 09:30, 10:30 … | **09:15, 10:15, 11:15 …** |
| `nifty-60m` (Kite history) | 09:30, 10:30 … (6/day) | **09:15, 10:15 … (7/day)** |

`bucket_start()` anchors to `MARKET_OPEN` instead of the epoch, and
`NiftyBarSource` fetches 15-minute source bars and re-buckets them with
`align_to_session()`, because Kite will not serve a session-aligned 60-minute
series directly.

The indicator arithmetic itself was verified against the Pine reference on the
same 16,226 bars: **PSAR is exact** (max difference 0.000000), and SuperTrend
matches `ta.supertrend` to within 0.01 points after its 14-bar warm-up.

A bar time without an offset is read as **exchange time**, never as the host's
zone. Kite returns naive timestamps, and `datetime.timestamp()` on a naive value
assumes the host's timezone — so the same bar floored to 09:15 on an IST machine
and to 14:45 in a UTC container. That is a silent 5h30m shift in every bucket,
and it is invisible to anyone developing in IST. The container runs in UTC, so
this is the path that matters.

## Conclusions to carry forward

1. **The exit rule is not the problem.** Flip is modestly better than flat on the
   payoff ratio, which is why it is the default, but neither clears costs.
2. **No simple filter rescues it.** MA50/100/200/500, session gating, EOD
   flattening, and time exits were all tested; only the 200MA row is positive and
   it fails out-of-sample.
3. **ADX makes it worse, monotonically.** On NIFTY 60-minute bars the unfiltered
   baseline is 177 trades / 41.2% / +₹160,318. ADX>15 gives 175 trades / 41.1% /
   +₹92,221; ADX>18 loses money; ADX>20 and ADX>25 lose more. The win rate does
   rise at high thresholds (44.2% at ADX>25) — but it rises by removing the large
   winners along with the losers, so the net falls. A higher win rate is not the
   same as a better strategy, and this is the clearest example of it in the repo.
   MA50 is the only filter that helped (87 trades / 51.7% / +₹247,679), and it is
   still a fit: negative second half, a CI that includes zero, and 48% of gross
   profit in one trade.
4. **Costs, not signal quality, are the binding constraint.** Any change that
   increases trade frequency makes this worse.
5. **The next real test needs a futures series.** Expired contracts are not in
   Kite's instrument dump, so multi-year futures history has to be sourced
   elsewhere before any of this can be called measured on the traded instrument.

## Reproducing

```bash
cd bot
../.venv/bin/python ../scripts/backtest.py --years 3 --cache /tmp/gift_3y_60m.json
../.venv/bin/python ../scripts/backtest.py --cache /tmp/gift_3y_60m.json \
    --trend-ma 200 --session nse --walk-forward
```

Signal generation is verified against `strategy.evaluate()` bar-for-bar across
16,158 bars (0 mismatches), so the replay is the bot's own rule and not an
approximation of it.
