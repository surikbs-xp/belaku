"""Operational watchdog: alert when the bot is up but not actually working.

The container healthcheck answers "is the process alive?". That is not the same
question. The failure modes that cost money are the ones where the process is
perfectly healthy and the *trading* is not:

* the signal series has stopped refreshing, or Kite history is failing (a token
  that expired mid-session, or a dead network path);
* the strategy is paused or latched on a risk breach and nobody noticed;
* orders are being rejected - margin, a bad symbol, an expired instrument dump.

None of these trip a liveness probe, so they need their own check. Alerts go to
a webhook (``ALERT_WEBHOOK_URL``) so they reach a phone rather than a log file
nobody is reading at 09:20.

The watchdog is deliberately conservative: it alerts on *sustained* conditions
and rate-limits each distinct alert, because an alerting system that cries wolf
gets muted, and a muted alerting system is worse than none.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import settings

log = logging.getLogger("watchdog")

# A signal series that has not refreshed for this long is treated as stale. The
# source polls Kite history every few seconds, so a multi-minute gap during
# market hours means something is wrong, not that the market is quiet.
STALE_TICK_SECONDS = 180.0

# The mark loop runs every ENGINE_INTERVAL_SECONDS (5s by default). Missing this
# many intervals in a row means the thread is gone, not that it is slow: the loop
# is the only thing that marks positions to market and samples equity, so a dead
# loop leaves the dashboard showing a frozen P&L that looks like a quiet market.
STALE_LOOP_INTERVALS = 6

# Do not repeat the same alert more often than this.
ALERT_COOLDOWN_SECONDS = 900.0

# Consecutive failing checks before alerting, so a single slow poll does not
# page anyone.
FAILURES_BEFORE_ALERT = 3


@dataclass
class Alert:
    kind: str
    detail: str
    severity: str = "warning"
    # Structured context rendered as labelled fields by the notifier. A dict
    # rather than more dataclass attributes because the useful fields differ per
    # alert kind - a feed alert has a tick age, an order alert has an order id -
    # and a fixed schema would be mostly empty.
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "severity": self.severity,
            "fields": {k: v for k, v in self.fields.items() if v not in (None, "")},
        }


@dataclass
class WatchdogState:
    """Last observed health, surfaced on the dashboard."""

    last_check_at: Optional[float] = None
    last_alert_at: Optional[float] = None
    last_alert: Optional[dict[str, Any]] = None
    alerts_sent: int = 0
    # How many of those the channel actually accepted. Separate from
    # `alerts_sent` because "the watchdog fired" and "the phone buzzed" are
    # different facts, and only the second one means the operator was told.
    alerts_delivered: int = 0
    consecutive_failures: int = 0
    healthy: bool = True
    checks: int = 0
    _cooldowns: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_check_at": self.last_check_at,
            "last_alert_at": self.last_alert_at,
            "last_alert": self.last_alert,
            "alerts_sent": self.alerts_sent,
            "alerts_delivered": self.alerts_delivered,
            "consecutive_failures": self.consecutive_failures,
            "healthy": self.healthy,
            "checks": self.checks,
        }


def _post_webhook(url: str, payload: dict[str, Any], timeout: float = 5.0) -> bool:
    """Send one alert on the configured channel. Never raises.

    The channel decides the payload shape (Telegram, Slack, Discord, ntfy each
    reject the others' format), so delivery lives in :mod:`notifier`. ``url`` is
    kept in the signature because tests inject a sender that takes it.
    """
    try:
        import notifier

        return notifier.send(payload, timeout=timeout)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Alert delivery failed: %s", exc)
        return False


def _undelivered_reason() -> str:
    """Why the last alert did not arrive, as one short clause.

    The sender returns a bare False for three different situations - the master
    switch is off, the channel has no token/URL, or the service rejected the
    message - and they need different fixes. Naming the cause here means the log
    line is actionable instead of a generic "not delivered".
    """
    try:
        import notifier

        if not notifier.enabled():
            return "alerting is switched off"
        if not notifier.channel_ready():
            return f"ALERT_CHANNEL={settings.alert_channel} is not configured"
        return "the channel rejected the message; see the line above"
    except Exception:  # pragma: no cover - defensive
        return "delivery failed"


class Watchdog:
    """Periodically checks trading health and alerts on sustained problems."""

    def __init__(
        self,
        *,
        webhook_url: Optional[str] = None,
        stale_tick_seconds: float = STALE_TICK_SECONDS,
        cooldown_seconds: float = ALERT_COOLDOWN_SECONDS,
        sender: Optional[Callable[[str, dict[str, Any]], bool]] = None,
    ) -> None:
        self.webhook_url = webhook_url if webhook_url is not None else settings.alert_webhook_url
        self.stale_tick_seconds = stale_tick_seconds
        self.cooldown_seconds = cooldown_seconds
        self._sender = sender or _post_webhook
        self.state = WatchdogState()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- checks ----------------------------------------------------------
    def check(
        self,
        *,
        market_open: bool,
        feed: dict[str, Any],
        engine: dict[str, Any],
        kite: Optional[dict[str, Any]] = None,
        heartbeat: Optional[dict[str, Any]] = None,
    ) -> list[Alert]:
        """Return the alerts that should fire right now.

        Pure with respect to the bot: it reads status dicts and decides. That
        keeps it testable without a running feed or a real webhook.
        """
        alerts: list[Alert] = []
        heartbeat = heartbeat or {}

        # Context every alert shares, so a message says which book and which
        # instrument it is about without the operator opening the dashboard.
        # `mode` is the venue the bot is exposed to (LIVE/PAPER), which is the
        # first thing anyone asks about an alert.
        common: dict[str, Any] = {}
        if engine.get("mode"):
            common["venue"] = engine["mode"]
        if engine.get("execution_symbol"):
            common["symbol"] = engine["execution_symbol"]
        if feed.get("interval"):
            common["interval"] = feed["interval"]

        def _alert(kind: str, detail: str, severity: str, **fields: Any) -> Alert:
            merged = dict(common)
            merged.update({k: v for k, v in fields.items() if v not in (None, "")})
            return Alert(kind, detail, severity=severity, fields=merged)

        if market_open:
            age = feed.get("tick_age_seconds")
            if age is not None and age > self.stale_tick_seconds:
                alerts.append(
                    _alert(
                        "STALE_FEED",
                        f"The signal series has not refreshed for {age:.0f}s "
                        f"(limit {self.stale_tick_seconds:.0f}s). "
                        f"Signals cannot form on a series that is not updating.",
                        severity="critical",
                        source=feed.get("symbol") or "NIFTY 50",
                        last_tick=feed.get("last_tick_at"),
                        last_price=feed.get("last_price"),
                        action_required="Check the Kite token and network; restart the bot if it does not recover.",
                    )
                )
            if feed.get("error"):
                alerts.append(
                    _alert(
                        "FEED_DOWN",
                        f"The signal series is failing: {feed.get('error')}. "
                        f"No signal can be generated until it recovers.",
                        severity="critical",
                        source=feed.get("symbol") or "NIFTY 50",
                        error=feed.get("error"),
                        action_required="Check the Kite access token and network; the bot cannot trade without market data.",
                    )
                )

        # The mark loop is the only thing that marks positions to market and
        # samples equity. A dead loop is invisible from outside: the API still
        # answers, the feed still ticks, and the dashboard shows a frozen P&L
        # that reads exactly like a quiet market. Checked in-session only,
        # because out of hours the loop still runs but nothing is moving.
        if market_open:
            age = heartbeat.get("age_seconds")
            interval = heartbeat.get("interval_seconds") or 5.0
            limit = float(interval) * STALE_LOOP_INTERVALS
            if age is not None and age > limit:
                alerts.append(
                    _alert(
                        "LOOP_STALLED",
                        f"The mark loop has not run for {age:.0f}s "
                        f"(limit {limit:.0f}s at {float(interval):.0f}s intervals). "
                        f"Positions are not being marked and equity is frozen.",
                        severity="critical",
                        loop_interval=f"{float(interval):.0f}s",
                        last_run=f"{age:.0f}s ago",
                        action_required="Restart the bot; open positions are unmanaged until the loop returns.",
                    )
                )
            if heartbeat.get("last_error"):
                alerts.append(
                    _alert(
                        "LOOP_ERROR",
                        f"The mark loop reported an error: {heartbeat['last_error']}",
                        severity="warning",
                        error=heartbeat["last_error"],
                    )
                )

        breach = engine.get("risk_breach")
        if breach:
            # Informational: a stop closes one trade and the strategy keeps
            # running, so this is not a "the bot stopped" alert.
            alerts.append(
                _alert(
                    "STOP_OUT",
                    breach.get("detail") or "a position hit its stop-loss",
                    severity="warning",
                    strategy=breach.get("strategy_id"),
                    symbol=breach.get("symbol"),
                    pnl=breach.get("pnl"),
                )
            )

        if engine.get("paused"):
            alerts.append(
                _alert(
                    "PAUSED",
                    f"Trading is paused: {engine.get('paused_reason') or 'no reason given'}. "
                    f"No new entry will be taken until it is resumed.",
                    severity="warning",
                    reason=engine.get("paused_reason") or "no reason given",
                    action_required="Resume from the dashboard once the cause is understood.",
                )
            )

        error_text = str(engine.get("last_error") or "")
        # An unconfirmed exit is reported by its own critical alert below. It
        # also lands in last_error, so without this exclusion one event pages
        # twice under two different kinds - and the warning would arrive beside
        # the critical, which reads as two separate problems.
        if error_text and "stop-out unconfirmed" not in error_text:
            alerts.append(
                _alert("ENGINE_ERROR", error_text, severity="warning", error=error_text)
            )

        # -- the four that cost money if missed ---------------------------
        # An order that was rejected means the bot decided to trade and could
        # not. Nothing else surfaces this: the strategy looks healthy, the feed
        # is fine, and the book simply stays flat through a signal.
        last = engine.get("last_result") or {}
        if isinstance(last, dict) and last.get("ok") is False:
            reason = str(last.get("reason") or "no reason given")
            alerts.append(
                _alert(
                    "ORDER_REJECTED",
                    f"An order was rejected: {last.get('action') or 'order'} "
                    f"{last.get('symbol') or ''} failed - {reason}. "
                    f"The signal was not taken.",
                    severity="critical",
                    strategy=last.get("strategy_id"),
                    symbol=last.get("symbol"),
                    action=last.get("action"),
                    quantity=last.get("quantity"),
                    error=reason,
                    action_required="Check margin, the instrument token, and the Kite session.",
                )
            )

        # An exit that could not be confirmed leaves a position open that the
        # bot believes it closed. This is the most dangerous state the bot can
        # be in, so it is critical rather than a warning.
        if "stop-out unconfirmed" in error_text:
            alerts.append(
                _alert(
                    "EXIT_UNCONFIRMED",
                    f"A stop-out could not be confirmed and the position is still "
                    f"open: {error_text}",
                    severity="critical",
                    error=error_text,
                    action_required="Square the position off by hand; the bot believes it is flat.",
                )
            )

        # Kite's access token dies around 06:00 IST. Without a fresh one the bot
        # runs on cached candles and cannot trade, so this is worth a nudge
        # before the open rather than a discovery at 09:20.
        kite = kite or {}
        if kite and kite.get("authenticated") is False:
            alerts.append(
                _alert(
                    "TOKEN_EXPIRED",
                    f"The Kite session is not authenticated: "
                    f"{kite.get('detail') or 're-login required'}. "
                    f"The bot cannot place orders until it is.",
                    severity="critical",
                    error=kite.get("detail") or "re-login required",
                    action_required="Re-authenticate Kite; the token expires daily at midnight IST.",
                )
            )

        # Authenticated is not the same as connected. A valid token with no SDK,
        # or with a quote that has stopped arriving, leaves the bot unable to
        # price anything while every other check passes - so the connection is
        # checked separately from the session.
        if kite and kite.get("authenticated"):
            if kite.get("sdk_available") is False:
                alerts.append(
                    _alert(
                        "KITE_SDK_MISSING",
                        "Kite credentials are present but the SDK is not importable, "
                        "so no quote or order can be sent.",
                        severity="critical",
                        error="kiteconnect is not importable",
                        action_required="Reinstall the runtime dependencies.",
                    )
                )
            if market_open:
                quote_age = kite.get("quote_age_seconds")
                if quote_age is not None and quote_age > self.stale_tick_seconds:
                    alerts.append(
                        _alert(
                            "KITE_QUOTE_STALE",
                            f"Kite is authenticated but no quote has arrived for "
                            f"{quote_age:.0f}s (limit {self.stale_tick_seconds:.0f}s). "
                            f"Orders cannot be priced.",
                            severity="critical",
                            last_quote=f"{quote_age:.0f}s ago",
                            action_required="Check the Kite session; a stale quote blocks every order.",
                        )
                    )

        return alerts

    # -- dispatch --------------------------------------------------------
    def _should_send(self, alert: Alert, now: float) -> bool:
        last = self.state._cooldowns.get(alert.kind)
        return last is None or (now - last) >= self.cooldown_seconds

    def dispatch(self, alerts: list[Alert], now: Optional[float] = None) -> int:
        """Send the alerts that are past their cooldown. Returns how many sent."""
        moment = now if now is not None else time.time()
        sent = 0
        for alert in alerts:
            if not self._should_send(alert, moment):
                continue
            payload = alert.to_dict()
            payload["source"] = "belaku"
            payload["at"] = moment
            # The rendered timestamp, in IST. `at` stays as the epoch float for
            # any machine consumer; the message needs the readable form.
            try:
                import database as db

                payload["at_ist"] = db.iso()
            except Exception:  # pragma: no cover - defensive
                payload["at_ist"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(moment))
            log.warning("ALERT %s: %s", alert.kind, alert.detail)
            # Delivery is decided by the sender, never by a gate here. The gate
            # used to be `notifier.configured()`, which answers "would a message
            # go out right now" - so while alerting was switched off, or the
            # channel was only partly configured, an *injected* sender was never
            # called at all. That made `dispatch` untestable with a stub and,
            # worse, made the watchdog report a live channel as broken: the
            # default sender already refuses when the channel cannot deliver and
            # returns False, so the gate only subtracted information. Calling it
            # unconditionally keeps one place deciding, and the counters below
            # still separate "fired" from "delivered".
            delivered = False
            # Guarded even though the default sender swallows its own errors:
            # dispatch runs on the watchdog thread, and an exception here would
            # kill the loop and silently stop all future alerting - the worst
            # possible failure for a watchdog.
            try:
                delivered = bool(self._sender(self.webhook_url, payload))
            except Exception:
                log.exception("Alert sender raised for %s", alert.kind)
            if not delivered:
                log.warning(
                    "ALERT %s not delivered (%s)",
                    alert.kind,
                    _undelivered_reason(),
                )
            with self._lock:
                # The cooldown is set either way. A message that could not be
                # delivered must not be retried every 30s: the channel is down,
                # not the alert, and retrying floods the log with the same line.
                self.state._cooldowns[alert.kind] = moment
                self.state.last_alert_at = moment
                self.state.last_alert = payload
                # Two counters, because they answer different questions.
                # `alerts_sent` is how often the watchdog fired; `alerts_delivered`
                # is how often the channel accepted it. Reporting only the first
                # made a broken channel look healthy - the dashboard said "12
                # alerts sent" while the phone stayed silent.
                self.state.alerts_sent += 1
                if delivered:
                    self.state.alerts_delivered += 1
            sent += 1 if delivered else 0
        return sent

    def run_once(
        self,
        *,
        market_open: bool,
        feed: dict[str, Any],
        engine: dict[str, Any],
        kite: Optional[dict[str, Any]] = None,
        heartbeat: Optional[dict[str, Any]] = None,
    ) -> list[Alert]:
        """One full cycle: check, then dispatch. Returns the alerts that fired."""
        alerts = self.check(
            market_open=market_open,
            feed=feed,
            engine=engine,
            kite=kite,
            heartbeat=heartbeat,
        )
        with self._lock:
            self.state.checks += 1
            self.state.last_check_at = time.time()
            self.state.healthy = not alerts
            self.state.consecutive_failures = (
                self.state.consecutive_failures + 1 if alerts else 0
            )
        # Only alert once a problem has persisted, so a single slow poll or a
        # momentary reconnect does not page anyone.
        if self.state.consecutive_failures >= FAILURES_BEFORE_ALERT:
            self.dispatch(alerts)
        return alerts

    # -- loop ------------------------------------------------------------
    def loop(self, stop: threading.Event, interval: float = 30.0) -> None:
        import database as db
        from engine import get_engine
        from kite_client import market_status

        log.info("Watchdog started (every %.0fs)", interval)
        while not stop.wait(interval):
            try:
                import bot as bot_module

                # The full status payload, so the checks see the same figures the
                # dashboard does - Kite auth lives beside `engine`, not inside it.
                status = bot_module.api_status()
                engine_status = status.get("engine") or {}
                self.run_once(
                    market_open=bool(market_status().get("is_open")),
                    feed=engine_status.get("feed") or {},
                    engine=engine_status,
                    kite=status.get("kite") or {},
                    # The mark loop's own heartbeat. It lives beside `engine` in
                    # the payload, not inside it, because it describes the
                    # process rather than a strategy.
                    heartbeat=(status.get("strategy") or {}).get("heartbeat") or {},
                )
            except Exception:  # pragma: no cover - defensive
                log.exception("Watchdog check failed")
        log.info("Watchdog stopped")

    def start(self, interval: float = 30.0) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.loop, args=(self._stop, interval), name="watchdog", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


_watchdog: Optional[Watchdog] = None
_watchdog_lock = threading.Lock()


def get_watchdog() -> Watchdog:
    global _watchdog
    with _watchdog_lock:
        if _watchdog is None:
            _watchdog = Watchdog()
        return _watchdog
