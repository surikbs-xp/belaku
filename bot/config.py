"""Central configuration for the GIFT Nifty -> Nifty Futures execution bot.

All values are sourced from the environment (.env) with safe defaults so the
system boots in PAPER mode without any credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

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

    # --- Dashboard / API bind --------------------------------------------
    # No inbound webhook: the bot pulls market data itself. These only control
    # where the dashboard and JSON API listen.
    webhook_host: str = _env("WEBHOOK_HOST", "127.0.0.1")
    webhook_port: int = _env_int("WEBHOOK_PORT", 9000)

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

    # --- Risk / pricing ---------------------------------------------------
    lot_margin: float = _env_float("LOT_MARGIN", 150_000.0)
    instrument_refresh_seconds: int = _env_int("INSTRUMENT_REFRESH_SECONDS", 3600)
    quote_poll_seconds: int = _env_int("QUOTE_POLL_SECONDS", 5)
    http_timeout_seconds: float = _env_float("HTTP_TIMEOUT_SECONDS", 4.0)

    # --- Strategy (st.pine) ----------------------------------------------
    # The engine evaluates RSI, TSV, SuperTrend and PSAR on GIFT Nifty candles.
    strategy_st_min_agree: int = _env_int("STRATEGY_ST_MIN_AGREE", 2)
    strategy_psar_start: float = _env_float("STRATEGY_PSAR_START", 0.02)
    strategy_psar_increment: float = _env_float("STRATEGY_PSAR_INCREMENT", 0.02)
    strategy_psar_maximum: float = _env_float("STRATEGY_PSAR_MAXIMUM", 0.2)
    strategy_rsi_length: int = _env_int("STRATEGY_RSI_LENGTH", 14)
    strategy_rsi_buy_max: float = _env_float("STRATEGY_RSI_BUY_MAX", 20.0)
    strategy_rsi_sell_min: float = _env_float("STRATEGY_RSI_SELL_MIN", 80.0)
    strategy_tsv_length: int = _env_int("STRATEGY_TSV_LENGTH", 13)

    # --- GIFT Nifty signal feed -------------------------------------------
    # The engine builds its own candles from live NSEIX ticks (Kite serves no
    # GIFT Nifty history), then evaluates st.pine on them. This interval must
    # match the timeframe the Pine strategy was tuned on.
    gift_candle_interval: str = _env("GIFT_CANDLE_INTERVAL", "5minute")

    # --- Engine loop ------------------------------------------------------
    # How often the mark loop updates PnL/equity and the strategy engine checks
    # whether a new GIFT Nifty bar has closed.
    engine_interval_seconds: float = _env_float("ENGINE_INTERVAL_SECONDS", 5.0)

    # --- Storage ----------------------------------------------------------
    db_path: str = _env_path("DB_PATH", str(BASE_DIR / "paper_trading.db"))

    extras: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
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


def strategy_params() -> dict[str, float]:
    """Indicator parameters for :mod:`strategy`, sourced from the environment."""
    return {
        "st_min_agree": settings.strategy_st_min_agree,
        "psar_start": settings.strategy_psar_start,
        "psar_increment": settings.strategy_psar_increment,
        "psar_maximum": settings.strategy_psar_maximum,
        "rsi_length": settings.strategy_rsi_length,
        "rsi_buy_max": settings.strategy_rsi_buy_max,
        "rsi_sell_min": settings.strategy_rsi_sell_min,
        "tsv_length": settings.strategy_tsv_length,
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
    return warnings
