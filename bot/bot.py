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

import fnmatch
import html
import hashlib
import json
import logging
import os
import re
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

import database as db
import dashboard_auth
import kite_auth
import risk
from config import (
    LIVE,
    PAPER,
    get_execution_mode,
    set_execution_mode,
    settings,
    strategy_summary,
    validate,
)
from engine import get_engine
from gift_nifty import GIFT_KEY, get_feed, reset_feed
from gift_book import get_gift_book, gift_books
from kite_client import expiry_label, future_symbol, get_client, market_status
from market_calendar import get_calendar
from order_manager import get_order_manager
from paper_mirror import get_mirror
from watchdog import get_watchdog
import notifier

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
LOG_FILE = Path(os.getenv("LOG_FILE", BASE_DIR / "logs" / "bot.log")).expanduser()
log = logging.getLogger("bot")

STARTED_AT = time.time()
STARTED_ISO = db.iso()


def _configure_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    # Stamp in IST, not the container clock. The container runs in UTC while
    # every other timestamp the dashboard shows is IST, so a UTC log line reads
    # as five and a half hours stale next to the trade it explains.
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    formatter.converter = lambda epoch: datetime.fromtimestamp(epoch, db.IST).timetuple()

    # The Kite SDK logs the Authorization header (api_key:access_token) at DEBUG,
    # which would put a credential into LOG_FILE — where /api/logs would then
    # serve it. Cap the SDK regardless of the operator's LOG_LEVEL so a
    # debugging session cannot leak the token.
    for noisy in ("kiteconnect", "urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not root.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    log_path = LOG_FILE.resolve()
    if not any(
        isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == log_path
        for handler in root.handlers
    ):
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Log tail (served to the dashboard's Logs tab)
# ---------------------------------------------------------------------------
# Matches the formatter's "%(asctime)s %(levelname)s %(name)s: %(message)s".
_LOG_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T][\d:,.]+\s+(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s"
)
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LOG_LEVEL_RANK = {name: rank for rank, name in enumerate(LOG_LEVELS)}

# Only the tail of the active file is read, so a 5 MB log costs one seek.
_LOG_TAIL_BYTES = 512 * 1024

# Credentials are written to .env, never logged, but a future log line or a
# caught exception could echo one. The dashboard is unauthenticated, so mask
# anything that looks like a secret before it leaves the process.
#
# Two layers, because a key/value pattern alone is not enough: the Kite SDK
# logs `Authorization: token <api_key>:<access_token>` at DEBUG, where the
# secret is the *second* half of a colon pair with no label of its own.
# `Authorization: token <api_key>:<access_token>` — the secret is the *second*
# half of a colon pair with no label of its own, so the whole header value is
# masked. This must run first: a key/value pattern would consume the literal
# word "token" and leave the credentials behind.
_AUTH_HEADER_RE = re.compile(r"(?P<key>\bauthorization\b\s*[:=]\s*).*$", re.IGNORECASE)
_SECRET_KEY_RE = re.compile(
    r"(?P<key>"
    r"kite_api_secret|kite_access_token|kite_api_key|api_secret|access_token"
    r"|request_token|webhook_secret|password|api_key"
    r")(?P<sep>['\"]?\s*[=:]\s*['\"]?|\s+)(?P<value>[^\s,;'\"]+)",
    re.IGNORECASE,
)
# `set_access_token(<token>)`, `TokenException: Invalid token: <token>`.
_SECRET_BARE_RE = re.compile(
    r"(?P<setter>\bset_access_token\s*\(\s*['\"]?)(?P<value>[\w.:-]+)"
    r"|(?P<invalid>\bInvalid\s+token\s*:\s*)(?P<value2>[\w.:-]+)",
    re.IGNORECASE,
)


def _redact_secrets(line: str) -> str:
    line = _AUTH_HEADER_RE.sub(lambda m: f"{m.group('key')}***", line)
    line = _SECRET_KEY_RE.sub(lambda m: f"{m.group('key')}{m.group('sep')}***", line)
    return _SECRET_BARE_RE.sub(
        lambda m: f"{m.group('setter') or m.group('invalid')}***",
        line,
    )


def _read_tail(path: Path, max_bytes: int = _LOG_TAIL_BYTES) -> list[str]:
    """Last lines of ``path``, reading at most ``max_bytes`` from the end."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # drop the partial line the seek landed inside
        data = handle.read()
    return data.decode("utf-8", errors="replace").splitlines()


def read_log_tail(
    limit: int = 400,
    level: Optional[str] = None,
    search: Optional[str] = None,
) -> dict[str, Any]:
    """Tail the bot's log file, oldest line first, with optional filters.

    ``level`` is a *minimum* severity: ``WARNING`` also returns ERROR and
    CRITICAL, which is what "show me what went wrong" means in practice. A line
    that does not match the log format is treated as a continuation (a
    traceback frame) and inherits the previous line's verdict, so filtering by
    level does not strip the stack trace that explains the error.
    """
    path = LOG_FILE
    payload: dict[str, Any] = {
        "lines": [],
        "count": 0,
        "scanned": 0,
        "path": str(path),
        "available": False,
        "size_bytes": 0,
        "updated_at": None,
        "error": None,
    }
    if not path.is_file():
        return payload

    try:
        lines = _read_tail(path)
        stat = path.stat()
    except OSError as exc:
        log.warning("Cannot read log file %s: %s", path, exc)
        payload["error"] = str(exc)
        return payload

    payload["available"] = True
    payload["scanned"] = len(lines)
    payload["size_bytes"] = stat.st_size
    payload["updated_at"] = db.iso(datetime.fromtimestamp(stat.st_mtime, db.IST))

    threshold = _LOG_LEVEL_RANK.get((level or "").strip().upper())
    needle = (search or "").strip().lower()

    selected: list[str] = []
    keep_continuation = False
    for line in lines:
        # Redact first, then filter. Testing the raw line would let a caller
        # confirm a secret's presence with ?search= even though the returned
        # text is masked — a byte-by-byte oracle over anything that reaches the
        # log. Masking before the substring test closes it.
        line = _redact_secrets(line)
        match = _LOG_LINE_RE.match(line)
        if match:
            keep_continuation = threshold is None or _LOG_LEVEL_RANK[match["level"]] >= threshold
            if not keep_continuation:
                continue
        elif not keep_continuation:
            # An unparsed line with no severe entry above it is not a traceback
            # we care about; drop it rather than showing orphaned frames.
            continue
        if needle and needle not in line.lower():
            continue
        selected.append(line)

    payload["lines"] = selected[-limit:] if limit else selected
    payload["count"] = len(payload["lines"])
    return payload


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
    """Start market data with a valid token, logging in headlessly if configured."""
    status = kite_auth.token_status()
    if not status.get("valid") and status.get("state") in {"no_token", "expired", "error"}:
        # A stateless deployment has no token to inherit, so the login has to
        # happen here rather than waiting for a human to open a browser.
        import kite_autologin

        if kite_autologin.configured():
            log.info("Kite token is %s; attempting headless login", status.get("state"))
            if kite_autologin.ensure_token():
                # ensure_token only returns a payload when it logged in, and a
                # successful login installs the token, so the feed can start
                # without another profile round-trip.
                status = {"state": "valid", "valid": True}

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
def token_keeper_loop(stop: threading.Event, interval: Optional[float] = None) -> None:
    """Re-login when the Kite token expires while the process is running.

    The startup path handles a fresh container. This handles the other half: a
    process that runs for weeks and crosses the ~06:00 IST expiry. Without it
    the bot would sit on cached candles until someone noticed.

    Checks are cheap (one profile call) and only run when headless login is
    configured, so a deployment that logs in by hand is unaffected.
    """
    import kite_autologin

    if not kite_autologin.configured():
        log.info("Token keeper idle: headless login is not configured")
        return

    period = float(interval if interval is not None else settings.token_check_seconds)
    log.info("Token keeper started (every %.0fs)", period)
    while not stop.wait(period):
        try:
            if kite_autologin.ensure_token():
                log.info("Kite token refreshed by the token keeper")
                _start_gift_feed(reconnect=True)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Token keeper check failed: %s", exc)


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
            # Keep the execution future's price fresh for the dashboard. Priced
            # here, at the mark cadence, rather than in the status endpoint,
            # which the dashboard polls every 1.5s - Kite's quote API is rate
            # limited and that would be ~16k calls a session.
            get_feed().refresh_exec_quote()
            # Mark both books from the one price map: the shadow book must value a
            # position exactly as the live book does, or the comparison measures
            # the pricing difference instead of the model's error.
            prices = manager.price_map()
            if prices:
                db.mark_open_positions(get_execution_mode(), prices)
                try:
                    from paper_mirror import get_mirror

                    get_mirror().mark(prices)
                except Exception:  # pragma: no cover - defensive
                    log.exception("Shadow book mark failed")
            # The index book is marked on its own level: there is no basis, which
            # is exactly what makes the comparison interesting.
            try:
                from gift_book import gift_books

                feed = get_feed()
                feed_status = feed.status()
                quote = feed_status.get("last_candle_close") or feed_status.get("price")
                # Every strategy's index book, not just the default one. One mark
                # per book: a single-book mark left the other books' open legs
                # valued at their entry, so their unrealised P&L read zero while
                # the index moved.
                for book in gift_books():
                    book.mark(quote)
            except Exception:  # pragma: no cover - defensive
                log.exception("GIFT index book mark failed")
        except Exception as exc:  # pragma: no cover - network
            log.warning("Execution quote refresh failed: %s", exc)
        try:
            positions = db.get_open_positions(mode=get_execution_mode())
            if positions:
                if get_execution_mode() == PAPER:
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
    # Constrained to a single safe line: `reason` is logged verbatim, so a
    # newline would let a caller forge a second, convincing log record in the
    # operator's audit trail.
    reason: str = Field(
        default="MANUAL",
        max_length=200,
        pattern=r"^[\w \-.:/@()\[\]]*$",
        description="Audit label",
    )


class BookPauseRequest(BaseModel):
    """Pause or resume one book's entries."""

    paused: bool = Field(..., description="true blocks this book's new BUY/SELL entries")
    strategy_id: str = Field(
        ..., min_length=1, max_length=64,
        description="The book to pause; its exits stay available either way",
    )
    reason: str = Field(
        default="MANUAL",
        max_length=200,
        pattern=r"^[\w \-.:/@()\[\]]*$",
        description="Audit label",
    )


class PanicRequest(BaseModel):
    confirm: bool = Field(
        default=False,
        description="Must be true; the client shows a confirm dialog first.",
    )
    reason: str = Field(
        default="PANIC",
        max_length=200,
        pattern=r"^[\w \-.:/@()\[\]]*$",
        description="Audit label",
    )


class JournalRequest(BaseModel):
    """Discretionary notes attached to a settled trade."""

    note: str = Field(default="", max_length=4000, description="What happened, in your words")
    setup: str = Field(default="", max_length=200, description="Named setup or pattern")
    tags: str = Field(default="", max_length=400, description="Comma-separated labels")
    rating: Optional[int] = Field(default=None, description="Execution quality 1-5")
    mistakes: str = Field(default="", max_length=1000, description="What to avoid next time")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    db.init_db()
    with STATUS_LOCK:
        STATUS.update(running=True, started_at=db.iso(), heartbeat_ts=time.time())

    # 1. GIFT Nifty tick feed -> candles (warmup carried over from SQLite).
    feed = get_feed()
    restored = feed.load_persisted()
    _connect_kite_on_startup()
    # Kite serves GIFT Nifty history, so warm the indicators from it instead of
    # waiting hours for tick-built candles.
    try:
        restored = feed.refresh_history(force=True) or restored
    except Exception as exc:  # pragma: no cover - network
        log.warning("Initial GIFT Nifty history load failed: %s", exc)

    # 2. Strategy engine: evaluates GIFT Nifty bars, trades the NFO future.
    engine = get_engine()
    engine.start()

    # 3. Mark-to-market loop: keeps equity/PnL live between signals.
    marks = threading.Thread(target=mark_loop, args=(_stop,), name="marks", daemon=True)
    marks.start()

    # 4. Watchdog: alerts when the process is up but the trading is not.
    watchdog = get_watchdog()
    watchdog.start(settings.watchdog_interval_seconds)

    # 5. Token keeper: re-logs in when the daily token expires mid-session.
    #    Startup covers a fresh container; this covers a process that outlives
    #    the token, which is the normal case for a bot that runs for weeks.
    token_keeper = threading.Thread(
        target=token_keeper_loop, args=(_stop,), name="token-keeper", daemon=True
    )
    token_keeper.start()

    log.info(
        "Bot ready - mode=%s db=%s signals=%s (%ss bars, %d restored) "
        "execution=%s dashboard=http://%s:%s/",
        get_execution_mode(), settings.db_path, GIFT_KEY,
        feed.seconds, restored, future_symbol(),
        settings.webhook_host, settings.webhook_port,
    )
    # A start message makes a silent crash distinguishable from a quiet market:
    # if the bot restarts overnight, the operator sees it in the morning.
    _lifecycle_alert("BOT_STARTED", f"Belaku started in {get_execution_mode()} mode", "info")
    try:
        yield
    finally:
        _stop.set()
        with STATUS_LOCK:
            STATUS["running"] = False
        _lifecycle_alert("BOT_STOPPED", "Belaku is shutting down", "warning")
        watchdog.stop()
        engine.stop()
        feed.stop()
        marks.join(timeout=5)
        log.info("Bot stopped")


# ---------------------------------------------------------------------------
# Request security
# ---------------------------------------------------------------------------
# The dashboard has no session auth, so a request's identity comes entirely from
# what the browser asserts: the Host header (which a DNS-rebinding attack
# controls) and the Origin header (a script cannot forge it). Both are checked.
def allowed_hosts() -> list[str]:
    """Host header allow-list, parsed from ``ALLOWED_HOSTS``."""
    hosts = [h.strip() for h in settings.allowed_hosts.split(",") if h.strip()]
    return hosts or ["127.0.0.1", "localhost", "[::1]"]


def allowed_origins() -> list[str]:
    """Extra cross-origin callers, parsed from ``ALLOWED_ORIGINS``."""
    return [o.strip().rstrip("/") for o in settings.allowed_origins.split(",") if o.strip()]


# ``Origin`` is the browser's own statement of page provenance, so unlike Host
# it cannot be forged by script — only omitted (in which case Host covers us).
_ORIGIN_OK_CACHE: dict[str, bool] = {}


def _origin_allowed(origin: str, host: str) -> bool:
    """True when ``origin`` is the same site as ``host``, or explicitly allowed.

    Same-site is compared by *host*, not scheme or port: a page on
    ``http://127.0.0.1:9000`` calling ``http://127.0.0.1:9000`` is same-origin,
    and the port can differ behind a proxy without becoming a different site.
    """
    if not origin:
        return True
    allowed = set(allowed_hosts()) | set(allowed_origins())
    if origin in allowed:
        return True
    cached = _ORIGIN_OK_CACHE.get(origin)
    if cached is not None:
        return cached
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    origin_host = (parsed.hostname or "").lower()
    host_only = (urlsplit(f"//{host}").hostname or host or "").lower()
    verdict = bool(origin_host) and (
        origin_host == host_only or origin_host in {h.lower() for h in allowed_hosts()}
    )
    _ORIGIN_OK_CACHE[origin] = verdict
    return verdict


# Endpoints that must answer without an Origin check. Health is polled by the
# container healthcheck (no Origin) and reveals nothing worth protecting.
_CSRF_EXEMPT: tuple[str, ...] = ("/api/health",)

# Reachable without a session: the login page and its POST, the health probe the
# container healthcheck uses, and the Zerodha redirect target (Zerodha's browser
# redirect carries no cookie of ours, and the request_token it delivers is
# useless without the API secret).
_AUTH_EXEMPT: tuple[str, ...] = (
    "/api/health",
    "/login",
    "/api/auth/login",
    "/api/auth/status",
    "/zerodha/callback",
    "/kite/callback",
    "/favicon.ico",
)

# Static assets are served to the login page itself, so they cannot require a
# session. They contain no data - only the dashboard's own CSS/JS.
_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = ("/static/",)


def _auth_exempt(path: str) -> bool:
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return True
    return any(fnmatch.fnmatch(path, p) for p in _AUTH_EXEMPT)


def _session_valid(request: Request) -> bool:
    return dashboard_auth.verify_token(request.cookies.get(dashboard_auth.COOKIE_NAME, ""))


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"

app = FastAPI(
    title="GIFT Nifty Execution Bot",
    version="3.0.0",
    description=(
        "Single-process bot: GIFT Nifty tick feed, st.pine strategy engine, "
        "Nifty futures execution, JSON API and dashboard."
    ),
    lifespan=lifespan,
    # The schema enumerates every control endpoint, including the one that
    # squares off live positions. Nothing needs it while running, so it is off
    # unless explicitly re-enabled (useful when developing against the API).
    docs_url="/docs" if os.getenv("ENABLE_API_DOCS", "0") in {"1", "true", "yes"} else None,
    redoc_url=None,
    openapi_url="/openapi.json"
    if os.getenv("ENABLE_API_DOCS", "0") in {"1", "true", "yes"}
    else None,
)


@app.middleware("http")
async def security_middleware(request: Request, call_next: Any) -> Any:
    """Reject cross-site requests and add defensive response headers.

    Three separate controls, because each covers a hole the others do not:

    * **TrustedHost** (registered separately) blocks DNS-rebinding, where the
      browser treats ``http://evil.com`` as same-origin because it resolves to
      the bot.
    * **Origin check** blocks a request made *by* a page on another site. It
      applies to state-changing methods only, so a static dashboard load is
      never rejected.
    * **Response headers** stop the dashboard being framed and constrain what
      an injected string could load.

    A request with no ``Origin`` at all (curl, the container healthcheck) is
    allowed: a browser always sends one on a cross-site POST, so its absence
    means the caller is not a browser acting for another page.
    """
    origin = request.headers.get("origin") or ""
    host = request.headers.get("host") or ""
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and not any(fnmatch.fnmatch(request.url.path, p) for p in _CSRF_EXEMPT)
        and not _origin_allowed(origin, host)
    ):
        log.warning("Blocked cross-site %s %s from origin=%r", request.method, request.url.path, origin)
        return JSONResponse(
            {"detail": "Cross-site request blocked"}, status_code=403
        )

    # Authentication. Runs after the Origin check so a cross-site request is
    # rejected as cross-site rather than as unauthenticated, and before routing
    # so a new endpoint is protected by default rather than by remembering to
    # add a dependency to it.
    if dashboard_auth.auth_required() and not _auth_exempt(request.url.path):
        if not _session_valid(request):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "Authentication required"}, status_code=401)
            # A browser navigating to the dashboard gets the login page instead
            # of a bare 401, so the operator is never stuck.
            return RedirectResponse("/login", status_code=303)

    response = await call_next(request)
    # No inline script/style is used by the dashboard (styles.css and app.js are
    # separate files, and Tailwind is configured through its own config object),
    # so a strict CSP costs nothing. The CDN is still needed for Tailwind itself.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# Registered after the decorator so it wraps the app *outside* security_middleware
# (Starlette applies the last-added middleware first), rejecting a spoofed Host
# before any other middleware or route sees the request.
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
@app.get("/api/health", tags=["status"])
def health() -> dict[str, Any]:
    return {"ok": True, "time": db.iso(), "uptime_seconds": round(time.time() - STARTED_AT, 1)}


@app.get("/api/logs", tags=["status"])
def api_logs(
    limit: int = Query(400, ge=1, le=5000),
    level: Optional[str] = Query(None, description="Minimum severity: DEBUG..CRITICAL"),
    search: Optional[str] = Query(None, description="Case-insensitive substring filter"),
) -> dict[str, Any]:
    """Tail of the bot's log file for the dashboard's Logs tab."""
    minimum = (level or "").strip().upper() or None
    if minimum and minimum not in _LOG_LEVEL_RANK:
        raise HTTPException(
            status_code=400, detail=f"level must be one of {', '.join(LOG_LEVELS)}"
        )
    payload = read_log_tail(limit=limit, level=minimum, search=search)
    payload["level"] = minimum
    payload["search"] = search or ""
    payload["levels"] = list(LOG_LEVELS)
    payload["max_bytes"] = _LOG_TAIL_BYTES
    return payload


@app.get("/api/mirror", tags=["analytics"])
def api_mirror() -> dict[str, Any]:
    """Shadow paper book: sync state and the live-vs-paper comparison.

    The mirror never places an order, so this endpoint is read-only and reports
    a disabled mirror rather than failing when ``PAPER_MIRROR`` is off.
    """
    return _mirror_status()


def _mirror_status() -> dict[str, Any]:
    """Mirror status that can never break ``/api/status``."""
    try:
        return get_mirror().status()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Mirror status unavailable: %s", exc)
        return {
            "enabled": False,
            "configured": False,
            "reason": f"status unavailable: {exc}",
        }


def _gift_book_status() -> dict[str, Any]:
    """Index-book status that can never break ``/api/status``.

    Every strategy's book, keyed by strategy id, plus an aggregate. One book per
    strategy means a single status object would report one book and hide the
    rest; ``books`` is the per-strategy view and the top-level numbers are the
    sum across them.
    """
    try:
        from gift_book import gift_books

        books = {}
        for book in gift_books():
            try:
                books[book.strategy_id] = book.status()
            except Exception as exc:  # pragma: no cover - defensive
                books[book.strategy_id] = {"enabled": False, "reason": str(exc)}
        if not books:
            return {"enabled": False, "configured": False, "books": {}}
        enabled = {k: v for k, v in books.items() if v.get("enabled")}
        if not enabled:
            return {"enabled": False, "configured": False, "books": books}
        return {
            "enabled": True,
            "configured": True,
            "books": books,
            "symbol": next(iter(enabled.values())).get("symbol"),
            "net_pnl": round(sum(float(v.get("net_pnl") or 0.0) for v in enabled.values()), 2),
            "unrealized_pnl": round(
                sum(float(v.get("unrealized_pnl") or 0.0) for v in enabled.values()), 2
            ),
            "equity": round(sum(float(v.get("equity") or 0.0) for v in enabled.values()), 2),
            "trades": sum(int(v.get("trades") or 0) for v in enabled.values()),
            "realized_today": round(
                sum(float(v.get("realized_today") or 0.0) for v in enabled.values()), 2
            ),
        }
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("GIFT index book status unavailable: %s", exc)
        return {"enabled": False, "configured": False, "reason": f"status unavailable: {exc}"}


def _lifecycle_alert(kind: str, detail: str, severity: str = "info") -> None:
    """Send a start/stop notice. Never raises, never blocks startup.

    Logged either way. A lifecycle alert that silently does nothing is
    indistinguishable from one that was never configured, and the whole point of
    the message is to make a restart visible.
    """
    if not settings.alert_on_lifecycle:
        log.info("Lifecycle alert %s skipped: ALERT_ON_LIFECYCLE is off", kind)
        return
    try:
        import notifier

        if not notifier.configured():
            log.info(
                "Lifecycle alert %s skipped: ALERT_CHANNEL=%s is not configured",
                kind, settings.alert_channel,
            )
            return
        delivered = notifier.send(
            {
                "kind": kind,
                "detail": detail,
                "severity": severity,
                "source": "belaku",
                "mode": get_execution_mode(),
                "at_ist": db.iso(),
                "fields": {
                    "venue": get_execution_mode(),
                    "at": db.iso(),
                },
            }
        )
        log.info("Lifecycle alert %s delivered=%s", kind, delivered)
    except Exception:  # pragma: no cover - defensive
        log.exception("Lifecycle alert failed")


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
        "bot_name": settings.bot_name,
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
            # Age rather than a timestamp: the watchdog compares it against a
            # threshold, and doing that here keeps the clock in one place.
            "quote_age_seconds": (
                round((datetime.now(db.IST) - client.last_quote_at).total_seconds(), 1)
                if client.last_quote_at
                else None
            ),
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
        "watchdog": get_watchdog().state.to_dict(),
        "alerts": {"alerting": notifier.status()},
        "mirror": _mirror_status(),
        "gift_book": _gift_book_status(),
        "strategy_config": strategy_summary(),
        "market": market,
        "symbol": {
            "current": manager.current_symbol(),
            "next": (
                get_client().front_month_symbol(months_ahead=1)
            )
            if get_execution_mode() == LIVE
            else future_symbol(months_ahead=1),
            # Derived from the symbol, so this costs no broker round-trip on a
            # poll that runs every 1.5s.
            "expiry": expiry_label(manager.current_symbol()),
            "next_expiry": expiry_label(
                get_client().front_month_symbol(months_ahead=1)
                if get_execution_mode() == LIVE
                else future_symbol(months_ahead=1)
            ),
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
def _trade_duration_seconds(row: dict[str, Any]) -> Optional[float]:
    """Seconds a position has been open, or was open for a closed one.

    Read from the ledger timestamps rather than a process clock, so a restart
    does not reset the age of a live trade and a closed row keeps its own.
    """
    entry = db._parse_ts(row.get("entry_time"))
    if entry is None:
        return None
    end = db._parse_ts(row.get("exit_time")) if row.get("status") == "CLOSED" else None
    if end is None:
        end = db.now_ist()
    return max(0.0, (end - entry).total_seconds())


def _format_duration(seconds: Optional[float]) -> Optional[str]:
    """``2h 05m`` / ``14m`` / ``38s`` - compact enough for a table cell."""
    if seconds is None:
        return None
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def _exit_reasons(position_ids: list[int]) -> dict[int, str]:
    """Map position id to the reason its trade row recorded.

    A position row has no exit reason of its own - the trade row does - so a
    closed position has to be joined to it to say why it went. Read in one query
    rather than per row, and tolerant of a missing trade row (a partial close
    leaves the position open, so there may be none).
    """
    if not position_ids:
        return {}
    placeholders = ",".join("?" for _ in position_ids)
    rows = db._fetch_all(
        f"SELECT position_id, exit_reason FROM trades "
        f"WHERE position_id IN ({placeholders}) ORDER BY id",
        list(position_ids),
        None,
    )
    # Last write wins: a position that was scaled out has several trade rows, and
    # the final one is the exit that closed it.
    return {
        int(row["position_id"]): str(row["exit_reason"])
        for row in rows
        if row.get("exit_reason")
    }


def _enrich_positions(
    positions: list[dict[str, Any]], prices: Optional[dict[str, float]] = None
) -> list[dict[str, Any]]:
    """Attach mark price and per-position PnL to raw ledger rows.

    ``prices`` defaults to the futures book's map. The GIFT index book passes its
    own, because the two books hold different instruments: pricing the index off
    the future would fold the basis into the index book's PnL and make the two
    books look like one position.
    """
    client = get_client()
    if prices is None:
        prices = get_order_manager().price_map()
    # The exit reason lives on the trade row, not the position, so a closed
    # position has to be joined to it to say *why* it went.
    exit_reasons = _exit_reasons([row["id"] for row in positions if row["status"] == "CLOSED"])
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
        # The dashboard labels a trade by the order that opened it, which is what
        # the operator actually pressed: a long is a BUY, a short a SELL.
        row["direction"] = "BUY" if key == 1 else "SELL"
        # How long the trade has been (or was) on. Derived from the ledger
        # timestamps so it survives a restart and reads the same for a closed row.
        row["duration_seconds"] = _trade_duration_seconds(row)
        row["duration"] = _format_duration(row["duration_seconds"])
        # There is no stop-loss on a position any more: the strategy's own signal
        # is the exit, so a row has no level to report. The keys are still
        # published as None because the dashboard reads them unconditionally, and
        # a missing key would render as "undefined" rather than as "no stop".
        row["stop_price"] = None
        row["stop_locked_points"] = None
        row["stop_distance_points"] = None
        row["stop_trailing"] = False
        row["trailing"] = None
        row["initial_stop_price"] = None
        row["initial_stop_distance"] = None
        row["trailing_increment"] = None
        row["trailing_distance"] = None
        row["trailing_tightened"] = False
        row["best_price"] = row.get("best_price")
        row["exit_status"] = (
            "OPEN" if row["status"] == "OPEN"
            else (exit_reasons.get(row["id"]) or "CLOSED")
        )
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


def _live_readiness() -> dict[str, Any]:
    """Everything that must be true before LIVE can be armed, and what is not.

    Surfaced rather than enforced by convention: a silent LIVE block is worse
    than an explained one, because the trader cannot tell "no signal" from
    "the bot refused to trade".
    """
    client = get_client()
    manager = get_order_manager()
    session = client.session_status()
    calendar = get_calendar()
    checks: list[dict[str, Any]] = []

    def add(key: str, ok: bool, detail: str, blocking: bool = True) -> None:
        checks.append({"key": key, "ok": ok, "detail": detail, "blocking": blocking})

    add(
        "kite_session",
        bool(session.get("valid")),
        "Kite session valid" if session.get("valid")
        else f"Kite session invalid: {session.get('error') or 're-login required'}",
    )
    add(
        "credentials",
        settings.is_live_configured,
        "API key, secret and access token present" if settings.is_live_configured
        else "Set KITE_API_KEY, KITE_API_SECRET and KITE_ACCESS_TOKEN",
    )
    add(
        "holiday_calendar",
        calendar.available,
        f"NSE calendar loaded ({calendar.status()['count']} dates)"
        if calendar.available else "No NSE holiday calendar; session cannot be verified",
    )
    add(
        "not_paused",
        not manager.paused,
        "Strategy running" if not manager.paused
        else f"Strategy paused ({manager.paused_reason or 'PAUSED'})",
    )
    # The strategy itself, not just the process. The rules are identical in both
    # modes - strategy.py and risk.py never read the mode - so what can differ is
    # whether the books are actually evaluating. A LIVE arm with a warming-up or
    # erroring strategy would place no orders and look like a quiet market.
    #
    # ``status()["strategies"]``, not ``["runs"]``: that is the key Engine.status
    # actually publishes. This read ``runs`` and so always got an empty list,
    # which skipped both checks below and reported LIVE ready with nothing
    # verified - the one outcome this block exists to prevent.
    try:
        runs = (get_engine().status() or {}).get("strategies") or []
    except Exception as exc:  # pragma: no cover - defensive
        runs = []
        add("strategy_ready", False, f"Engine status unavailable: {exc}")
    # Absent is not the same as fine. An empty or missing list means the engine
    # could not name a single book, so it cannot be evidence that one is trading.
    if not runs:
        add("strategy_ready", False, "Engine reported no strategy books to check")
    if runs:
        ready_runs = [r for r in runs if (r.get("state") or {}).get("ready")]
        add(
            "strategy_ready",
            bool(ready_runs),
            (
                f"{len(ready_runs)} of {len(runs)} book(s) evaluating: "
                + ", ".join(str(r.get("id")) for r in ready_runs)
                if ready_runs
                else "No strategy book is ready; every run is warming up or erroring"
            ),
        )
        errored = [r for r in runs if (r.get("state") or {}).get("last_error")]
        if errored:
            add(
                "strategy_errors",
                False,
                "; ".join(
                    f"{r.get('id')}: {(r.get('state') or {}).get('last_error')}"
                    for r in errored
                ),
            )
    # Arming LIVE from a screen that is not updating is the failure this report
    # cannot see any other way: every check above is about configuration and
    # reconciliation, and all of them pass just as happily against a bot whose
    # mark loop has died. The loop is what marks positions to market and samples
    # equity, so a dead loop leaves a frozen P&L that reads exactly like a quiet
    # market - and a trader would arm LIVE on the strength of a stale screen.
    #
    # The thresholds are the watchdog's own (``STALE_TICK_SECONDS`` and
    # ``STALE_LOOP_INTERVALS``) rather than new numbers, so the gate and the alert
    # cannot disagree about what "stale" means. Only enforced in-session: out of
    # hours the loop still runs but nothing moves, and LIVE is refused then anyway.
    try:
        from kite_client import market_status
        from watchdog import STALE_LOOP_INTERVALS, STALE_TICK_SECONDS

        if market_status().get("is_open"):
            heartbeat = (api_status().get("strategy") or {}).get("heartbeat") or {}
            loop_age = heartbeat.get("age_seconds")
            loop_limit = float(heartbeat.get("interval_seconds") or 5.0) * STALE_LOOP_INTERVALS
            add(
                "loop_alive",
                loop_age is not None and loop_age <= loop_limit,
                (
                    f"Mark loop last ran {loop_age:.0f}s ago"
                    if loop_age is not None
                    else "Mark loop has never run; positions cannot be marked"
                ),
            )
            quote_age = (api_status().get("kite") or {}).get("quote_age_seconds")
            add(
                "quote_fresh",
                quote_age is not None and quote_age <= STALE_TICK_SECONDS,
                (
                    f"Last quote {quote_age:.0f}s ago"
                    if quote_age is not None
                    else "No quote has arrived; orders cannot be priced"
                ),
            )
    except Exception as exc:  # pragma: no cover - defensive
        add("loop_alive", False, f"Could not read the mark loop's heartbeat: {exc}")

    # Checked even while the bot is in PAPER. The whole purpose of this report is
    # to gate *arming* LIVE, so skipping the reconciliation until LIVE is already
    # on would leave the one precondition that protects real money unverified at
    # exactly the moment it is needed.
    sync = manager._live_sync_state(mode=LIVE)
    if sync is not None:
        if sync.get("error"):
            add("position_sync", False, sync["error"])
        else:
            add(
                "position_sync",
                bool(sync.get("in_sync")),
                (
                    f"Zerodha {sync['broker']} = ledger {sync['ledger']}"
                    if sync.get("in_sync")
                    else f"Zerodha {sync['broker']} != ledger {sync['ledger']}"
                ),
            )

    # Can the account actually fund one lot? Every check above is about
    # configuration and reconciliation, and all of them pass just as happily on an
    # account that cannot afford a single contract - the bot would arm LIVE, look
    # healthy, and reject every entry with "Insufficient margin". That reads as a
    # strategy that never fires rather than as an unfunded account, which is the
    # one thing this report exists to prevent.
    #
    # The requirement is Kite's own SPAN+exposure figure, not the static
    # LOT_MARGIN estimate: the estimate is a guess and the whole point of the
    # check is to compare against the number the broker will actually demand.
    try:
        symbol = client.front_month_symbol()
        lot_size = int(settings.lot_size)
        # force=True for the same reason as the position sync: this report runs
        # while the bot is still in PAPER, so the client's own mode would
        # short-circuit the read and the check could never see a figure.
        required = client.required_margin(
            symbol, "BUY", lot_size, product=settings.product, force=True
        )
        available = client.available_cash()
        if required is None or available is None:
            # Unknown is not the same as fine, but it is also not evidence of a
            # problem: the margin API can be unavailable while the session is
            # valid. Reported as non-blocking so it is visible without refusing
            # an arm the operator may have funded another way.
            add(
                "margin",
                False,
                "Could not read the margin requirement or available cash from Kite",
                blocking=False,
            )
        else:
            add(
                "margin",
                float(available) >= float(required),
                (
                    f"Rs {float(available):,.0f} available, "
                    f"Rs {float(required):,.0f} needed for 1 lot of {symbol}"
                ),
            )
    except Exception as exc:  # pragma: no cover - network
        add("margin", False, f"Could not check margin: {exc}", blocking=False)

    blocking = [c for c in checks if c["blocking"] and not c["ok"]]
    return {
        "ready": not blocking,
        "checks": checks,
        "blocking": [c["key"] for c in blocking],
    }


@app.get("/api/live/readiness", tags=["control"])
def api_live_readiness() -> dict[str, Any]:
    """Pre-flight report for arming LIVE mode."""
    return _live_readiness()


@app.get("/api/holidays", tags=["analytics"])
def api_holidays(refresh: bool = Query(False)) -> dict[str, Any]:
    """NSE trading holidays, for the P&L calendar and session gating."""
    calendar = get_calendar()
    if refresh:
        calendar.refresh(force=True)
    return {"holidays": calendar.holidays, **calendar.status()}


# ---------------------------------------------------------------------------
# Positions / orders / equity / trades / signals
# ---------------------------------------------------------------------------
@app.get("/api/positions", tags=["positions"])
def api_positions(
    status: str = Query("all", pattern="^(all|open|closed)$"),
    limit: int = Query(200, ge=1, le=2000),
) -> dict[str, Any]:
    """Open and/or closed Nifty futures positions, newest first."""
    mode = get_execution_mode()
    if status == "open":
        rows = db.get_open_positions(mode=mode)
        rows = sorted(rows, key=lambda r: r["entry_time"], reverse=True)
    else:
        query = "SELECT * FROM positions WHERE mode = ?"
        params: list[Any] = [mode]
        if status == "closed":
            query += " AND status = 'CLOSED'"
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
    selected_mode = mode or get_execution_mode()
    rows = [r for r in rows if (r.get("mode") or "").upper() == selected_mode]
    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if needle in " ".join(str(v) for v in r.values()).lower()]
    return {"orders": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# GIFT Nifty index book
#
# Separate endpoints rather than a flag on the futures ones: the two books live in
# different databases, and GIFT Nifty is a non-tradeable index, so its rows can
# never be returned alongside a real contract's.
# ---------------------------------------------------------------------------
def _gift_books_for(strategy_id: Optional[str] = None) -> list[Any]:
    """The index books an endpoint should report on.

    ``strategy_id`` narrows to one book; omitting it reports every configured
    strategy's book. There is one book per strategy, so a single-book default
    would show one strategy's GIFT leg and silently hide the rest - which reads
    as "the other strategies never traded the index".

    A deployment with no strategy configured for the index leg still gets the
    default book, so a pre-multi-strategy ``.env`` returns exactly what it did.
    """
    if strategy_id:
        return [get_gift_book(strategy_id)]
    books = gift_books()
    return books or [get_gift_book()]


def _tag_gift(rows: list[dict[str, Any]], book: Any) -> list[dict[str, Any]]:
    """Stamp each row with the strategy whose book it came from.

    Without it an aggregate row cannot be traced back to a strategy, and with
    two books on one index the operator cannot tell which leg is which.
    """
    return [{**row, "strategy_id": book.strategy_id, "venue": "GIFT"} for row in rows]


@app.get("/api/gift/positions", tags=["gift"])
def api_gift_positions(
    status: str = Query("all", pattern="^(all|open|closed)$"),
    limit: int = Query(200, ge=1, le=2000),
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """GIFT Nifty index-book positions (paper only - the index cannot be traded)."""
    books = _gift_books_for(strategy_id)
    rows: list[dict[str, Any]] = []
    for book in books:
        book_rows = book.positions(status=status, limit=limit)
        # Priced off the index's own mark, never the futures map: the two books
        # hold different instruments and must not be valued as one position.
        mark = book.mark_price()
        rows.extend(_tag_gift(_enrich_positions(book_rows, {book.symbol: mark} if mark else {}), book))
    positions = sorted(rows, key=lambda r: r.get("entry_time") or "", reverse=True)[:limit]
    return {
        "positions": positions,
        "count": len(positions),
        "open_count": sum(1 for p in positions if p["status"] == "OPEN"),
        "enabled": any(book.enabled for book in books),
        "symbol": books[0].symbol if books else None,
        "books": [book.strategy_id for book in books],
        "tradeable": False,
    }


@app.get("/api/gift/trades", tags=["gift"])
def api_gift_trades(
    limit: int = Query(500, ge=1, le=5000),
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """Settled round trips in the GIFT Nifty index book."""
    rows: list[dict[str, Any]] = []
    books = _gift_books_for(strategy_id)
    for book in books:
        rows.extend(_tag_gift(book.trades(limit=limit), book))
    rows = sorted(rows, key=lambda r: r.get("exit_time") or "", reverse=True)[:limit]
    return {
        "trades": rows,
        "count": len(rows),
        "books": [book.strategy_id for book in books],
        "tradeable": False,
    }


@app.get("/api/gift/orders", tags=["gift"])
def api_gift_orders(
    limit: int = Query(500, ge=1, le=5000),
    search: Optional[str] = Query(None, description="Case-insensitive substring filter"),
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """Order log for the GIFT Nifty index book."""
    rows: list[dict[str, Any]] = []
    books = _gift_books_for(strategy_id)
    for book in books:
        rows.extend(_tag_gift(book.orders(limit=limit), book))
    if search:
        needle = search.strip().lower()
        rows = [r for r in rows if needle in " ".join(str(v) for v in r.values()).lower()]
    rows = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)[:limit]
    return {
        "orders": rows,
        "count": len(rows),
        "books": [book.strategy_id for book in books],
        "tradeable": False,
    }


@app.get("/api/gift/signals", tags=["gift"])
def api_gift_signals(
    limit: int = Query(100, ge=1, le=1000),
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """Signal audit trail for the GIFT Nifty index book.

    A separate endpoint for the same reason as the other ``/api/gift/*`` routes:
    the index book keeps its own database, so its decisions cannot be read out of
    the futures ``signals`` table without reporting the wrong book.
    """
    rows: list[dict[str, Any]] = []
    books = _gift_books_for(strategy_id)
    for book in books:
        if not book.enabled:
            continue
        rows.extend(_tag_gift(book.signals(limit=limit), book))
    rows = sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True)[:limit]
    return {
        "signals": rows,
        "count": len(rows),
        "enabled": any(book.enabled for book in books),
        "books": [book.strategy_id for book in books],
        "tradeable": False,
    }


@app.get("/api/gift/summary", tags=["gift"])
def api_gift_summary(
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """Index-book health and metrics, including why it is off when it is."""
    books = _gift_books_for(strategy_id)
    enabled = [book for book in books if book.enabled]
    if not enabled:
        return {
            "enabled": False,
            "configured": bool(settings.gift_book),
            "symbol": books[0].symbol if books else None,
            "books": [book.strategy_id for book in books],
            "tradeable": False,
            "reason": "GIFT_BOOK is off",
        }
    summaries = {book.strategy_id: book.summary() for book in enabled}
    if len(summaries) == 1:
        return {**next(iter(summaries.values())), "configured": True}
    return {
        "enabled": True,
        "configured": True,
        "books": summaries,
        "tradeable": False,
    }


@app.get("/api/gift/analytics", tags=["gift"])
def api_gift_analytics(
    limit: int = Query(5000, ge=1, le=20000),
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """Post-close statistics for the index book.

    A separate endpoint rather than a ``venue`` parameter on ``/api/analytics``:
    the two books are different databases, and a query parameter that silently
    switched ledgers would make a wrong number look like a real one.

    With more than one book the result is *merged* into the same shape a single
    book returns, with the per-strategy figures alongside under ``books``. The
    Performance tab reads ``days`` and ``summary`` off the top level, so leaving
    them undefined for a multi-book deployment would blank the calendar - a
    consolidation that silently removes the page it was meant to enrich.
    """
    books = _gift_books_for(strategy_id)
    enabled = [book for book in books if book.enabled]
    if not enabled:
        return {"enabled": False, "reason": "GIFT_BOOK is off", "tradeable": False}
    per_book = {book.strategy_id: book.analytics(limit=limit) for book in enabled}
    if len(per_book) == 1:
        return {"enabled": True, "tradeable": False, **next(iter(per_book.values()))}
    return {
        "enabled": True,
        "tradeable": False,
        **_merge_analytics(list(per_book.values())),
        "books": per_book,
    }


def _merge_analytics(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold several books' analytics into one report of the same shape.

    Sums where the figure is additive (trades, P&L, points, fees) and recomputes
    where it is not: a win rate is wins over trades across the whole set, and a
    profit factor is total gross win over total gross loss. Averaging the
    per-book ratios would let a book with two trades outweigh one with two
    hundred.

    The shape matches a single book's report because the Performance tab reads
    ``days`` and ``summary`` off the top level - returning only a per-book map
    would blank the calendar, which is the page this consolidation is meant to
    enrich.
    """
    def _s(report: dict[str, Any], key: str) -> float:
        return float((report.get("summary") or {}).get(key) or 0.0)

    def _bucket(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out = {
            key: sum(float(row.get(key) or 0.0) for row in rows)
            for key in ("trades", "wins", "losses", "net_pnl", "gross_pnl", "fees", "points")
        }
        for key in ("trades", "wins", "losses"):
            out[key] = int(out[key])
        out["win_rate"] = (out["wins"] / out["trades"] * 100.0) if out["trades"] else 0.0
        return out

    summary = _bucket([
        # The summary carries no per-row fields _bucket reads beyond these, so
        # passing it straight through is what makes the merged figures add up.
        dict(report.get("summary") or {})
        for report in reports
    ])
    gross_win = sum(_s(r, "gross_win") for r in reports)
    gross_loss = abs(sum(_s(r, "gross_loss") for r in reports))
    trades = summary["trades"]
    summary.update({
        "gross_win": gross_win,
        "gross_loss": -gross_loss,
        "profit_factor": (
            (gross_win / gross_loss) if gross_loss > 0
            else (float("inf") if gross_win > 0 else 0.0)
        ),
        "largest_win": max((_s(r, "largest_win") for r in reports), default=0.0),
        "largest_loss": min((_s(r, "largest_loss") for r in reports), default=0.0),
        "avg_win": (gross_win / summary["wins"]) if summary["wins"] else 0.0,
        "avg_loss": (-gross_loss / summary["losses"]) if summary["losses"] else 0.0,
        "expectancy": (summary["net_pnl"] / trades) if trades else 0.0,
        "avg_points": (summary["points"] / trades) if trades else 0.0,
        "breakeven": trades - summary["wins"] - summary["losses"],
        # The longest single hold across the books: there is no meaningful
        # average of two books' average holds, and the longest is the one that
        # tells an operator how much capital sat in one trade.
        "avg_hold_minutes": max((_s(r, "avg_hold_minutes") for r in reports), default=0.0),
    })

    # Daily buckets keyed by date and summed, not concatenated: two books trading
    # the same day is one day carrying both books' trades.
    days: dict[str, list[dict[str, Any]]] = {}
    for report in reports:
        for day in report.get("days") or []:
            days.setdefault(str(day.get("date")), []).append(day)
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    ordered_days = []
    for date in sorted(days):
        bucket = {"date": date, **_bucket(days[date])}
        running += bucket["net_pnl"]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)
        bucket["cumulative"] = running
        bucket["drawdown"] = running - peak
        ordered_days.append(bucket)

    def _merged(field: str, key_name: str) -> list[dict[str, Any]]:
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for report in reports:
            for row in report.get(field) or []:
                grouped.setdefault(row.get(key_name), []).append(row)
        out = [{key_name: key, **_bucket(rows)} for key, rows in grouped.items()]
        return sorted(out, key=lambda row: str(row.get(key_name) or ""))

    by_direction = {
        side: _bucket([
            (report.get("by_direction") or {}).get(side) or {}
            for report in reports
        ])
        for side in ("LONG", "SHORT")
    }

    return {
        "mode": PAPER,
        "summary": summary,
        "streaks": {
            # "Current" is a per-book fact and there is no honest single answer
            # for a set, so it is reported as none while the maxima are kept.
            "current_kind": "none",
            "current_run": 0,
            "max_win": max(
                (int((r.get("streaks") or {}).get("max_win") or 0) for r in reports),
                default=0,
            ),
            "max_loss": max(
                (int((r.get("streaks") or {}).get("max_loss") or 0) for r in reports),
                default=0,
            ),
        },
        "days": ordered_days,
        "by_weekday": _merged("by_weekday", "weekday"),
        "by_hour": _merged("by_hour", "hour"),
        "by_exit_reason": _merged("by_exit_reason", "reason"),
        "by_direction": by_direction,
        "max_drawdown": max_dd,
        "max_drawdown_pct": (max_dd / peak * 100.0) if peak > 0 else 0.0,
    }

@app.get("/api/gift/portfolio", tags=["gift"])
def api_gift_portfolio(
    strategy_id: Optional[str] = Query(None, description="One strategy's book, or all"),
) -> dict[str, Any]:
    """Index-book account metrics, in the same shape as ``/api/portfolio``.

    The Performance tab's headline tiles are fed from here when the index venue
    is selected, so the two venues are compared on identical arithmetic.

    Aggregated across strategies when none is named: each book has its own
    virtual balance, so the totals are sums rather than one book's numbers
    presented as the venue's.
    """
    books = _gift_books_for(strategy_id)
    enabled = [book for book in books if book.enabled]
    if not enabled:
        return {"enabled": False, "reason": "GIFT_BOOK is off", "tradeable": False}
    metrics_rows = [(book, book.summary().get("metrics") or {}) for book in enabled]

    def _total(key: str) -> float:
        return round(sum(float(metrics.get(key) or 0.0) for _, metrics in metrics_rows), 2)

    initial = _total("initial_balance")
    equity = _total("equity")
    realized_total = _total("realized_total")
    unrealized = _total("unrealized_pnl")
    return {
        "enabled": True,
        "tradeable": False,
        "mode": PAPER,
        "currency": "INR",
        "symbol": enabled[0].symbol,
        "books": [book.strategy_id for book in enabled],
        "initial_balance": initial,
        "balance": _total("balance"),
        "equity": equity,
        "unrealized_pnl": unrealized,
        "realized_today": _total("realized_today"),
        "realized_total": realized_total,
        "total_pnl": round(realized_total + unrealized, 2),
        "total_return_pct": (
            round((realized_total + unrealized) / initial * 100.0, 2) if initial else 0.0
        ),
        "max_drawdown_pct": max(
            (round(float(m.get("max_drawdown_pct") or 0.0), 2) for _, m in metrics_rows),
            default=0.0,
        ),
        "total_trades": sum(int(m.get("total_trades") or 0) for _, m in metrics_rows),
        "win_rate": (
            round(
                sum(float(m.get("win_rate") or 0.0) * int(m.get("total_trades") or 0)
                    for _, m in metrics_rows)
                / max(1, sum(int(m.get("total_trades") or 0) for _, m in metrics_rows)),
                2,
            )
        ),
        "profit_factor": (
            None
            if any(m.get("profit_factor") == float("inf") for _, m in metrics_rows)
            else round(
                sum(float(m.get("profit_factor") or 0.0) for _, m in metrics_rows)
                / max(1, len(metrics_rows)),
                2,
            )
        ),
        "open_position_count": sum(len(book.open_positions()) for book in enabled),
    }


@app.get("/api/venues", tags=["gift"])
def api_venues(limit: int = Query(2000, ge=2, le=20000)) -> dict[str, Any]:
    """Futures venue beside the GIFT Nifty index venue, on the same signals.

    Only meaningful when ``GIFT_BOOK`` is on; otherwise the index side reports
    itself disabled rather than returning an empty book that looks like a losing
    strategy. The index side is aggregated across every strategy's book, because
    there is one book per strategy and each keeps its own ledger file.
    """
    books = [book for book in gift_books() if book.enabled]
    futures_mode = get_execution_mode()
    futures_metrics = db.get_performance_metrics(
        open_positions=db.get_open_positions(mode=futures_mode),
        prices=get_order_manager().price_map(),
        mode=futures_mode,
    )
    index_metrics = None
    if books:
        # Summed from each book's own summary, not recomputed against the main
        # ledger: the index books are separate database files, so reading their
        # positions through the futures path would value them on the wrong
        # prices and report a P&L that belongs to neither venue.
        summaries = [book.summary().get("metrics") or {} for book in books]
        index_metrics = {
            "realized_total": sum(float(m.get("realized_total") or 0.0) for m in summaries),
            "realized_today": sum(float(m.get("realized_today") or 0.0) for m in summaries),
            "unrealized_pnl": sum(float(m.get("unrealized_pnl") or 0.0) for m in summaries),
            "equity": sum(float(m.get("equity") or 0.0) for m in summaries),
            "total_trades": sum(int(m.get("total_trades") or 0) for m in summaries),
            "win_rate": (
                round(
                    sum(float(m.get("win_rate") or 0.0) * int(m.get("total_trades") or 0)
                        for m in summaries)
                    / max(1, sum(int(m.get("total_trades") or 0) for m in summaries)),
                    2,
                )
            ),
            "profit_factor": (
                None
                if any(m.get("profit_factor") == float("inf") for m in summaries)
                else round(
                    sum(float(m.get("profit_factor") or 0.0) for m in summaries)
                    / max(1, len(summaries)),
                    2,
                )
            ),
        }

    def _view(metrics: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not metrics:
            return None
        return {
            "net_pnl": round(float(metrics.get("realized_total") or 0.0), 2),
            "realized_today": round(float(metrics.get("realized_today") or 0.0), 2),
            "unrealized_pnl": round(float(metrics.get("unrealized_pnl") or 0.0), 2),
            "equity": round(float(metrics.get("equity") or 0.0), 2),
            "trades": int(metrics.get("total_trades") or 0),
            "win_rate": metrics.get("win_rate"),
            "profit_factor": metrics.get("profit_factor"),
        }

    futures_view = _view(futures_metrics)
    index_view = _view(index_metrics)
    # One index venue across strategies, so the labels come from whichever book
    # is enabled rather than from a single book that may not exist.
    index_symbol = books[0].symbol if books else None
    index_units = books[0].units if books else 0
    return {
        "signal_source": GIFT_KEY,
        "futures": {
            "label": "Nifty future",
            "mode": futures_mode,
            "symbol": get_order_manager().current_symbol(),
            "tradeable": True,
            "metrics": futures_view,
        },
        "gift_index": {
            "label": "GIFT Nifty index",
            "mode": PAPER,
            "symbol": index_symbol,
            "tradeable": False,
            "enabled": bool(books),
            "books": [book.strategy_id for book in books],
            "units": index_units,
            "metrics": index_view,
        },
        "net_pnl_gap": (
            round((index_view or {}).get("net_pnl", 0.0) - (futures_view or {}).get("net_pnl", 0.0), 2)
            if index_view and futures_view
            else None
        ),
    }


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
    selected_mode = mode or get_execution_mode()
    rows = [r for r in rows if (r.get("mode") or "").upper() == selected_mode]
    return {"trades": rows, "count": len(rows)}


@app.get("/api/signals", tags=["orders"])
def api_signals(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
    """Signal audit trail: every engine decision, accepted or blocked."""
    rows = db.get_signals(limit=limit)
    return {"signals": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Analytics + journal
# ---------------------------------------------------------------------------
@app.get("/api/analytics", tags=["analytics"])
def api_analytics(
    mode: Optional[str] = Query(None, pattern="^(PAPER|LIVE)$"),
    limit: int = Query(5000, ge=1, le=20000),
) -> dict[str, Any]:
    """Post-close trade statistics: KPIs, streaks, daily curve and breakdowns."""
    return db.get_trade_analytics(mode=mode or get_execution_mode(), limit=limit)


@app.get("/api/journal", tags=["journal"])
def api_journal(
    mode: Optional[str] = Query(None, pattern="^(PAPER|LIVE)$"),
    limit: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Settled trades merged with their journal annotations."""
    selected = mode or get_execution_mode()
    trades = [t for t in db.get_trades(limit=limit) if (t.get("mode") or "").upper() == selected]
    notes = db.get_trade_notes()
    return {
        "mode": selected,
        "entries": [{"trade": trade, "note": notes.get(int(trade["id"]))} for trade in trades],
        "count": len(trades),
        "annotated": sum(1 for trade in trades if int(trade["id"]) in notes),
    }


@app.put("/api/journal/{trade_id}", tags=["journal"])
def api_save_journal(trade_id: int, request: JournalRequest) -> dict[str, Any]:
    """Create or update the journal entry attached to a settled trade."""
    if trade_id not in {int(t["id"]) for t in db.get_trades(limit=10000)}:
        raise HTTPException(status_code=404, detail=f"Trade {trade_id} not found")
    try:
        saved = db.save_trade_note(
            trade_id,
            note=request.note,
            setup=request.setup,
            tags=request.tags,
            rating=request.rating,
            mistakes=request.mistakes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "note": saved}


@app.delete("/api/journal/{trade_id}", tags=["journal"])
def api_delete_journal(trade_id: int) -> dict[str, Any]:
    """Remove a journal entry. The trade ledger itself is never touched."""
    return {"ok": db.delete_trade_note(trade_id)}


# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------
@app.post("/api/toggle-mode", tags=["control"])
def api_toggle_mode(request: ModeRequest) -> dict[str, Any]:
    """Switch execution mode. Arming LIVE requires ``confirm=true``.

    The pre-flight report is *enforced* here, not merely displayed. It used to be
    advisory: the dashboard showed "LIVE blocked by 1 check" and the switch armed
    anyway, so an account that could not fund a single lot went live and then
    rejected every entry with "Insufficient margin" - which reads as a strategy
    that never fires rather than as an unfunded account. A gate that only warns
    is not a gate.
    """
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
    if target == LIVE:
        report = _live_readiness()
        if not report.get("ready"):
            blocking = [
                c for c in report.get("checks", [])
                if c.get("blocking") and not c.get("ok")
            ]
            raise HTTPException(
                status_code=409,
                detail=(
                    "LIVE refused: "
                    + "; ".join(f"{c['key']}: {c['detail']}" for c in blocking)
                ),
            )

    previous = get_execution_mode()
    set_execution_mode(target)
    get_client().mode = target
    # The strategy rules are identical in both modes, but the bookkeeping around
    # them is per-mode: the bar guard, the exit-candle latch, the overnight
    # reconcile and the persisted owed entry all belong to the mode being left.
    # Clearing them makes the switch a clean start, so the new mode evaluates its
    # own first bar and needs its own fresh signal. Open positions are untouched.
    get_engine().reset_for_mode(target)
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


@app.post("/api/books/pause", tags=["control"])
def api_book_pause(request: BookPauseRequest) -> dict[str, Any]:
    """Pause or resume one book's entries.

    Separate from ``/api/pause``, which is the operator's global switch: pausing
    one book must leave the others trading, and the global pause must still stop
    everything regardless of per-book state - so this can only ever narrow.
    Exits are never blocked either way: a position that cannot be closed is worse
    than one that cannot be opened.
    """
    manager = get_order_manager()
    if request.paused:
        manager.pause_book(request.strategy_id, request.reason or "MANUAL")
    else:
        manager.resume_book(request.strategy_id)
    return {
        "ok": True,
        "strategy_id": request.strategy_id,
        "paused": manager.book_paused(request.strategy_id),
        "reason": manager.book_paused_reason(request.strategy_id),
        "paused_books": manager.paused_books(),
    }


@app.post("/api/risk/clear", tags=["control"])
def api_risk_clear() -> dict[str, Any]:
    """Dismiss the reported stop-out notice.

    Housekeeping only. A stop-out no longer pauses the strategy or blocks
    entries, so this does not "resume" anything - it just clears what the
    dashboard shows.
    """
    engine = get_engine()
    previous = engine.breach
    engine.clear_breach()
    return {
        "ok": True,
        "cleared": previous.to_dict() if previous else None,
        "risk_breach": None,
    }


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

    # The index book is a separate database, so the futures square-off above
    # cannot reach it. An emergency stop that leaves index positions open is the
    # one state it must never leave behind, so every book is flattened too.
    index_books: dict[str, Any] = {}
    try:
        from gift_book import gift_books

        for book in gift_books():
            try:
                closed = book.flatten(request.reason or "PANIC")
                if closed is not None:
                    index_books[book.strategy_id] = closed
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Could not flatten the index book for %s", book.strategy_id)
                index_books[book.strategy_id] = {"error": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("Could not enumerate the index books for PANIC")
        index_books = {"error": str(exc)}
    payload["index_books"] = index_books

    log.warning(
        "PANIC via API: ok=%s %s; index books flattened: %s",
        result.ok, result.reason, sorted(index_books) or "none",
    )
    return payload


# ---------------------------------------------------------------------------
# Kite login
# ---------------------------------------------------------------------------
class TokenRequest(BaseModel):
    request_token: str = Field(..., description="request_token from the Kite redirect")


@app.get("/api/strategies", tags=["control"])
def api_strategies() -> dict[str, Any]:
    """Every configured strategy, its state, and the portfolio exposure.

    Exists because two books trading one contract are not independent: the
    per-strategy rows say what each book believes it holds, ``portfolio.netted``
    says what the broker will report, and a divergence between them is the fact
    the operator needs to see. Both are returned rather than one derived from the
    other.
    """
    import portfolio
    from config import strategy_configs

    engine = get_engine()
    runs = [engine._safe_run_status(run) for run in engine.runs]
    try:
        exposure = portfolio.get_aggregator().exposure(get_execution_mode())
    except Exception as exc:  # pragma: no cover - defensive
        exposure = {"error": str(exc)}

    # Per-book performance and account, so each band can show what that strategy
    # has actually made and what capital it holds. A trader comparing two books
    # needs the score and the equity, not just the current signal.
    #
    # Keyed on each run's *own* mode, not the bot's. Once one book can be live
    # while another stays in paper, a single mode would report the wrong ledger
    # for one of them - the paper band showing a live balance, or a live band
    # showing a simulated one, which is the number a trader would act on.
    book_modes = {run.id: run.mode() for run in engine.runs}
    try:
        performance = _strategy_performance(book_modes)
    except Exception as exc:  # pragma: no cover - defensive
        performance = {"error": str(exc)}

    try:
        accounts = _strategy_accounts(book_modes)
    except Exception as exc:  # pragma: no cover - defensive
        accounts = {"error": str(exc)}

    # Per-leg score, so a two-leg strategy can be read as two positions. The
    # book-level figure above is the sum; this is what each venue actually did.
    try:
        legs = _leg_performance(book_modes)
    except Exception as exc:  # pragma: no cover - defensive
        legs = {"error": str(exc)}

    return {
        "ok": True,
        "configured": [cfg.to_dict() for cfg in strategy_configs()],
        "runs": runs,
        "portfolio": exposure,
        "performance": performance,
        "leg_performance": legs,
        "accounts": accounts,
    }


def _book_mode(modes: Any, strategy_id: str) -> str:
    """One book's mode out of the per-book map, falling back to the bot's.

    Accepts a plain mode string as well as the map so every existing caller and
    test keeps working: ``_strategy_accounts(PAPER)`` still means "one mode for
    all books", which is what it meant before books could differ.
    """
    if isinstance(modes, dict):
        return str(modes.get(strategy_id) or get_execution_mode())
    return str(modes or get_execution_mode())


def _strategy_accounts(modes: Any) -> dict[str, dict[str, Any]]:
    """Each book's own capital, equity and margin.

    Every strategy carries its own virtual balance, so the two can be compared on
    their own equity rather than on a share of one blended number. Equity is the
    book's balance plus its open P&L, which is the same definition the aggregate
    portfolio uses - a different one here would make the two disagree.

    ``modes`` is either one mode for every book or a per-book map. The open P&L
    is read from that book's own ledger: a live book's unrealised figure cannot
    come from the paper ledger, because the two hold different fills at
    different prices.
    """
    import database as db
    import portfolio
    from config import strategy_configs

    # Every configured book must appear, not only the ones that already have a
    # row. A fresh database has a single legacy row ("Surikbs"), so with
    # gift-flip / nifty-flip configured neither band found an account and both
    # rendered a blank equity - the book is running, it just has no ledger yet.
    # `get_account` seeds a missing row, which is what the first read should do.
    for configured in strategy_configs():
        db.get_account(strategy_id=configured.id)

    # Zerodha's own cash and margins, read once for the whole report. Only used
    # for books that are live: the figure beside a LIVE label has to be the
    # money at the broker, or the label is describing a number that does not
    # exist. Reading it per book would make one API call per band per poll.
    live_cash: Optional[float] = None
    live_pnl: Optional[float] = None
    if any(_book_mode(modes, key) == LIVE for key in db.get_accounts()):
        try:
            client = get_client()
            live_cash = client.available_cash()
            # Zerodha's own P&L for the book's net quantity, so a live band can
            # show the broker's figure rather than a locally computed one.
            live_pnl = sum(
                float(row.get("pnl") or 0.0) for row in client.positions(force=True)
            )
        except Exception as exc:  # pragma: no cover - network
            log.warning("Could not read Zerodha cash/margins: %s", exc)

    rows = db.get_accounts()
    out: dict[str, dict[str, Any]] = {}
    for strategy_id, account in rows.items():
        mode = _book_mode(modes, strategy_id)
        initial = float(account.get("initial_balance") or 0.0)
        balance = float(account.get("balance") or 0.0)
        unrealised = 0.0
        for position in db.get_open_positions(mode=mode, strategy_id=strategy_id):
            entry = float(position.get("entry_price") or 0.0)
            mark = float(position.get("last_price") or entry)
            units = int(position.get("quantity") or 0) * int(position.get("position_key") or 0)
            unrealised += (mark - entry) * units
        # A live book is funded by the broker, not by the virtual balance the
        # paper ledger was seeded with. Reporting the seed beside a LIVE chip
        # showed Rs 2,00,000 of money that was not there while the account
        # actually held the Zerodha cash - the one figure a trader would act on.
        source = "ledger"
        if mode == LIVE and live_cash is not None:
            balance = float(live_cash)
            unrealised = float(live_pnl or 0.0)
            initial = float(live_cash) - unrealised
            source = "zerodha"
        equity = balance + unrealised
        out[strategy_id] = {
            "mode": mode,
            "source": source,
            "initial_balance": round(initial, 2),
            "balance": round(balance, 2),
            "unrealised": round(unrealised, 2),
            "equity": round(equity, 2),
            "return_pct": round(100.0 * (equity - initial) / initial, 2) if initial else 0.0,
        }
    return out


def _strategy_performance(modes: Any) -> dict[str, dict[str, Any]]:
    """Closed-trade stats per strategy book, keyed by strategy id.

    Computed from the trades table rather than from a running counter, so it
    survives a restart and cannot drift from the ledger the operator can read.

    ``modes`` is one mode for every book or a per-book map. Reading every trade
    and then filtering per book is what makes the split possible: a live book's
    score must come from its live trades, not from the paper trades it made
    before it was armed. Rows for one book are narrowed to that book's mode; a
    book's mode is the same for all of its own trades.
    """
    import database as db

    rows = db.get_trades(limit=5000)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("strategy_id") or db.DEFAULT_STRATEGY_ID
        if str(row.get("mode") or "") != _book_mode(modes, key):
            continue
        entry = out.setdefault(key, {
            "trades": 0, "wins": 0, "net_pnl": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0, "last_exit": None,
            "mode": _book_mode(modes, key),
        })
        net = float(row.get("net_pnl") or 0.0)
        entry["trades"] += 1
        entry["net_pnl"] += net
        if net > 0:
            entry["wins"] += 1
            entry["gross_win"] += net
        elif net < 0:
            entry["gross_loss"] += -net
        if entry["last_exit"] is None:
            entry["last_exit"] = row.get("exit_time")

    for entry in out.values():
        n = entry["trades"]
        entry["win_rate"] = round(100.0 * entry["wins"] / n, 1) if n else 0.0
        entry["net_pnl"] = round(entry["net_pnl"], 2)
        # A profit factor with no losses is unbounded, not infinite; report None
        # so the UI can say "—" instead of printing a number that means nothing.
        entry["profit_factor"] = (
            round(entry["gross_win"] / entry["gross_loss"], 2)
            if entry["gross_loss"] > 0 else None
        )
        entry.pop("gross_win", None)
        entry.pop("gross_loss", None)
    return out


def _leg_performance(modes: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Closed-trade stats per *execution leg*, keyed by strategy id then venue.

    A two-leg strategy is two positions, and each leg fills, marks and exits on
    its own. Reporting one P&L for the pair hides the half that is working and
    the half that is not - which is the whole reason the legs are tracked
    separately in the first place.

    The venue is derived from the trade's own symbol rather than from the
    strategy's configured legs: a leg that never filled has no trades, and
    inventing a zero row for it would claim a result it never had. The UI draws
    the configured legs and fills them from here, so an absent key means "no
    closed trades on this leg", which is exactly what it should say.

    The index book keeps its own database, so its trades are read from there -
    the futures ledger cannot see them.
    """
    import database as db

    out: dict[str, dict[str, dict[str, Any]]] = {}

    def _add(strategy_id: str, venue: str, row: dict[str, Any]) -> None:
        book = out.setdefault(strategy_id, {})
        entry = book.setdefault(venue, {
            "trades": 0, "wins": 0, "net_pnl": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0, "last_exit": None,
        })
        net = float(row.get("net_pnl") or 0.0)
        entry["trades"] += 1
        entry["net_pnl"] += net
        if net > 0:
            entry["wins"] += 1
            entry["gross_win"] += net
        elif net < 0:
            entry["gross_loss"] += -net
        if entry["last_exit"] is None:
            entry["last_exit"] = row.get("exit_time")

    for row in db.get_trades(limit=5000):
        key = row.get("strategy_id") or db.DEFAULT_STRATEGY_ID
        if str(row.get("mode") or "") != _book_mode(modes, key):
            continue
        _add(key, "futures", row)

    # The index legs live in their own ledger, one file per strategy.
    try:
        from gift_book import gift_books

        for book in gift_books():
            if not book.enabled:
                continue
            for row in book.trades(limit=5000):
                _add(book.strategy_id, "gift", row)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not read the index legs' trades: %s", exc)

    for book in out.values():
        for entry in book.values():
            n = entry["trades"]
            entry["win_rate"] = round(100.0 * entry["wins"] / n, 1) if n else 0.0
            entry["net_pnl"] = round(entry["net_pnl"], 2)
            entry["profit_factor"] = (
                round(entry["gross_win"] / entry["gross_loss"], 2)
                if entry["gross_loss"] > 0 else None
            )
            entry.pop("gross_win", None)
            entry.pop("gross_loss", None)
    return out


@app.get("/api/alerts/status", tags=["control"])
def api_alerts_status() -> dict[str, Any]:
    """What alerting is configured, and what it has sent.

    The token is never returned - only whether it is set - so this endpoint is
    safe to leave reachable from the dashboard.
    """
    import notifier

    return {
        "ok": True,
        "alerting": notifier.status(),
        "watchdog": get_watchdog().state.to_dict(),
    }


class AlertToggleRequest(BaseModel):
    enabled: bool


@app.post("/api/alerts/enabled", tags=["control"])
def api_alerts_set_enabled(payload: AlertToggleRequest) -> dict[str, Any]:
    """Switch alerting on or off, now and after a restart.

    Stored in SQLite rather than rewritten into ``.env``: the file is the
    deployment's default, this is the operator's live intent, and a container
    restart must not silently revert a decision someone made deliberately.
    """
    import notifier

    state = notifier.set_enabled(payload.enabled)
    log.warning("Alerting %s from the dashboard", "enabled" if state else "disabled")
    return {"ok": True, "alerting": notifier.status(), "enabled": state}


@app.delete("/api/alerts/enabled", tags=["control"])
def api_alerts_clear_enabled() -> dict[str, Any]:
    """Drop the override so ``ALERT_ENABLED`` from .env applies again."""
    import notifier

    state = notifier.clear_enabled()
    return {"ok": True, "alerting": notifier.status(), "enabled": state}


@app.post("/api/alerts/test", tags=["control"])
def api_alerts_test() -> dict[str, Any]:
    """Send one test alert on the configured channel.

    Exists because a channel that is silently misconfigured looks exactly like a
    quiet market. Testing it on demand is the only way to know before a real
    problem needs to reach you.
    """
    import notifier

    if not notifier.configured():
        reason = (
            "ALERT_ENABLED is off"
            if not notifier.enabled()
            else f"ALERT_CHANNEL={settings.alert_channel} is not fully configured"
        )
        raise HTTPException(
            status_code=400, detail=f"{reason}; see /api/alerts/status"
        )
    payload = {
        "kind": "TEST_ALERT",
        "detail": "This is a test alert from Belaku. If you can read this, alerting works.",
        "severity": "info",
        "source": "belaku",
        "mode": get_execution_mode(),
        "at_ist": db.iso(),
    }
    delivered = notifier.send(payload)
    return {
        "ok": delivered,
        "delivered": delivered,
        "channel": settings.alert_channel,
        "detail": "Test alert sent" if delivered else "Delivery failed; check the bot log",
    }


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
    # Resolve the execution future and hand it to the feed before start(), so
    # _handle_connect can subscribe it in the same pass as GIFT Nifty. A failure
    # here is not fatal: refresh_exec_quote() polls instead.
    symbol = get_order_manager().current_symbol()
    feed.set_exec_token(symbol, feed._resolve_exec_token(symbol))
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


def _callback_page(
    ok: bool, title: str, detail: str, extra: str = "", code: str = ""
) -> HTMLResponse:
    """Human-readable result for the browser that just came back from Zerodha.

    Every value here is potentially attacker-influenced — ``status`` comes off
    the query string and ``detail``/``code`` can carry broker error text — so
    all of them are HTML-escaped. ``code`` is the single slot rendered inside a
    ``<code>`` chip, and it is escaped too rather than accepted as markup: this
    page runs same-origin with the dashboard and its control endpoints.
    """
    colour = "#10b981" if ok else "#f43f5e"
    icon = "✅" if ok else "⚠️"
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(settings.bot_name)} · {html.escape(title)}</title>
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
  <h1>{icon} {html.escape(title)}</h1>
  <p>{html.escape(detail)} {f'<code>{html.escape(code)}</code>' if code else ''}</p>
  {f'<p class="extra">{html.escape(extra)}</p>' if extra else ''}
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
            "Zerodha did not return a"
            + (f" (status={status})" if status else "")  # escaped by _callback_page
            + ".",
            "Usually this means the app's Redirect URL does not match "
            f"{kite_auth.callback_url()} exactly. Check it in the "
            "Kite developer console, or paste the request_token manually on the "
            "dashboard.",
            code="request_token",
        )

    try:
        result = kite_auth.exchange_request_token(request_token)
    except kite_auth.KiteAuthError as exc:
        return _callback_page(
            False,
            "Token exchange failed",
            "The exchange was rejected:",
            "request_tokens are single-use and short-lived. Start the login "
            "again from the dashboard.",
            code=str(exc),
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
# Authentication
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    password: str = Field(..., max_length=200)


@app.get("/api/auth/status", tags=["auth"])
def api_auth_status(request: Request) -> dict[str, Any]:
    """Whether a password is configured, and whether this caller is signed in."""
    required = dashboard_auth.auth_required()
    return {
        "auth_required": required,
        "authenticated": _session_valid(request) if required else True,
    }


@app.post("/api/auth/login", tags=["auth"])
def api_auth_login(request: Request, payload: LoginRequest) -> Any:
    """Exchange the dashboard password for a signed session cookie."""
    if not dashboard_auth.auth_required():
        return {"ok": True, "auth_required": False}

    client = _client_key(request)
    if dashboard_auth.rate_limited(client):
        log.warning("Login rate limited for %s", client)
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again in a few minutes.",
        )

    if not dashboard_auth.check_password(payload.password):
        dashboard_auth.record_failure(client)
        log.warning("Failed dashboard login from %s", client)
        raise HTTPException(status_code=401, detail="Incorrect password")

    dashboard_auth.clear_failures(client)
    log.info("Dashboard login from %s", client)
    response = JSONResponse({"ok": True, "auth_required": True})
    response.set_cookie(
        dashboard_auth.COOKIE_NAME,
        dashboard_auth.issue_token(),
        max_age=dashboard_auth.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        # Not Secure: the dashboard is served over plain HTTP on loopback or a
        # private network. Setting Secure here would make the cookie unusable
        # and lock the operator out. Put TLS in front before exposing it.
        secure=False,
        path="/",
    )
    return response


@app.post("/api/auth/logout", tags=["auth"])
def api_auth_logout() -> Any:
    response = JSONResponse({"ok": True})
    response.delete_cookie(dashboard_auth.COOKIE_NAME, path="/")
    return response


@app.get("/login", include_in_schema=False)
def login_page() -> Any:
    page = WEB_DIR / "login.html"
    if not page.is_file():
        return HTMLResponse("<h1>Login page missing</h1>", status_code=500)
    return FileResponse(page, media_type="text/html")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def _asset_version() -> str:
    """Cache-buster derived from the assets themselves.

    The page used to hardcode ``?v=trader-40``. Browsers cache ``app.js`` under
    that exact URL, so every later change to the markup or the script was
    invisible until someone cleared their cache - and a missing function then
    looked like "the dashboard is broken" rather than "you are running the old
    file". Hashing the files makes the URL change whenever they do, which is the
    only reliable way to guarantee the browser fetches what the server has.
    """
    parts: list[str] = []
    for name in ("app.js", "styles.css"):
        path = WEB_DIR / name
        try:
            stat = path.stat()
            parts.append(f"{name}:{stat.st_size}:{int(stat.st_mtime)}")
        except OSError:
            parts.append(f"{name}:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:10]


@app.get("/", include_in_schema=False)
def index() -> Any:
    page = WEB_DIR / "index.html"
    if not page.is_file():
        return JSONResponse(
            {"detail": f"Dashboard not found at {page}", "api": "/docs"}, status_code=404
        )
    try:
        page_html = page.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        return JSONResponse({"detail": f"Cannot read dashboard: {exc}"}, status_code=500)
    # Rewrite the placeholder version, so a redeploy needs no manual edit and a
    # stale cached bundle cannot survive a change.
    page_html = page_html.replace("?v=trader-40", f"?v={_asset_version()}")
    return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})


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
