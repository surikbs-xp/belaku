"""Central configuration for the GIFT Nifty -> Nifty Futures execution bot.

All values are sourced from the environment (.env) with safe defaults so the
system boots in PAPER mode without any credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv, dotenv_values

import strategy

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)


def _prefer_env_file_token() -> None:
    """Let a token written by the login flow win over the process environment.

    ``load_dotenv`` does not override variables that are already set, which is
    normally what you want: a container's ``environment:`` block should beat a
    stale file. The Kite token is the exception.

    The bot writes a refreshed token into ``.env`` when someone logs in through
    the browser. On a host where the token is *also* supplied as an environment
    variable - a Lightsail deployment spec, a compose ``environment:`` entry -
    the stale value would win on the next restart and silently discard the
    login. Only this one key is re-read, because ``.env`` may also carry
    defaults (``WEBHOOK_HOST=127.0.0.1``) that must not override the deployment.
    """
    try:
        values = dotenv_values(ENV_FILE)
    except OSError:
        return
    token = (values.get("KITE_ACCESS_TOKEN") or "").strip()
    if token:
        os.environ["KITE_ACCESS_TOKEN"] = token


_prefer_env_file_token()

PAPER = "PAPER"
LIVE = "LIVE"
VALID_MODES = (PAPER, LIVE)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int_optional(name: str, default: Optional[int] = None) -> Optional[int]:
    """An int setting where empty means "unset" rather than zero.

    ``_env_int`` cannot express this: it falls back to the default on a bad
    value, so an operator who deliberately blanks the variable would silently
    get the default back. Here an empty string is a real answer - "leave the
    parameter off" - which is what ``MARKET_PROTECTION`` needs.
    """
    raw = _env(name, "" if default is None else str(default))
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return default


def _env_path(name: str, default: str) -> str:
    path = Path(_env(name, default)).expanduser()
    return str(path if path.is_absolute() else BASE_DIR / path)


@dataclass(frozen=True)
class Settings:
    # --- Zerodha Kite -----------------------------------------------------
    api_key: str = _env("KITE_API_KEY")
    api_secret: str = _env("KITE_API_SECRET")
    access_token: str = _env("KITE_ACCESS_TOKEN")
    # Where Kite sends the browser back after login. Point this at the bot's
    # own /zerodha/callback and the request_token is captured automatically.
    kite_redirect_url: str = _env("KITE_REDIRECT_URL", "http://127.0.0.1:9000/zerodha/callback")

    # --- Headless login (optional) ---------------------------------------
    # Kite has no token API, so an unattended deployment has to perform the
    # browser flow itself. With these three set the bot logs in on startup and
    # whenever the token is rejected, which is what makes a stateless container
    # viable: the token never has to survive a restart.
    #
    # Requires TOTP-based 2FA on the account. An account that approves logins
    # through the Kite mobile app cannot be logged in headlessly.
    kite_user_id: str = _env("KITE_USER_ID")
    kite_password: str = _env("KITE_PASSWORD")
    kite_totp_secret: str = _env("KITE_TOTP_SECRET")
    # How often the token keeper checks whether the token still works. The
    # token expires once a day, so this only needs to be frequent enough to
    # recover before the next bar closes.
    token_check_seconds: float = _env_float("TOKEN_CHECK_SECONDS", 300.0)

    # --- Dashboard / API bind --------------------------------------------
    # No inbound webhook: the bot pulls market data itself. These only control
    # where the dashboard and JSON API listen.
    webhook_host: str = _env("WEBHOOK_HOST", "127.0.0.1")
    webhook_port: int = _env_int("WEBHOOK_PORT", 9000)
    # Shown as the browser tab title and the dashboard header.
    bot_name: str = _env("BOT_NAME", "Belaku")
    # Build provenance, baked into the image by the CI build (see Dockerfile).
    # Empty for a local `docker build` or a bare `python bot/bot.py`, which the
    # dashboard reports as "unknown" rather than inventing a version.
    build_sha: str = _env("BUILD_SHA", "")
    build_time: str = _env("BUILD_TIME", "")
    build_ref: str = _env("BUILD_REF", "")
    # Host header allow-list. Without this a malicious page can DNS-rebind its
    # own hostname to 127.0.0.1 and talk to the API "same-origin" with no CORS
    # involvement at all.
    allowed_hosts: str = _env(
        "ALLOWED_HOSTS", "127.0.0.1,localhost,[::1],testserver"
    )
    # Extra origins allowed to make cross-origin calls, beyond same-origin.
    # Empty means same-origin only, which is what the bundled dashboard needs.
    allowed_origins: str = _env("ALLOWED_ORIGINS", "")
    # Dashboard password. Empty leaves the dashboard open, which is only
    # acceptable on a machine nobody else can reach - the control endpoints can
    # arm live trading and square off positions.
    dashboard_password: str = _env("DASHBOARD_PASSWORD", "")
    # Pins the session signing key so logins survive a restart. Empty means a
    # fresh random key per process, which invalidates sessions on restart.
    dashboard_session_secret: str = _env("DASHBOARD_SESSION_SECRET", "")

    # --- Execution --------------------------------------------------------
    execution_mode: str = _env("EXECUTION_MODE", PAPER).upper()
    default_lots: int = _env_int("DEFAULT_LOTS", 1)
    lot_size: int = _env_int("NIFTY_LOT_SIZE", 65)
    virtual_balance: float = _env_float("VIRTUAL_BALANCE", 200_000.0)
    max_open_lots: int = _env_int("MAX_OPEN_LOTS", 4)
    slippage_ticks: float = _env_float("SLIPPAGE_TICKS", 1.0)
    tick_size: float = _env_float("TICK_SIZE", 0.05)
    product: str = _env("PRODUCT", "NRML")
    order_type: str = _env("ORDER_TYPE", "MARKET")
    # Zerodha's market-protection band, in ticks. A MARKET order without one can
    # fill far from the touch in a thin book, and the exchange rejects an order
    # whose protection band is too wide. -1 lets Kite choose; empty leaves the
    # parameter off entirely so the broker's own default applies.
    market_protection: Optional[int] = _env_int_optional("MARKET_PROTECTION", -1)
    # Whether to place an exchange-held SL-M behind every LIVE position. This is
    # the difference between a stop that survives the bot dying and one that does
    # not: the engine's stop is a software check on a mark loop, so a crash, a
    # kill or an API outage leaves the position with no stop at all. On by
    # default; turning it off is only reasonable for a broker account that cannot
    # accept SL-M orders.
    protective_stops: bool = _env_bool("PROTECTIVE_STOPS", True)

    # --- Risk / pricing ---------------------------------------------------
    lot_margin: float = _env_float("LOT_MARGIN", 150_000.0)
    # Stop opening new trades once the day's realised loss reaches this many
    # rupees. 0 disables the check. This is a portfolio-level circuit breaker,
    # not a per-trade stop: the strategy's own signal plus each book's
    # protective stop are the exits, and this only refuses *new* entries. Open
    # positions are left alone - flattening them here would turn a bad day into
    # a realised one at the worst possible moment, and they already carry their
    # own stop.
    max_daily_loss: float = _env_float("MAX_DAILY_LOSS", 0.0)

    # --- Alerting ---------------------------------------------------------
    # Where alerts go. Empty disables sending; alerts are still logged and shown
    # on the dashboard.
    #
    #   telegram  needs ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID
    #   slack     needs ALERT_WEBHOOK_URL (incoming webhook)
    #   discord   needs ALERT_WEBHOOK_URL (channel webhook)
    #   ntfy      needs ALERT_WEBHOOK_URL (https://ntfy.sh/<topic>)
    #   generic   needs ALERT_WEBHOOK_URL; receives the raw JSON payload
    #
    # The channel matters because each service rejects a payload it does not
    # recognise: Slack wants {"text": ...}, Discord {"content": ...}, ntfy a
    # plain body. Sending one generic shape to all of them silently fails.
    alert_channel: str = _env("ALERT_CHANNEL", "generic").strip().lower()
    # Alerts are off unless deliberately switched on. The channel settings above
    # say *where* a message would go; this says whether any goes at all, so a
    # filled-in token in .env cannot turn the phone into a log stream by itself.
    alert_enabled: bool = _env_bool("ALERT_ENABLED", False)
    alert_webhook_url: str = _env("ALERT_WEBHOOK_URL", "")
    alert_telegram_bot_token: str = _env("ALERT_TELEGRAM_BOT_TOKEN", "")
    alert_telegram_chat_id: str = _env("ALERT_TELEGRAM_CHAT_ID", "")
    # Send a message when the bot starts and stops, so a silent crash is
    # distinguishable from a quiet market.
    alert_on_lifecycle: bool = _env_bool("ALERT_ON_LIFECYCLE", True)

    # How often the watchdog checks trading health, in seconds.
    watchdog_interval_seconds: float = _env_float("WATCHDOG_INTERVAL_SECONDS", 30.0)
    # How often the mark loop reconciles unresolved orders against the Zerodha
    # order book. A late fill must be discovered while the bot is idle, not only
    # when it next tries to trade - otherwise a position can exist at the broker
    # that the ledger does not know about, and its protective stop is never
    # applied. 0 disables the periodic pass (startup and pre-order reconciliation
    # still run).
    reconcile_interval_seconds: float = _env_float("RECONCILE_INTERVAL_SECONDS", 60.0)
    instrument_refresh_seconds: int = _env_int("INSTRUMENT_REFRESH_SECONDS", 3600)
    quote_poll_seconds: int = _env_int("QUOTE_POLL_SECONDS", 5)
    http_timeout_seconds: float = _env_float("HTTP_TIMEOUT_SECONDS", 4.0)

    # --- Strategy (SuperTrend + PSAR) -------------------------------------
    # Exactly two indicators, both fixed: SuperTrend 14×3 and PSAR
    # (0.02, 0.02, 0.2). There is no vote across instances and no third
    # indicator, so the only strategy knobs are the PSAR inputs.
    strategy_psar_start: float = _env_float("STRATEGY_PSAR_START", 0.02)
    strategy_psar_increment: float = _env_float("STRATEGY_PSAR_INCREMENT", 0.02)
    strategy_psar_maximum: float = _env_float("STRATEGY_PSAR_MAXIMUM", 0.2)
    # Exit rule. ``flip`` holds a position through a NO_SIGNAL bar and closes it
    # when the pair turns the other way; ``flat`` closes on the first
    # disagreement. ``flip`` is the default because on 3 years of real bars
    # (scripts/backtest.py) it has the higher payoff ratio, and both are sharper
    # net of the 14.85 points per round trip than every other exit tested.
    strategy_exit: str = _env("STRATEGY_EXIT", "flip").strip().lower()
    # Which ledger book this process trades. Two strategies trading the same
    # front-month contract are separated by this id, not by their symbol, so it
    # must match the rows already in the database or the bot will look flat when
    # it is not. Defaults to the id every pre-existing row carries.
    strategy_id: str = _env("STRATEGY_ID", "Surikbs").strip() or "Surikbs"
    # --- GIFT Nifty signal feed -------------------------------------------
    # The engine loads GIFT Nifty candles from Kite history (the source of
    # truth) and falls back to building them from live NSEIX ticks only while
    # history is unavailable, then evaluates st.pine on them. This interval must
    # match the timeframe the Pine strategy was tuned on.
    gift_candle_interval: str = _env("GIFT_CANDLE_INTERVAL", "60minute")

    # --- Engine loop ------------------------------------------------------
    # How often the mark loop updates PnL/equity and the strategy engine checks
    # whether a new GIFT Nifty bar has closed.
    engine_interval_seconds: float = _env_float("ENGINE_INTERVAL_SECONDS", 5.0)

    # --- Storage ----------------------------------------------------------
    db_path: str = _env_path("DB_PATH", str(BASE_DIR / "paper_trading.db"))

    extras: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def gift_candle_interval_seconds(self) -> int:
        """Bar length in seconds, derived from ``GIFT_CANDLE_INTERVAL``."""
        import gift_nifty  # local: gift_nifty imports this module

        return gift_nifty.interval_to_seconds(self.gift_candle_interval)

    @property
    def is_live_configured(self) -> bool:
        return bool(self.api_key and _env("KITE_ACCESS_TOKEN", self.access_token) and self.api_secret)

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)


settings = Settings()


def get_execution_mode() -> str:
    """Execution mode, re-read from the environment on every call."""
    mode = _env("EXECUTION_MODE", settings.execution_mode).upper()
    return mode if mode in VALID_MODES else PAPER


def set_execution_mode(mode: str) -> str:
    """Persist the mode in this process' environment and return it."""
    mode = mode.upper()
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid execution mode: {mode}")
    os.environ["EXECUTION_MODE"] = mode
    return mode


def strategy_params() -> dict[str, Any]:
    """Indicator parameters for :mod:`strategy`, sourced from the environment.

    Only the PSAR inputs are configurable: the SuperTrend instance is fixed at
    14×3, because a second one would reintroduce the vote this strategy dropped.
    """
    return {
        "psar_start": settings.strategy_psar_start,
        "psar_increment": settings.strategy_psar_increment,
        "psar_maximum": settings.strategy_psar_maximum,
        "exit": settings.strategy_exit,
    }


def strategy_summary() -> dict[str, Any]:
    """Human-readable strategy configuration for the dashboard."""
    return {
        "supertrend": {
            "length": strategy.SUPERTREND_ATR_LENGTH,
            "multiplier": strategy.SUPERTREND_MULTIPLIER,
        },
        "supertrend_label": (
            f"{strategy.SUPERTREND_ATR_LENGTH}×{strategy.SUPERTREND_MULTIPLIER:g}"
        ),
        # The confirmation vote is 2-of-2: the SuperTrend direction plus the PSAR
        # price-position check, both required.
        "confirmations_total": 2,
        "psar": {
            "start": settings.strategy_psar_start,
            "increment": settings.strategy_psar_increment,
            "maximum": settings.strategy_psar_maximum,
        },
    }


def validate() -> list[str]:
    """Return a list of human readable configuration warnings."""
    warnings: list[str] = []
    mode = get_execution_mode()
    if mode not in VALID_MODES:
        warnings.append(f"Unknown EXECUTION_MODE {mode!r}; falling back to PAPER.")
    if not _env("KITE_ACCESS_TOKEN", settings.access_token):
        warnings.append(
            "KITE_ACCESS_TOKEN is empty - the GIFT Nifty feed needs it and the bot "
            "cannot evaluate the strategy until it is set."
        )
    if mode == LIVE and not settings.is_live_configured:
        warnings.append("EXECUTION_MODE=LIVE but Kite credentials are incomplete.")
    # ORDER_TYPE is used for entries *and* exits, and the client only forwards a
    # price for LIMIT. An SL or SL-M here would send an order with no trigger
    # price - which the exchange rejects - and the protective stop is placed
    # separately as its own SL-M, so there is no reason to configure one here.
    order_type = str(settings.order_type or "").upper()
    if order_type not in {"MARKET", "LIMIT"}:
        warnings.append(
            f"ORDER_TYPE={settings.order_type!r} is not supported for entries and exits; "
            "use MARKET or LIMIT. Protective stops are placed as their own SL-M."
        )
    if not getattr(settings, "protective_stops", True) and mode == LIVE:
        warnings.append(
            "PROTECTIVE_STOPS is off: LIVE positions will have no exchange-held stop "
            "and are protected only while this process is running."
        )
    # Two books sharing an id share one ledger, one account and one set of
    # positions. The engine would build both, the dashboard would show two bands
    # with the same name, and every scoped query would return both books' rows -
    # so a stop-out on one would close the other. Nothing downstream can tell
    # them apart, which makes this a configuration error worth refusing loudly.
    try:
        ids = [cfg.id for cfg in strategy_configs() if cfg.enabled]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            warnings.append(
                f"Duplicate strategy id(s) {', '.join(duplicates)}: each book needs its "
                "own STRATEGY_<n>_ID, or they share one ledger and one position."
            )
    except Exception:  # pragma: no cover - defensive
        pass
    return warnings


# ---------------------------------------------------------------------------
# Multiple strategies
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StrategyConfig:
    """One strategy's settings, as a unit rather than as scattered keys.

    A flat namespace cannot describe two strategies: ``STRATEGY_EXIT`` can only
    hold one value. Each run is therefore configured as ``STRATEGY_<n>_*`` and
    parsed into one of these.
    """

    index: int
    id: str
    source: str
    interval: str
    exit: str = strategy.EXIT_FLIP
    lots: int = 1
    enabled: bool = True
    name: str = ""
    # Optional entry filters, both off by default. ADX gates on trend strength,
    # the moving average on the trend's own side; see the Backtesting page for
    # what they measured and why they are not on by default.
    filter_adx: float = 0.0
    filter_ma: int = 0
    # Whether this book places real orders once the bot is switched to LIVE.
    #
    # A per-run switch rather than a global one, because the two books are not
    # equivalent instruments. GIFT Nifty is a *non-tradeable index* on Kite, so
    # its signal can never be filled as itself - a LIVE order from the GIFT book
    # becomes a Nifty-futures order, which is a different instrument from the one
    # the strategy read. The NIFTY book reads the Nifty index and can genuinely
    # trade it. Defaulting to False keeps a book that has not said so explicitly
    # in paper, which is the safe direction: an unset flag must never be what
    # puts real money on a signal the operator has not thought about.
    live: bool = False
    # Protective stop, in index points, measured on the *futures* execution
    # price. ``stop_loss`` is the initial distance from the fill; the trail
    # activates at ``trail_after`` points of open profit and then follows the
    # best price at ``trail_points`` (or ``trail_points_after`` once the wider
    # band is reached). Zero disables the corresponding piece, so a book with
    # ``stop_loss=0`` keeps the signal-only exit it had before.
    stop_loss: float = 0.0
    trail_points: float = 0.0
    trail_after: float = 0.0
    trail_points_after: float = 0.0

    @property
    def stop_rules(self) -> Any:
        """This book's stop configuration, as the ``stops`` module reads it."""
        import stops

        return stops.StopRules(
            initial_points=self.stop_loss,
            trail_points=self.trail_points,
            trail_after=self.trail_after,
            trail_points_after=self.trail_points_after,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "id": self.id,
            "name": self.name or self.id,
            "source": self.source,
            "interval": self.interval,
            "exit": self.exit,
            "lots": self.lots,
            "enabled": self.enabled,
            "filter_adx": self.filter_adx,
            "filter_ma": self.filter_ma,
            "live": self.live,
            "stop_loss": self.stop_loss,
            "trail_points": self.trail_points,
            "trail_after": self.trail_after,
            "trail_points_after": self.trail_points_after,
        }

    def strategy_params(self) -> dict[str, Any]:
        """The params ``strategy.evaluate`` takes, from this run's settings."""
        return {
            "psar_start": settings.strategy_psar_start,
            "psar_increment": settings.strategy_psar_increment,
            "psar_maximum": settings.strategy_psar_maximum,
            "exit": self.exit,
            # Without these the filters were parsed and reported but never
            # applied, so a strategy configured as "ADX>15" traded unfiltered.
            "filter_adx": self.filter_adx,
            "filter_ma": self.filter_ma,
        }


# The highest index the parser will look for, so a stray env var cannot make it
# walk forever.
MAX_STRATEGIES = 8


def strategy_configs() -> list[StrategyConfig]:
    """Every configured strategy, in order.

    With nothing configured this returns exactly one strategy built from the flat
    keys, which is what every existing deployment has - so an unchanged ``.env``
    behaves precisely as before. Adding ``STRATEGY_2_SOURCE=nifty`` is what turns
    the second book on, and nothing else has to change.
    """
    out: list[StrategyConfig] = []
    for index in range(1, MAX_STRATEGIES + 1):
        prefix = f"STRATEGY_{index}_"
        source = _env(prefix + "SOURCE")
        explicit = bool(source)
        if index > 1 and not explicit:
            # Only the first strategy may be implied by the flat keys; a second
            # needs to say what it reads, or it would silently duplicate the
            # first and trade the same book twice.
            continue
        if not explicit and index == 1:
            source = "gift"
        resolved_source = (source or "gift").strip().lower()
        out.append(
            StrategyConfig(
                index=index,
                id=_env(prefix + "ID", settings.strategy_id if index == 1 else "")
                or (settings.strategy_id if index == 1 else f"strategy-{index}"),
                source=resolved_source,
                interval=_env(
                    prefix + "INTERVAL",
                    settings.gift_candle_interval if index == 1 else "60minute",
                ),
                exit=(
                    _env(prefix + "EXIT", settings.strategy_exit if index == 1 else "flip")
                    .strip().lower()
                ),
                lots=_env_int(
                    prefix + "LOTS", settings.default_lots if index == 1 else 1
                ),
                enabled=_env_bool(prefix + "ENABLED", True),
                name=_env(prefix + "NAME", ""),
                filter_adx=_env_float(prefix + "FILTER_ADX", 0.0),
                filter_ma=_env_int(prefix + "FILTER_MA", 0),
                live=_env_bool(prefix + "LIVE", False),
                stop_loss=_env_float(prefix + "STOP_LOSS", 0.0),
                trail_points=_env_float(prefix + "TRAIL_POINTS", 0.0),
                trail_after=_env_float(prefix + "TRAIL_AFTER", 0.0),
                trail_points_after=_env_float(prefix + "TRAIL_POINTS_AFTER", 0.0),
            )
        )
    return out


def enabled_strategies() -> list[StrategyConfig]:
    return [cfg for cfg in strategy_configs() if cfg.enabled]


def build_bar_source_for(cfg: StrategyConfig, *, gift_feed: Any = None) -> Any:
    """The bar source one strategy reads.

    Imported lazily so ``config`` stays free of a module-level dependency on
    ``bar_source`` (which imports ``config`` in turn). The GIFT feed is passed in
    rather than constructed here: it owns a process-global websocket, so every
    GIFT strategy must share the one instance.
    """
    from bar_source import GiftBarSource, NiftyBarSource

    if cfg.source in {"gift", "gift_nifty", "nseix:gift nifty"}:
        # Pass the configured interval through: the feed builds it from the same
        # ticks as its own series. Dropping it here is what made
        # STRATEGY_<n>_INTERVAL silently do nothing for a GIFT book.
        return GiftBarSource(gift_feed, interval=cfg.interval)
    if cfg.source in {"nifty", "nifty50", "nse:nifty 50"}:
        return NiftyBarSource(cfg.interval)
    raise ValueError(f"Unknown source {cfg.source!r} for strategy {cfg.id!r}")
