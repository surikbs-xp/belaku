"""GIFT Nifty execution bot - one process, one runtime, no TradingView.

    python bot.py

This single process owns everything:

* the **GIFT Nifty tick feed** - a KiteTicker websocket subscription that
  builds OHLCV candles from live ticks (`gift_nifty.py`);
* the **strategy engine** - evaluates the ``st.pine`` conditions on those
  candles and sends orders to the NFO Nifty future (`engine.py`);
* the **mark loop** - marks positions to market and samples the equity curve;
* the **JSON API** and the **dashboard** - `web/` served as static files.

Signals and execution are deliberately different instruments: GIFT Nifty is
quote-only on Kite (it cannot be traded), so it is used as the *signal* source
and the front-month Nifty future as the *execution* venue.

Concurrency model
-----------------
FastAPI's event loop owns the socket; handlers are synchronous ``def`` so
Starlette runs them in its threadpool and blocking SQLite/Kite calls never
stall the loop. The tick feed, the strategy engine and the mark loop are daemon
threads. All trading state is mutated through ``OrderManager``, which
serialises execution behind its own lock.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database as db
import kite_auth
from config import LIVE, PAPER, get_execution_mode, set_execution_mode, settings, validate
from engine import get_engine
from gift_nifty import GIFT_KEY, get_feed, reset_feed
from kite_client import future_symbol, get_client, market_status
from order_manager import get_order_manager

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("bot")

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

STARTED_AT = time.time()
STARTED_ISO = db.iso()

# ---------------------------------------------------------------------------
# Shared runtime state (this process only - that is the point)
# ---------------------------------------------------------------------------
STATUS_LOCK = threading.RLock()
STATUS: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "last_signal_at": None,
    "last_signal_action": None,
    "last_result": None,
    "received": 0,
    "accepted": 0,
    "rejected": 0,
    "heartbeat_ts": None,
    "engine": {
        "ticks": 0,
        "last_tick_at": None,
        "last_mark_at": None,
        "marks": 0,
        "interval_seconds": 0.0,
        "last_error": None,
    },
}

_stop = threading.Event()


def _connect_kite_on_startup() -> None:
    """Start market data with a valid token or open the interactive login."""
    status = kite_auth.token_status()
    if status.get("valid"):
        feed = _start_gift_feed()
        if feed["start_requested"]:
            log.info("Kite token is valid; GIFT Nifty feed startup requested")
        else:
            log.error("Kite token is valid but the GIFT feed failed to start: %s", feed.get("error"))
        return

    if status.get("state") not in {"no_token", "expired"}:
        log.warning("Kite startup connection unavailable: %s", status.get("detail"))
        return

    login_url = status.get("login_url")
    if not login_url:
        log.warning("Kite login is required, but a login URL could not be created: %s", status.get("detail"))
        return

    try:
        opened = webbrowser.open(login_url, new=2)
    except webbrowser.Error as exc:
        log.warning("Kite login is required; could not open a browser: %s", exc)
        return
    if opened:
        log.info("Kite login is required; opened Zerodha in the local browser")
    else:
        log.warning("Kite login is required; open the Kite login from the dashboard")


def _iso_age_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=db.IST)
    return (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()


def _status_snapshot() -> dict[str, Any]:
    with STATUS_LOCK:
        return json.loads(json.dumps(STATUS, default=str))


# ---------------------------------------------------------------------------
# Mark-to-market loop
# ---------------------------------------------------------------------------
def mark_loop(stop: threading.Event, interval: Optional[float] = None) -> None:
    """Mark open positions to market and sample equity on a fixed cadence.

    Separate from the strategy engine: this keeps unrealised PnL and the equity
    curve live while the strategy waits for a signal. It never places orders.
    """
    period = float(interval if interval is not None else settings.engine_interval_seconds)
    manager = get_order_manager()
    log.info("Mark loop started (every %.1fs)", period)
    while not stop.wait(period):
        tick_started = time.perf_counter()
        with STATUS_LOCK:
            STATUS["heartbeat_ts"] = time.time()
            engine = STATUS["engine"]
            engine["ticks"] = int(engine["ticks"]) + 1
            engine["last_tick_at"] = db.iso()
            engine["interval_seconds"] = period
        try:
            positions = db.get_open_positions()
            if positions:
                manager._mark_equity()
                with STATUS_LOCK:
                    engine = STATUS["engine"]
                    engine["marks"] = int(engine["marks"]) + 1
                    engine["last_mark_at"] = db.iso()
                    engine["last_error"] = None
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Mark failed: %s", exc)
            with STATUS_LOCK:
                STATUS["engine"]["last_error"] = str(exc)
        with STATUS_LOCK:
            STATUS["engine"]["last_duration_ms"] = round(
                (time.perf_counter() - tick_started) * 1000, 1
            )
    log.info("Mark loop stopped")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ModeRequest(BaseModel):
    mode: str = Field(..., description="PAPER or LIVE")
    confirm: bool = Field(
        default=False,
        description="Must be true to arm LIVE; guards against a stray click.",
    )


class PauseRequest(BaseModel):
    paused: bool = Field(..., description="true blocks new BUY/SELL entries")
    reason: str = Field(default="MANUAL", description="Audit label")


class PanicRequest(BaseModel):
    confirm: bool = Field(
        default=False,
        description="Must be true; the client shows a confirm dialog first.",
    )
    reason: str = Field(default="PANIC", description="Audit label")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    with STATUS_LOCK:
        STATUS.update(running=True, started_at=db.iso(), heartbeat_ts=time.time())

    # 1. GIFT Nifty tick feed -> candles (warmup carried over from SQLite).
    feed = get_feed()
    restored = feed.load_persisted()
    _connect_kite_on_startup()

    # 2. Strategy engine: evaluates GIFT Nifty bars, trades the NFO future.
    engine = get_engine()
    engine.start()

    # 3. Mark-to-market loop: keeps equity/PnL live between signals.
    marks = threading.Thread(target=mark_loop, args=(_stop,), name="marks", daemon=True)
    marks.start()

    log.info(
        "Bot ready - mode=%s db=%s signals=%s (%ss bars, %d restored) "
        "execution=%s dashboard=http://%s:%s/",
        get_execution_mode(), settings.db_path, GIFT_KEY,
        feed.seconds, restored, future_symbol(),
        settings.webhook_host, settings.webhook_port,
    )
    try:
        yield
    finally:
        _stop.set()
        with STATUS_LOCK:
            STATUS["running"] = False
        engine.stop()
        feed.stop()
        marks.join(timeout=5)
        log.info("Bot stopped")


app = FastAPI(
    title="GIFT Nifty Execution Bot",
    version="3.0.0",
    description=(
        "Single-process bot: GIFT Nifty tick feed, st.pine strategy engine, "
        "Nifty futures execution, JSON API and dashboard."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["status"])
def health() -> dict[str, Any]:
    return {"ok": True, "time": db.iso(), "uptime_seconds": round(time.time() - STARTED_AT, 1)}


@app.get("/api/status", tags=["status"])
def api_status() -> dict[str, Any]:
    """Execution mode, uptime, Kite authorization and strategy heartbeats."""
    client = get_client()
    auth = client.auth_status()
    manager = get_order_manager()
    market = market_status()
    snapshot = _status_snapshot()
    engine = snapshot.get("engine", {})
    age = _iso_age_seconds(engine.get("last_tick_at"))

    return {
        "mode": get_execution_mode(),
        "modes": [PAPER, LIVE],
        "live_configured": settings.is_live_configured,
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "started_at": STARTED_ISO,
        "server_time": db.iso(),
        "kite": {
            "authenticated": bool(auth.get("authenticated")),
            "detail": auth.get("detail") or "",
            "sdk_available": client.sdk_ready,
            "credentials_present": settings.has_credentials,
            "data_source": client.data_source(),
            "last_quote_at": client.last_quote_at.isoformat(timespec="seconds")
            if client.last_quote_at
            else None,
        },
        "strategy": {
            "paused": manager.paused,
            "heartbeat": {
                "ticks": engine.get("ticks", 0),
                "last_tick_at": engine.get("last_tick_at"),
                "age_seconds": round(age, 1) if age is not None else None,
                "interval_seconds": engine.get("interval_seconds"),
                "marks": engine.get("marks", 0),
                "last_mark_at": engine.get("last_mark_at"),
                "last_error": engine.get("last_error"),
                "last_duration_ms": engine.get("last_duration_ms"),
            },
        },
        "engine": get_engine().status(),
        "market": market,
        "symbol": {
            "current": manager.current_symbol(),
            "next": (
                (get_client().front_month_contract(months_ahead=1) or {}).get("tradingsymbol")
            )
            if get_execution_mode() == LIVE
            else future_symbol(months_ahead=1),
        },
        "config": {
            "product": settings.product,
            "order_type": settings.order_type,
            "default_lots": settings.default_lots,
            "lot_size": (
                (get_client().instrument_for_symbol(manager.current_symbol()) or {}).get(
                    "lot_size", settings.lot_size
                )
                if get_execution_mode() == LIVE
                else settings.lot_size
            ),
            "max_open_lots": settings.max_open_lots,
            "warnings": validate(),
        },
    }


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------
def _enrich_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach mark price and per-position PnL to raw ledger rows."""
    client = get_client()
    prices = get_order_manager().price_map()
    out: list[dict[str, Any]] = []
    for pos in positions:
        row = dict(pos)
        key = int(row["position_key"])
        if row["status"] == "OPEN":
            mark = prices.get(row["symbol"]) or row.get("last_price") or row["entry_price"]
            row["mark_price"] = round(float(mark), 2)
            row["unrealized_pnl"] = round(
                (float(mark) - row["entry_price"]) * row["quantity"] * key, 2
            )
        else:
            row["mark_price"] = row.get("exit_price")
            row["unrealized_pnl"] = 0.0
        row["side"] = "LONG" if key == 1 else "SHORT"
        instrument = client.instrument_for_symbol(row["symbol"])
        lot_size = int((instrument or {}).get("lot_size") or settings.lot_size)
        row["lots"] = round(row["quantity"] / max(1, lot_size), 2)
        out.append(row)
    return out


@app.get("/api/portfolio", tags=["portfolio"])
def api_portfolio() -> dict[str, Any]:
    """Balance, margin utilisation and realised/unrealised PnL."""
    manager = get_order_manager()
    metrics = manager.portfolio()
    mode = metrics["mode"]

    open_positions = metrics["open_positions"]
    client = get_client()
    margin_used = sum(
        abs(p["quantity"])
        / max(
            1,
            int(
                ((client.instrument_for_symbol(p["symbol"]) or {}).get("lot_size"))
                or settings.lot_size
            ),
        )
        for p in open_positions
    )
    margin_used *= settings.lot_margin

    if mode == LIVE:
        cash = get_client().available_cash()
        available = float(cash) if cash is not None else 0.0
    else:
        available = float(metrics["balance"])

    return {
        "mode": mode,
        "currency": "INR",
        "initial_balance": round(metrics["initial_balance"], 2),
        "balance": round(metrics["balance"], 2),
        "equity": round(metrics["equity"], 2),
        "available_margin": round(available, 2),
        "margin_used": round(margin_used, 2),
        "margin_utilisation_pct": round(margin_used / (margin_used + available) * 100.0, 2)
        if (margin_used + available) > 0
        else 0.0,
        "unrealized_pnl": round(metrics["unrealized_pnl"], 2),
        "realized_today": round(metrics["realized_today"], 2),
        "realized_total": round(metrics["realized_total"], 2),
        "total_pnl": round(metrics["realized_total"] + metrics["unrealized_pnl"], 2),
        "total_return_pct": round(metrics["total_return_pct"], 2),
        "max_drawdown_pct": round(metrics["max_drawdown_pct"], 2),
        "total_trades": metrics["total_trades"],
        "win_rate": round(metrics["win_rate"], 2),
        "profit_factor": (
            None if metrics["profit_factor"] == float("inf") else round(metrics["profit_factor"], 2)
        ),
        "avg_win": round(metrics["avg_win"], 2),
        "avg_loss": round(metrics["avg_loss"], 2),
        "open_position_count": len(open_positions),
        "data_source": metrics["data_source"],
        "market": metrics["market"],
    }


# ---------------------------------------------------------------------------
# Positions / orders / equity / trades / signals
# ---------------------------------------------------------------------------
@app.get("/api/positions", tags=["positions"])
def api_positions(
    status: str = Query("all", pattern="^(all|open|closed)$"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Open and/or closed Nifty futures positions, newest first."""
    if status == "open":
        rows = db.get_open_positions()
        rows = sorted(rows, key=lambda r: r["entry_time"], reverse=True)
    else:
        query = "SELECT * FROM positions"
        params: list[Any] = []
        if status == "closed":
            query += " WHERE status = 'CLOSED'"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db._fetch_all(query, params, None)

    positions = _enrich_positions(rows)
    return {
        "positions": positions,
        "count": len(positions),
        "open_count": sum(1 for p in positions if p["status"] == "OPEN"),
    }


@app.get("/api/orders", tags=["orders"])
def api_orders(
    limit: int = Query(500, ge=1, le=5000),
    search: Optional[str] = Query(None, description="Case-insensitive substring filter"),
    mode: Optional[str] = Query(None, pattern="^(PAPER|LIVE)$"),
) -> dict[str, Any]:
    """Order book straight from SQLite, newest first."""
    rows = db.get_orders(limit=limit)
    if mode:
        rows = [r for r in rows if (r.get("mode") or "").upper() == mode]
    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if needle in " ".join(str(v) for v in r.values()).lower()]
    return {"orders": rows, "count": len(rows)}


@app.get("/api/equity", tags=["analytics"])
def api_equity(limit: int = Query(2000, ge=2, le=20000)) -> dict[str, Any]:
    """Cumulative equity curve, oldest first, with realised-PnL fallback."""
    curve = db.get_equity_curve(limit=limit)
    if curve:
        points = [
            {
                "ts": r["ts"],
                "equity": round(float(r["equity"]), 2),
                "balance": round(float(r["balance"]), 2),
                "unrealized_pnl": round(float(r["unrealized_pnl"]), 2),
            }
            for r in curve
        ]
        return {"source": "equity_curve", "points": points, "count": len(points)}

    # No marks yet: derive a curve from settled trades so the chart is useful
    # on a fresh install instead of empty.
    trades = list(reversed(db.get_trades(limit=limit)))
    account = db.get_account()
    running = float(account["initial_balance"])
    points = [
        {
            "ts": account["updated_at"],
            "equity": round(running, 2),
            "balance": round(running, 2),
            "unrealized_pnl": 0.0,
        }
    ]
    for trade in trades:
        running += float(trade["net_pnl"])
        points.append(
            {
                "ts": trade["exit_time"],
                "equity": round(running, 2),
                "balance": round(running, 2),
                "unrealized_pnl": 0.0,
            }
        )
    return {"source": "trade_ledger", "points": points, "count": len(points)}


@app.get("/api/trades", tags=["analytics"])
def api_trades(
    limit: int = Query(500, ge=1, le=5000),
    mode: Optional[str] = Query(None, pattern="^(PAPER|LIVE)$"),
) -> dict[str, Any]:
    """Settled round trips from the trade ledger (fees and net PnL included)."""
    rows = db.get_trades(limit=limit)
    if mode:
        rows = [r for r in rows if (r.get("mode") or "").upper() == mode]
    return {"trades": rows, "count": len(rows)}


@app.get("/api/signals", tags=["orders"])
def api_signals(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Signal audit trail: every engine decision, accepted or blocked."""
    rows = db.get_signals(limit=limit)
    return {"signals": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
@app.post("/api/toggle-mode", tags=["control"])
def api_toggle_mode(request: ModeRequest) -> dict[str, Any]:
    """Switch execution mode. Arming LIVE requires ``confirm=true``."""
    target = request.mode.strip().upper()
    if target not in (PAPER, LIVE):
        raise HTTPException(status_code=400, detail=f"mode must be one of {[PAPER, LIVE]}")
    if target == LIVE and not request.confirm:
        raise HTTPException(
            status_code=400,
            detail="Switching to LIVE requires confirm=true - real orders will be placed.",
        )
    if target == LIVE and not settings.is_live_configured:
        raise HTTPException(
            status_code=409,
            detail="LIVE mode requires KITE_API_KEY, KITE_API_SECRET and KITE_ACCESS_TOKEN.",
        )

    previous = get_execution_mode()
    set_execution_mode(target)
    get_client().mode = target
    log.warning("Execution mode %s -> %s", previous, target)
    return {"ok": True, "previous": previous, "mode": target}


@app.post("/api/pause", tags=["control"])
def api_pause(request: PauseRequest) -> dict[str, Any]:
    """Block or unblock new entries. Exits stay available while paused."""
    manager = get_order_manager()
    if request.paused:
        manager.pause(request.reason or "MANUAL")
    else:
        manager.resume()
    return {"ok": True, "paused": manager.paused, "reason": request.reason}


@app.post("/api/panic", tags=["control"])
def api_panic(request: PanicRequest) -> dict[str, Any]:
    """Square off every open position, then pause the strategy."""
    if not request.confirm:
        raise HTTPException(status_code=400, detail="Panic requires confirm=true")

    manager = get_order_manager()
    result = manager.square_off(request.reason or "PANIC", source="API_PANIC")
    manager.pause("PANIC square-off")
    payload = result.to_dict()
    payload["paused"] = manager.paused
    log.warning("PANIC via API: ok=%s %s", result.ok, result.reason)
    return payload


# ---------------------------------------------------------------------------
# Kite login
# ---------------------------------------------------------------------------
class TokenRequest(BaseModel):
    request_token: str = Field(..., description="request_token from the Kite redirect")


@app.get("/api/kite/status", tags=["kite"])
def api_kite_status() -> dict[str, Any]:
    """Token health plus the URL to start a fresh login."""
    status = kite_auth.token_status()
    status["redirect_url"] = kite_auth.callback_url()
    status["note"] = kite_auth.token_age_note()
    status["api_key_present"] = bool(settings.api_key and settings.api_secret)
    status["feed"] = get_feed().status()
    return status


def _start_gift_feed(*, reconnect: bool = False) -> dict[str, Any]:
    feed = reset_feed() if reconnect else get_feed()
    if feed.bar_count() == 0:
        feed.load_persisted()
    started = feed.start()
    status = feed.status()
    status["start_requested"] = started
    return status


@app.post("/api/kite/login-url", tags=["kite"])
def api_kite_login_url() -> dict[str, Any]:
    """Return the URL the user opens to authenticate with Zerodha."""
    try:
        url = kite_auth.login_url()
    except kite_auth.KiteAuthError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "ok": True,
        "login_url": url,
        "redirect_url": kite_auth.callback_url(),
        "note": "Open the login URL, authenticate, and Kite redirects to the redirect URL.",
    }


@app.post("/api/kite/session", tags=["kite"])
def api_kite_session(request: TokenRequest) -> dict[str, Any]:
    """Exchange a request_token for an access token and start using it."""
    try:
        result = kite_auth.exchange_request_token(request.request_token)
    except kite_auth.KiteAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    feed = _start_gift_feed()

    return {
        "ok": True,
        "user_id": result.get("user_id"),
        "user_name": result.get("user_name"),
        "persisted": result.get("persisted"),
        "env_path": result.get("env_path"),
        "feed_started": feed["start_requested"],
        "feed": feed,
    }


@app.post("/api/kite/feed/reconnect", tags=["kite"])
def api_kite_feed_reconnect() -> dict[str, Any]:
    """Reconnect the GIFT Nifty websocket using the current valid access token."""
    client = get_client()
    profile = client.profile()
    if not profile.get("ok"):
        raise HTTPException(
            status_code=409,
            detail=profile.get("error") or "Kite access token is not valid; sign in again.",
        )
    feed = _start_gift_feed(reconnect=True)
    return {"ok": feed["start_requested"], "feed": feed}


def _callback_page(ok: bool, title: str, detail: str, extra: str = "") -> HTMLResponse:
    """Human-readable result for the browser that just came back from Zerodha."""
    colour = "#10b981" if ok else "#f43f5e"
    icon = "✅" if ok else "⚠️"
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#080b14; color:#e2e8f0;
         font:15px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif; }}
  .card {{ max-width:32rem; margin:2rem; padding:2rem; border:1px solid #1e293b;
           border-radius:1rem; background:#0d1220; }}
  h1 {{ margin:0 0 .5rem; font-size:1.15rem; color:{colour}; }}
  p {{ margin:.35rem 0; color:#94a3b8; }}
  code {{ background:#121a2c; padding:.15rem .4rem; border-radius:.35rem;
          font-family:ui-monospace,Menlo,monospace; font-size:.85em; }}
  a {{ display:inline-block; margin-top:1.25rem; padding:.6rem 1rem;
       border-radius:.6rem; background:linear-gradient(135deg,#38bdf8,#818cf8);
       color:#0d1220; font-weight:600; text-decoration:none; }}
  .extra {{ margin-top:1rem; font-size:.85rem; color:#7c8aa5; }}
</style></head><body><div class="card">
  <h1>{icon} {title}</h1>
  <p>{detail}</p>
  {f'<p class="extra">{extra}</p>' if extra else ''}
  <a href="/">Open the dashboard</a>
</div></body></html>"""
    )


@app.get(kite_auth.LEGACY_CALLBACK_PATH, include_in_schema=False)
@app.get(kite_auth.CALLBACK_PATH, include_in_schema=False)
def kite_callback(
    request_token: Optional[str] = None, status: Optional[str] = None
) -> Any:
    """Kite redirects here after login.

    Register ``<host>/zerodha/callback`` as the Redirect URL in the Kite
    developer console and the request_token is exchanged automatically.
    """
    if not request_token:
        return _callback_page(
            False,
            "Kite login did not complete",
            "Zerodha did not return a <code>request_token</code>"
            + (f" (status={status})" if status else "")
            + ".",
            "Usually this means the app's Redirect URL does not match "
            f"<code>{kite_auth.callback_url()}</code> exactly. Check it in the "
            "Kite developer console, or paste the request_token manually on the "
            "dashboard.",
        )

    try:
        result = kite_auth.exchange_request_token(request_token)
    except kite_auth.KiteAuthError as exc:
        return _callback_page(
            False,
            "Token exchange failed",
            f"<code>{exc}</code>",
            "request_tokens are single-use and short-lived. Start the login "
            "again from the dashboard.",
        )

    feed = _start_gift_feed()

    persisted = result.get("persisted")
    return _callback_page(
        True,
        f"Connected as {result.get('user_id') or 'Kite user'}",
        (
            "Kite login succeeded and GIFT Nifty ticks are arriving."
            if feed["connected"] and feed["last_tick_at"]
            else "Kite login succeeded; the GIFT Nifty feed is still connecting. Check its status on the dashboard."
        ),
        (
            "Token saved to bot/.env — it survives a restart."
            if persisted
            else "Token is active for this process only (bot/.env was not writable)."
        )
        + (" Check the dashboard for the GIFT feed connection status."),
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index() -> Any:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        return JSONResponse(
            {"detail": f"Dashboard not found at {page}", "api": "/docs"}, status_code=404
        )
    return FileResponse(page, media_type="text/html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Any:
    icon = WEB_DIR / "favicon.svg"
    if icon.is_file():
        return FileResponse(icon, media_type="image/svg+xml")
    return JSONResponse({}, status_code=204)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
def run(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Start the bot (blocking)."""
    import uvicorn

    uvicorn.run(
        app,
        host=host or settings.webhook_host,
        port=int(port or settings.webhook_port),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        # The dashboard polls ~5 req/s; access logs would bury the bot's own
        # signal/order lines. Set UVICORN_ACCESS_LOG=1 when debugging.
        access_log=os.getenv("UVICORN_ACCESS_LOG", "0").lower() in {"1", "true", "yes"},
    )


if __name__ == "__main__":
    run()
