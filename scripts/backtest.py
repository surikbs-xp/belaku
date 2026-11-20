"""Backtest the strategy on real Kite history, over years rather than months.

Why this exists
---------------
The engine's own ledger is the only other record of what the strategy did, and
that record is short: a few dozen trades, all of them in whatever regime the
market happened to be in. A result measured on nine trades is not a result. This
tool pulls several years of closed bars from Kite and replays the *live* rule on
them, so a claim about the strategy can be checked against a sample big enough to
mean something.

The signal comes from ``strategy.evaluate`` itself, not a reimplementation. That
is the point: a backtest that shares no code with the bot can only ever say that
some indicators are profitable, not that *this* bot is.

What it fills on
----------------
Bars come from one instrument, and so do the fills. With ``--instrument gift``
that is the GIFT Nifty index, which is what the code's GIFT index book trades and
the only continuous series spanning years - NSE futures expire monthly and Kite
serves no history for the expired ones. Trading the future instead would add the
spot/future basis, whose change is a ~40 pt/bar random walk at 60 minutes: noise
large enough to swamp a 60-minute edge without biasing it either way.

Costs are the real thing: ``order_manager.estimate_charges`` at 25,000. On one lot
that is ~14.85 points per round trip, and STT scales with turnover, so a bigger
lot does not amortise it.

Usage
-----
    cd bot
    ../.venv/bin/python ../scripts/backtest.py --years 3
    ../.venv/bin/python ../scripts/backtest.py --years 3 --walk-forward
    ../.venv/bin/python ../scripts/backtest.py --years 3 --exit flat --session nse
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import statistics
import sys
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
sys.path.insert(0, str(BOT_DIR))

import kite_client  # noqa: E402
import strategy  # noqa: E402
from order_manager import estimate_charges  # noqa: E402

# The two series a strategy can be signalled from. The futures token cannot be
# used: Kite only lists live contracts, so there is no multi-year future series
# to replay.
GIFT_TOKEN = 291849
NIFTY_SPOT_TOKEN = 256265

# Which token each --source reads. Naming them here rather than at the call site
# is what keeps the signal series and the fill series from drifting apart.
SOURCE_TOKENS = {"gift": GIFT_TOKEN, "nifty": NIFTY_SPOT_TOKEN}

LOT = 65
# Kite's own limits: 400 days per request above 15 minutes, 200 at or below it.
MAX_CHUNK_DAYS = 395

NSE_START, NSE_END = "09:15", "15:30"

INTERVALS = ["60minute", "30minute", "15minute", "day"]

# Signals for the series just evaluated, so the walk-forward does not recompute
# them once per grid entry. Keyed by first timestamp and length.
_SIG_CACHE: dict[tuple, list[str]] = {}


def fetch(interval: str, years: int, source: str = "gift") -> list[dict]:
    """Closed bars from Kite, oldest first, de-duplicated across windows.

    ``source`` picks the series the *signal* is computed on. It used to be
    hardcoded to GIFT, which meant a run described as "NIFTY signals" was in fact
    reading GIFT bars - the one thing a backtest must not get wrong.
    """
    token = SOURCE_TOKENS.get(source)
    if token is None:
        raise SystemExit(f"unknown source {source!r}; expected one of {sorted(SOURCE_TOKENS)}")
    client = kite_client.get_client()
    if not client.sdk_ready:
        raise SystemExit("Kite SDK is not available; the .venv has no kiteconnect")
    if not client.auth_status().get("authenticated"):
        raise SystemExit("Kite session is not authenticated; refresh the daily token")

    end = dt.datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
    start = end - dt.timedelta(days=365 * years + 5)

    rows: list[dict] = []
    seen: set[str] = set()
    cur = start
    while cur < end:
        nxt = min(cur + dt.timedelta(days=MAX_CHUNK_DAYS), end)
        batch = client.historical_data(token, cur, nxt, interval) or []
        for row in batch:
            stamp = row["date"].replace(tzinfo=None).isoformat(sep=" ")
            if stamp in seen:
                continue
            seen.add(stamp)
            rows.append({
                "time": stamp,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
        print(f"  {cur.date()} .. {nxt.date()}: {len(batch):5d} rows, {len(rows)} total")
        cur = nxt

    rows.sort(key=lambda r: r["time"])
    return rows


def signals(candles: list[dict], params: dict | None = None) -> list[str]:
    """The live rule, one signal per bar.

    Runs ``strategy.evaluate`` on the series up to each bar. That is slower than
    vectorising the indicators here, and deliberately so: the result is the bot's
    own verdict, so a change to the strategy cannot silently diverge from the
    backtest that is supposed to be checking it.

    ``params`` carries the entry filters. Passing ``{}`` here - which is what this
    did - meant the ADX and MA gates could never be measured, so the numbers
    quoted for them came from somewhere other than this script.
    """
    params = params or {}
    needed = strategy.required_bars(params)
    out: list[str] = []
    for i in range(len(candles)):
        if i + 1 < needed:
            out.append(strategy.NO_SIGNAL)
            continue
        try:
            out.append(strategy.evaluate(candles[: i + 1], params).signal)
        except strategy.InsufficientData:
            out.append(strategy.NO_SIGNAL)
    return out


def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= n:
            running -= values[i - n]
        if i >= n - 1:
            out[i] = running / n
    return out


def in_nse(stamp: str) -> bool:
    return NSE_START <= stamp[11:16] <= NSE_END


def replay(
    candles: list[dict],
    sig: list[str],
    *,
    exit_rule: str = "flip",
    trend_ma: int | None = None,
    session: str | None = None,
    eod_flatten: bool = False,
    max_hold: int | None = None,
) -> list[dict]:
    """Walk the bars once and record round trips.

    Entry needs a *transition* into a direction, matching the engine: a bar that
    merely still agrees is not a new signal. ``exit_rule='flip'`` closes when the
    pair turns the other way (the shipped rule); ``'flat'`` closes on the first
    disagreeing bar (the rule it replaced).
    """
    close = [c["close"] for c in candles]
    stamps = [c["time"] for c in candles]
    dates = [s[:10] for s in stamps]
    ma = sma(close, trend_ma) if trend_ma else None

    trades: list[dict] = []
    pos = 0
    entry = entry_i = None

    for i in range(1, len(candles)):
        prev, cur = sig[i - 1], sig[i]

        if pos != 0:
            exit_px = why = None
            if exit_rule == "flat" and cur == strategy.NO_SIGNAL \
                    and prev != strategy.NO_SIGNAL:
                exit_px, why = close[i], "FLAT"
            elif exit_rule == "flip":
                if (pos == 1 and cur == strategy.BEAR) \
                        or (pos == -1 and cur == strategy.BULL):
                    exit_px, why = close[i], "FLIP"
            if max_hold is not None and (i - entry_i) >= max_hold:
                exit_px, why = close[i], "TIME"
            if eod_flatten and i + 1 < len(candles) and dates[i] != dates[i + 1]:
                exit_px, why = close[i], "EOD"
            if exit_px is not None:
                trades.append({
                    "entry_at": stamps[entry_i], "exit_at": stamps[i],
                    "dir": pos, "entry": entry, "exit": exit_px, "why": why,
                    "pts": (exit_px - entry) * pos, "bars": i - entry_i,
                })
                pos = 0
                entry = entry_i = None

        if pos == 0 and cur in (strategy.BULL, strategy.BEAR) and cur != prev:
            if session == "nse" and not in_nse(stamps[i]):
                continue
            if ma is not None:
                m = ma[i]
                if m is None:
                    continue
                if not ((cur == strategy.BULL and close[i] > m)
                        or (cur == strategy.BEAR and close[i] < m)):
                    continue
            pos = 1 if cur == strategy.BULL else -1
            entry = close[i]
            entry_i = i

    return trades


def charges() -> float:
    return estimate_charges(25_000 * LOT, 25_000 * LOT)


def summarise(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    pts = [t["pts"] for t in trades]
    gross = sum(pts) * LOT
    cost = charges() * len(trades)
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    return {
        "n": len(trades),
        "win": 100 * len(wins) / len(trades),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "gross": gross,
        "cost": cost,
        "net": gross - cost,
        "net_per": (gross - cost) / len(trades),
        "gross_per_pts": statistics.mean(pts),
        "pf": (sum(wins) / abs(sum(losses))) if losses else float("inf"),
        "median_bars": statistics.median(t["bars"] for t in trades),
        "max_bars": max(t["bars"] for t in trades),
    }


def line(label: str, s: dict) -> str:
    if not s.get("n"):
        return f"{label:<38} no trades"
    return (
        f"{label:<38} n={s['n']:4d}  win={s['win']:5.1f}%  "
        f"gross/t={s['gross_per_pts']:+7.2f}pt  PF={s['pf']:5.2f}  "
        f"net=Rs{s['net']:>11,.0f}  /t=Rs{s['net_per']:>8,.0f}  "
        f"hold~{s['median_bars']:.0f}"
    )


def bootstrap(trades: list[dict], trials: int = 10_000) -> list[float]:
    """Per-trade *net* profit in rupees, resampled.

    An interval that includes zero means the sample cannot separate the strategy
    from a coin that pays its costs: whatever the point estimate says, the trade
    list is consistent with no edge at all.
    """
    pts = [t["pts"] for t in trades]
    cost_pts = charges() / LOT
    random.seed(7)
    return sorted(
        (statistics.mean(random.choices(pts, k=len(pts))) - cost_pts) * LOT
        for _ in range(trials)
    )


def report(rows: list[dict], args) -> None:
    print(f"\n{len(rows)} bars, {rows[0]['time']} .. {rows[-1]['time']}")
    print("Computing signals from strategy.evaluate() ...")
    sig = signals(
        [{"time": r["time"], "open": r["open"], "high": r["high"],
          "low": r["low"], "close": r["close"]} for r in rows],
        {"filter_adx": args.filter_adx, "filter_ma": args.filter_ma},
    )
    # Signals depend only on the series, and every grid point in a walk-forward
    # reuses the same one. Evaluating per configuration instead would recompute
    # them 20 times over and turn a two-minute run into an hour.
    _SIG_CACHE[(rows[0]["time"], len(rows), args.filter_adx, args.filter_ma)] = sig
    print(f"  {sum(1 for s in sig if s != strategy.NO_SIGNAL)} directional bars of {len(sig)}")
    print(f"  cost per round trip: Rs{charges():,.2f} = {charges() / LOT:.2f} points\n")

    trades = replay(rows, sig, exit_rule=args.exit, trend_ma=args.trend_ma,
                    session=args.session, eod_flatten=args.eod, max_hold=args.max_hold)
    s = summarise(trades)
    print(line("this configuration", s))
    if not trades:
        return

    means = bootstrap(trades)
    lo, hi = means[250], means[9_750]
    positive = sum(1 for m in means if m > 0) / len(means)
    print(f"  bootstrap 95% CI on net/trade: [Rs{lo:,.0f}, Rs{hi:,.0f}]  "
          f"P(profitable)={positive:.1%}")

    print("\nper calendar year")
    for year in sorted({t["exit_at"][:4] for t in trades}):
        sub = [t for t in trades if t["exit_at"][:4] == year]
        y = summarise(sub)
        print(f"  {year}  n={y['n']:4d}  win={y['win']:5.1f}%  net=Rs{y['net']:>11,.0f}")

    half = len(trades) // 2
    for name, part in (("first half", trades[:half]), ("second half", trades[half:])):
        h = summarise(part)
        print(f"  {name:<11} n={h['n']:4d}  PF={h['pf']:5.2f}  net=Rs{h['net']:>11,.0f}")

    longs = [t for t in trades if t["dir"] == 1]
    shorts = [t for t in trades if t["dir"] == -1]
    for name, part in (("long", longs), ("short", shorts)):
        if part:
            p = summarise(part)
            print(f"  {name:<11} n={p['n']:4d}  win={p['win']:5.1f}%  net=Rs{p['net']:>11,.0f}")

    best = max(trades, key=lambda t: t["pts"])
    gross_pts = sum(t["pts"] for t in trades)
    if gross_pts > 0:
        print(f"  biggest trade {best['pts']:+.1f} pts = "
              f"{100 * best['pts'] / gross_pts:.0f}% of gross")
    else:
        print(f"  biggest trade {best['pts']:+.1f} pts (gross is negative, "
              f"so it cannot carry the result)")

    hold = rows[-1]["close"] - rows[strategy.required_bars()]["close"]
    print(f"  buy & hold same span: {hold:+.0f} pts = Rs{hold * LOT:,.0f} (no costs)")


def walk_forward(rows: list[dict], sig: list[str], args) -> None:
    """Choose a configuration on one year, trade the next. The only honest test
    of a filter found by sweeping: a config that only works in the year it was
    fitted to is a curve fit, however good its backtest looked."""
    years = sorted({r["time"][:4] for r in rows})
    if len(years) < 2:
        print("\nnot enough years for a walk-forward")
        return

    print("\nwalk-forward: fit on one year, trade the next")
    grid = [
        (ma, rule, eod)
        for ma in (None, 50, 100, 200, 500)
        for rule in ("flip", "flat")
        for eod in (False, True)
    ]
    first = {y: next(i for i, r in enumerate(rows) if r["time"][:4] == y) for y in years}
    for train, test in zip(years, years[1:]):
        train_rows = [r for r in rows if r["time"][:4] == train]
        train_sig = sig[first[train]: first[train] + len(train_rows)]

        best = None
        for ma, rule, eod in grid:
            t = replay(train_rows, train_sig, exit_rule=rule, trend_ma=ma,
                       session=args.session, eod_flatten=eod)
            if len(t) < 10:
                continue
            s = summarise(t)
            if best is None or s["net"] > best[0]["net"]:
                best = (s, (ma, rule, eod))
        if best is None:
            continue

        s, (ma, rule, eod) = best
        test_rows = [r for r in rows if r["time"][:4] == test]
        test_sig = sig[first[test]: first[test] + len(test_rows)]
        out = summarise(replay(test_rows, test_sig, exit_rule=rule, trend_ma=ma,
                               session=args.session, eod_flatten=eod))
        print(f"  fit {train}: MA={ma} {rule} eod={eod} -> train Rs{s['net']:,.0f}")
        if out.get("n"):
            print(f"  trade {test}: net Rs{out['net']:>11,.0f}  n={out['n']:3d}  "
                  f"win={out['win']:.1f}%")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--years", type=int, default=3, help="how much history to pull")
    p.add_argument("--interval", default="60minute", choices=INTERVALS)
    p.add_argument("--exit", default="flip", choices=["flip", "flat"])
    p.add_argument("--trend-ma", type=int, default=None,
                   help="only enter when price is on the trend's side of this SMA")
    p.add_argument("--filter-adx", type=float, default=0.0,
                   help="only enter when ADX(14) is above this (0 = off)")
    p.add_argument("--filter-ma", type=int, default=0,
                   help="only enter when close is above this SMA (0 = off)")
    p.add_argument("--source", default="gift", choices=sorted(SOURCE_TOKENS),
                   help="series the signal is computed on (default: gift)")
    p.add_argument("--session", default=None, choices=["nse"],
                   help="nse restricts entries to 09:15-15:30 IST")
    p.add_argument("--eod", action="store_true", help="flatten before the daily close")
    p.add_argument("--max-hold", type=int, default=None, help="bars before a time exit")
    p.add_argument("--walk-forward", action="store_true",
                   help="fit on each year and trade the next")
    p.add_argument("--cache", default=None, help="JSON path to reuse instead of refetching")
    args = p.parse_args()

    if args.cache and Path(args.cache).exists():
        import json
        rows = json.load(open(args.cache))
        print(f"loaded {len(rows)} bars from {args.cache}")
    else:
        print(f"fetching {args.years}y of {args.interval} {args.source} bars")
        rows = fetch(args.interval, args.years, args.source)
        if args.cache:
            import json
            json.dump(rows, open(args.cache, "w"))
            print(f"cached to {args.cache}")

    if len(rows) < strategy.required_bars() + 10:
        raise SystemExit("not enough bars returned")

    report(rows, args)
    if args.walk_forward:
        walk_forward(rows, _SIG_CACHE[(rows[0]["time"], len(rows), args.filter_adx, args.filter_ma)], args)


if __name__ == "__main__":
    main()
