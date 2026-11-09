"""Operational watchdog: alert when the bot is up but not actually working.

The container healthcheck answers "is the process alive?". That is not the same
question. The failure modes that cost money are the ones where the process is
perfectly healthy and the *trading* is not:

* the GIFT feed is connected but no ticks have arrived for minutes (a silently
  dead websocket, or a token that expired mid-session);
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

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from config import settings

log = logging.getLogger("watchdog")

# A feed with no tick for this long is treated as stale. GIFT Nifty trades
# nearly continuously through the session, so a multi-minute gap during market
# hours means something is wrong, not that the market is quiet.
STALE_TICK_SECONDS = 180.0

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

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": self.detail, "severity": self.severity}


@dataclass
class WatchdogState:
    """Last observed health, surfaced on the dashboard."""

    last_check_at: Optional[float] = None
    last_alert_at: Optional[float] = None
    last_alert: Optional[dict[str, Any]] = None
    alerts_sent: int = 0
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
            "consecutive_failures": self.consecutive_failures,
            "healthy": self.healthy,
            "checks": self.checks,
        }


def _post_webhook(url: str, payload: dict[str, Any], timeout: float = 5.0) -> bool:
    """Send one alert. Never raises: a failed alert must not kill the bot."""
    try:
        import urllib.request

        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        log.warning("Alert webhook failed: %s", exc)
        return False


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
    def check(self, *, market_open: bool, feed: dict[str, Any], engine: dict[str, Any]) -> list[Alert]:
        """Return the alerts that should fire right now.

        Pure with respect to the bot: it reads status dicts and decides. That
        keeps it testable without a running feed or a real webhook.
        """
        alerts: list[Alert] = []

        if market_open:
            age = feed.get("tick_age_seconds")
            if feed.get("connected") and age is not None and age > self.stale_tick_seconds:
                alerts.append(
                    Alert(
                        "STALE_FEED",
                        f"GIFT feed connected but no tick for {age:.0f}s "
                        f"(limit {self.stale_tick_seconds:.0f}s)",
                        severity="critical",
                    )
                )
            if not feed.get("connected") and not feed.get("starting"):
                alerts.append(
                    Alert(
                        "FEED_DOWN",
                        f"GIFT feed is not connected: {feed.get('error') or 'no error reported'}",
                        severity="critical",
                    )
                )

        breach = engine.get("risk_breach")
        if breach:
            alerts.append(
                Alert("RISK_BREACH", breach.get("detail") or "risk limit breached", severity="critical")
            )

        if engine.get("paused"):
            alerts.append(
                Alert(
                    "PAUSED",
                    f"Strategy paused: {engine.get('paused_reason') or 'no reason given'}",
                    severity="warning",
                )
            )

        if engine.get("last_error"):
            alerts.append(
                Alert("ENGINE_ERROR", str(engine["last_error"]), severity="warning")
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
            log.warning("ALERT %s: %s", alert.kind, alert.detail)
            if self.webhook_url:
                # Guarded even though the default sender swallows its own
                # errors: dispatch runs on the watchdog thread, and an
                # exception here would kill the loop and silently stop all
                # future alerting - the worst possible failure for a watchdog.
                try:
                    self._sender(self.webhook_url, payload)
                except Exception:
                    log.exception("Alert sender raised for %s", alert.kind)
            with self._lock:
                self.state._cooldowns[alert.kind] = moment
                self.state.last_alert_at = moment
                self.state.last_alert = payload
                self.state.alerts_sent += 1
            sent += 1
        return sent

    def run_once(self, *, market_open: bool, feed: dict[str, Any], engine: dict[str, Any]) -> list[Alert]:
        """One full cycle: check, then dispatch. Returns the alerts that fired."""
        alerts = self.check(market_open=market_open, feed=feed, engine=engine)
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
        from gift_nifty import get_feed
        from kite_client import market_status

        log.info("Watchdog started (every %.0fs)", interval)
        while not stop.wait(interval):
            try:
                self.run_once(
                    market_open=bool(market_status().get("is_open")),
                    feed=get_feed().status(),
                    engine=get_engine().status(),
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
