"""SQLite persistence for paper positions, order book, trades, balance and metrics.

Design notes
------------
* WAL journal + 5s busy timeout: the tick feed, the strategy engine, the mark
  loop and the API threadpool all hit the same file concurrently.
* Money is stored as REAL (SQLite has no DECIMAL); quantities are INTEGER.
* ``open_positions`` rows carry a ``position_key`` (+1 long / -1 short) so an
  opposite trade knows it must close the existing position instead of stacking.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

from config import settings

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id                    INTEGER PRIMARY KEY CHECK (id = 1),
    initial_balance       REAL NOT NULL,
    balance               REAL NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol                TEXT NOT NULL,
    position_key          INTEGER NOT NULL CHECK (position_key IN (1, -1)),
    quantity              INTEGER NOT NULL,
    entry_price           REAL NOT NULL,
    entry_time            TEXT NOT NULL,
    last_price            REAL,
    sl_price              REAL,
    trailing_stop         REAL,
    status                TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED')),
    exit_price            REAL,
    exit_time             TEXT,
    realized_pnl          REAL NOT NULL DEFAULT 0,
    fees                  REAL NOT NULL DEFAULT 0,
    mode                  TEXT NOT NULL DEFAULT 'PAPER',
    source                TEXT NOT NULL DEFAULT 'WEBHOOK',
    strategy              TEXT,
    notes                 TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status, symbol);

CREATE TABLE IF NOT EXISTS orders (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    transaction_type      TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
    quantity              INTEGER NOT NULL,
    price                 REAL,
    order_type            TEXT NOT NULL,
    product               TEXT NOT NULL,
    status                TEXT NOT NULL,
    mode                  TEXT NOT NULL,
    source                TEXT NOT NULL,
    exchange_order_id     TEXT,
    position_id           INTEGER,
    message               TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders (ts DESC);

CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id           INTEGER NOT NULL,
    symbol                TEXT NOT NULL,
    direction             TEXT NOT NULL CHECK (direction IN ('LONG', 'SHORT')),
    quantity              INTEGER NOT NULL,
    entry_price           REAL NOT NULL,
    exit_price            REAL NOT NULL,
    entry_time            TEXT NOT NULL,
    exit_time             TEXT NOT NULL,
    gross_pnl             REAL NOT NULL,
    fees                  REAL NOT NULL,
    net_pnl               REAL NOT NULL,
    mode                  TEXT NOT NULL,
    exit_reason           TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades (exit_time DESC);

CREATE TABLE IF NOT EXISTS equity_curve (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT NOT NULL,
    equity                REAL NOT NULL,
    balance               REAL NOT NULL,
    unrealized_pnl        REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                    TEXT NOT NULL,
    action                TEXT NOT NULL,
    payload               TEXT,
    accepted              INTEGER NOT NULL,
    reason                TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals (ts DESC);

-- GIFT Nifty candles built from live ticks. Kite serves no NSEIX history, so
-- this table *is* the warmup: it lets the strategy survive a restart instead of
-- re-accumulating 20 bars from scratch.
CREATE TABLE IF NOT EXISTS gift_candles (
    start                 TEXT PRIMARY KEY,
    open                  REAL NOT NULL,
    high                  REAL NOT NULL,
    low                   REAL NOT NULL,
    close                 REAL NOT NULL,
    volume                REAL NOT NULL DEFAULT 0,
    ticks                 INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gift_candles_start ON gift_candles (start DESC);
"""


def now_ist() -> datetime:
    return datetime.now(IST)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or now_ist()).isoformat(timespec="seconds")


_in_init_db = False


def _connect(path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection, creating the schema on first touch.

    ``init_db`` runs here rather than in every helper so a fresh ``DB_PATH``
    works for reads, writes and every worker thread alike. ``init_db`` itself
    calls ``_connect``, so the recursion is guarded by a flag.
    """
    global _in_init_db
    db_path = Path(path or settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    if not _in_init_db:
        _in_init_db = True
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT OR IGNORE INTO account (id, initial_balance, balance, updated_at) "
                "VALUES (1, ?, ?, ?)",
                (
                    float(settings.virtual_balance),
                    float(settings.virtual_balance),
                    iso(),
                ),
            )
        finally:
            _in_init_db = False
    return conn


@contextmanager
def session(path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(path: Optional[str] = None, initial_balance: Optional[float] = None) -> None:
    balance = float(initial_balance if initial_balance is not None else settings.virtual_balance)
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO account (id, initial_balance, balance, updated_at) "
            "VALUES (1, ?, ?, ?)",
            (balance, balance, iso()),
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
def get_account(path: Optional[str] = None) -> dict[str, Any]:
    init_db(path)
    with session(path) as conn:
        row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
        return dict(row)


def adjust_balance(delta: float, path: Optional[str] = None) -> float:
    with session(path) as conn:
        conn.execute(
            "UPDATE account SET balance = balance + ?, updated_at = ? WHERE id = 1",
            (delta, iso()),
        )
        row = conn.execute("SELECT balance FROM account WHERE id = 1").fetchone()
        return float(row["balance"])


def reset_account(
    path: Optional[str] = None, initial_balance: Optional[float] = None
) -> None:
    """Wipe all trading state and restore the virtual balance."""
    balance = float(initial_balance if initial_balance is not None else settings.virtual_balance)
    with session(path) as conn:
        for table in ("positions", "orders", "trades", "equity_curve", "signals"):
            conn.execute(f"DELETE FROM {table}")
        conn.execute(
            "UPDATE account SET initial_balance = ?, balance = ?, updated_at = ? WHERE id = 1",
            (balance, balance, iso()),
        )


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------
def get_open_positions(
    path: Optional[str] = None, mode: Optional[str] = None
) -> list[dict[str, Any]]:
    query = "SELECT * FROM positions WHERE status = 'OPEN'"
    params: list[Any] = []
    if mode:
        query += " AND mode = ?"
        params.append(mode)
    query += " ORDER BY entry_time"
    with session(path) as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_open_position(
    symbol: str, mode: str, path: Optional[str] = None
) -> Optional[dict[str, Any]]:
    with session(path) as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE status = 'OPEN' AND symbol = ? AND mode = ? "
            "ORDER BY entry_time DESC LIMIT 1",
            (symbol, mode),
        ).fetchone()
        return dict(row) if row else None


def open_position(symbol: str, quantity: int, price: float, path: Optional[str] = None, **extra: Any) -> int:
    cols = {
        "symbol": symbol,
        "position_key": 1 if quantity > 0 else -1,
        "quantity": abs(quantity),
        "entry_price": price,
        "entry_time": iso(),
        "last_price": price,
        "mode": extra.pop("mode", "PAPER"),
        "source": extra.pop("source", "WEBHOOK"),
        "strategy": extra.pop("strategy", "Surikbs"),
        "sl_price": extra.pop("sl_price", None),
        "notes": extra.pop("notes", None),
    }
    cols.update(extra)
    placeholders = ", ".join("?" for _ in cols)
    with session(path) as conn:
        cur = conn.execute(
            f"INSERT INTO positions ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(cols.values()),
        )
        return int(cur.lastrowid)


def update_position_price(
    position_id: int, price: float, path: Optional[str] = None
) -> None:
    with session(path) as conn:
        conn.execute(
            "UPDATE positions SET last_price = ? WHERE id = ? AND status = 'OPEN'",
            (price, position_id),
        )


def close_position(
    position_id: int,
    exit_price: float,
    path: Optional[str] = None,
    fees: float = 0.0,
    exit_reason: str = "SIGNAL",
    quantity: Optional[int] = None,
) -> dict[str, Any]:
    """Close all or part of a position, write the trade row and settle balance."""
    with session(path) as conn:
        row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
        if row is None:
            raise KeyError(f"Position {position_id} not found")
        if row["status"] != "OPEN":
            raise ValueError(f"Position {position_id} is already closed")

        pos = dict(row)
        direction = "LONG" if pos["position_key"] == 1 else "SHORT"
        open_quantity = int(pos["quantity"])
        qty = open_quantity if quantity is None else int(quantity)
        if qty <= 0 or qty > open_quantity:
            raise ValueError(f"Close quantity must be between 1 and {open_quantity}")
        gross = (exit_price - pos["entry_price"]) * qty * pos["position_key"]
        net = gross - fees
        closed_at = iso()

        if qty == open_quantity:
            conn.execute(
                "UPDATE positions SET status='CLOSED', exit_price=?, exit_time=?, "
                "realized_pnl=realized_pnl + ?, fees=fees + ?, last_price=? WHERE id=?",
                (exit_price, closed_at, net, fees, exit_price, position_id),
            )
        else:
            conn.execute(
                "UPDATE positions SET quantity=quantity - ?, realized_pnl=realized_pnl + ?, "
                "fees=fees + ?, last_price=? WHERE id=?",
                (qty, net, fees, exit_price, position_id),
            )
        conn.execute(
            "INSERT INTO trades (position_id, symbol, direction, quantity, entry_price, "
            "exit_price, entry_time, exit_time, gross_pnl, fees, net_pnl, mode, exit_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                position_id,
                pos["symbol"],
                direction,
                qty,
                pos["entry_price"],
                exit_price,
                pos["entry_time"],
                closed_at,
                gross,
                fees,
                net,
                pos["mode"],
                exit_reason,
            ),
        )
        conn.execute(
            "UPDATE account SET balance = balance + ?, updated_at = ? WHERE id = 1",
            (net, closed_at),
        )
        return {
            "position_id": position_id,
            "symbol": pos["symbol"],
            "direction": direction,
            "quantity": qty,
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "exit_time": closed_at,
        }


# ---------------------------------------------------------------------------
# Orders / trades
# ---------------------------------------------------------------------------
def record_order(path: Optional[str] = None, **fields: Any) -> int:
    cols = {
        "ts": iso(),
        "symbol": fields["symbol"],
        "transaction_type": fields["transaction_type"],
        "quantity": fields["quantity"],
        "price": fields.get("price"),
        "order_type": fields.get("order_type", "MARKET"),
        "product": fields.get("product", "NRML"),
        "status": fields.get("status", "COMPLETE"),
        "mode": fields.get("mode", "PAPER"),
        "source": fields.get("source", "WEBHOOK"),
        "exchange_order_id": fields.get("exchange_order_id"),
        "position_id": fields.get("position_id"),
        "message": fields.get("message"),
    }
    placeholders = ", ".join("?" for _ in cols)
    with session(path) as conn:
        cur = conn.execute(
            f"INSERT INTO orders ({', '.join(cols)}) VALUES ({placeholders})",
            tuple(cols.values()),
        )
        return int(cur.lastrowid)


def get_pending_orders(
    mode: str, symbol: Optional[str] = None, path: Optional[str] = None
) -> list[dict[str, Any]]:
    statuses = ("OPEN", "TRIGGER_PENDING", "PENDING")
    query = "SELECT * FROM orders WHERE mode = ? AND status IN (?, ?, ?)"
    params: list[Any] = [mode, *statuses]
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    query += " ORDER BY id"
    return _fetch_all(query, params, path)


def record_signal(
    action: str,
    payload: str,
    accepted: bool,
    reason: str,
    path: Optional[str] = None,
) -> None:
    init_db(path)
    with session(path) as conn:
        conn.execute(
            "INSERT INTO signals (ts, action, payload, accepted, reason) VALUES (?, ?, ?, ?, ?)",
            (iso(), action, payload, int(accepted), reason),
        )


def record_equity(
    equity: float, balance: float, unrealized: float, path: Optional[str] = None
) -> None:
    with session(path) as conn:
        conn.execute(
            "INSERT INTO equity_curve (ts, equity, balance, unrealized_pnl) VALUES (?, ?, ?, ?)",
            (iso(), equity, balance, unrealized),
        )


def get_equity_curve(
    limit: int = 5000, path: Optional[str] = None
) -> list[dict[str, Any]]:
    """Marked equity samples in chronological order (oldest first)."""
    rows = _fetch_all(
        "SELECT * FROM equity_curve ORDER BY ts DESC, id DESC LIMIT ?", [limit], path
    )
    return list(reversed(rows))


def clear_equity_curve(path: Optional[str] = None) -> None:
    with session(path) as conn:
        conn.execute("DELETE FROM equity_curve")


# ---------------------------------------------------------------------------
# GIFT Nifty candles
# ---------------------------------------------------------------------------
def record_gift_candle(
    start: str,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 0.0,
    ticks: int = 0,
    path: Optional[str] = None,
) -> None:
    """Upsert one GIFT Nifty bar (``start`` is the bucket timestamp)."""
    init_db(path)
    with session(path) as conn:
        conn.execute(
            "INSERT INTO gift_candles (start, open, high, low, close, volume, ticks, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(start) DO UPDATE SET open=excluded.open, high=excluded.high, "
            "low=excluded.low, close=excluded.close, volume=excluded.volume, ticks=excluded.ticks",
            (start, open_, high, low, close, volume, ticks, iso()),
        )


def get_gift_candles(
    limit: int = 2000, path: Optional[str] = None
) -> list[dict[str, Any]]:
    """Persisted GIFT Nifty bars in chronological order (oldest first)."""
    rows = _fetch_all(
        "SELECT * FROM gift_candles ORDER BY start DESC LIMIT ?", [limit], path
    )
    return list(reversed(rows))


def gift_candle_count(path: Optional[str] = None) -> int:
    init_db(path)
    with session(path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM gift_candles").fetchone()
        return int(row["n"])


def _fetch_all(query: str, params: Sequence[Any], path: Optional[str]) -> list[dict[str, Any]]:
    init_db(path)
    with session(path) as conn:
        return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_orders(limit: int = 500, path: Optional[str] = None) -> list[dict[str, Any]]:
    return _fetch_all("SELECT * FROM orders ORDER BY ts DESC, id DESC LIMIT ?", [limit], path)


def get_trades(limit: int = 1000, path: Optional[str] = None) -> list[dict[str, Any]]:
    return _fetch_all(
        "SELECT * FROM trades ORDER BY exit_time DESC, id DESC LIMIT ?", [limit], path
    )


def get_signals(limit: int = 100, path: Optional[str] = None) -> list[dict[str, Any]]:
    return _fetch_all("SELECT * FROM signals ORDER BY id DESC LIMIT ?", [limit], path)


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------
def _max_drawdown(equity: Iterable[float]) -> float:
    peak = float("-inf")
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)
    return worst * 100.0


def get_performance_metrics(
    path: Optional[str] = None,
    open_positions: Optional[list[dict[str, Any]]] = None,
    prices: Optional[dict[str, float]] = None,
    mode: Optional[str] = None,
) -> dict[str, Any]:
    """Aggregate realised stats from the trade ledger plus live unrealised PnL."""
    account = get_account(path)
    trade_query = "SELECT * FROM trades"
    trade_params: list[Any] = []
    if mode:
        trade_query += " WHERE mode = ?"
        trade_params.append(mode)
    trade_query += " ORDER BY exit_time"
    trades = _fetch_all(trade_query, trade_params, path)
    equity = (
        [float(r["equity"]) for r in get_equity_curve(path=path)]
        if mode in (None, "PAPER")
        else []
    )

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] < 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))
    net_pnl = sum(t["net_pnl"] for t in trades)

    realized_today = 0.0
    today = now_ist().date().isoformat()
    for t in trades:
        _source = t["exit_time"] or t["entry_time"] or ""
        if _source.startswith(today):
            realized_today += float(t["net_pnl"])

    unrealized = 0.0
    positions = (
        open_positions
        if open_positions is not None
        else get_open_positions(path, mode=mode)
    )
    for pos in positions:
        mark = (prices or {}).get(pos["symbol"]) or pos["last_price"] or pos["entry_price"]
        unrealized += (mark - pos["entry_price"]) * pos["quantity"] * pos["position_key"]

    start_equity = float(equity[0]) if equity else account["initial_balance"]
    current_equity = float(account["balance"]) + unrealized
    total_return = (
        (current_equity / account["initial_balance"] - 1.0) * 100.0
        if account["initial_balance"]
        else 0.0
    )

    return {
        "initial_balance": account["initial_balance"],
        "balance": account["balance"],
        "equity": current_equity,
        "unrealized_pnl": unrealized,
        "realized_total": net_pnl,
        "realized_today": realized_today,
        "total_trades": len(trades),
        "win_rate": (len(wins) / len(trades) * 100.0) if trades else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "max_drawdown_pct": _max_drawdown(equity) if equity else 0.0,
        "start_equity": start_equity,
        "total_return_pct": total_return,
    }
