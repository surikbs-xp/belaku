"""Deliver alerts to a phone.

The watchdog decides *what* is wrong; this module decides *how to say it* to a
particular service. The split matters because each service rejects a payload it
does not recognise:

* Telegram wants ``{"chat_id": ..., "text": ...}`` on a bot-token URL;
* Slack wants ``{"text": ...}``;
* Discord wants ``{"content": ...}``;
* ntfy wants a plain-text body (or ``{"message": ...}``).

Sending one generic JSON shape to all of them - which is what this used to do -
fails silently at the service with a 400, and the operator believes they are
being alerted when they are not. So the payload is built per channel here, and
:func:`describe` renders the human-readable line once for every channel to share.

Nothing in this module raises. An alerting path that can throw is worse than no
alerting: it takes down the watchdog loop and every future alert with it.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from config import settings

log = logging.getLogger("notifier")

# Telegram's sendMessage endpoint, with the token substituted per call.
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# Severity to a symbol, so a glance at the notification says how urgent it is.
SEVERITY_MARK = {
    "critical": "\U0001F6A8",   # rotating light
    "warning": "\u26A0\uFE0F",  # warning sign
    "info": "\u2139\uFE0F",     # information
}


# The order fields are rendered in. Fixed rather than "whatever the payload
# happens to carry", so the same alert reads identically every time and a
# missing field is a visible gap instead of a silently shorter message.
FIELD_ORDER = (
    "strategy",
    "source",
    "interval",
    "venue",
    "symbol",
    "action",
    "quantity",
    "price",
    "order_id",
    "position_id",
    "pnl",
    "bar",
    "at",
    "error",
    "action_required",
)


def _format_at(value: Any) -> str:
    """Render a timestamp as ``YYYY-MM-DD HH:MM:SS IST``.

    The payloads carry ISO strings with a ``+05:30`` offset, which is correct but
    reads as noise on a phone. The offset is dropped only because every timestamp
    in this bot is IST; the label says so rather than leaving it implied.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    # 2026-11-20T11:58:00+05:30 -> 2026-11-20 11:58:00 IST
    if "T" in text:
        head, _, tail = text.partition("T")
        clock = tail[:8]
        if clock.count(":") == 2:
            return f"{head} {clock} IST"
    return text


def _format_value(value: Any) -> str:
    """Render one field value, shortening timestamps and trimming floats.

    Applied to every field rather than only the ones known to hold a timestamp,
    because the payloads carry bar times and tick times under several different
    keys and a raw ``+05:30`` offset on a phone is noise.
    """
    text = str(value).strip()
    if not text:
        return ""
    if "T" in text and ("+" in text or text.endswith("Z")):
        return _format_at(text)
    # 24180.500000000002 is a float artefact, not a price.
    try:
        number = float(text)
    except ValueError:
        return text
    if number == int(number) and "." not in text:
        return text
    return f"{number:.2f}"


def describe(payload: dict[str, Any]) -> str:
    """Render an alert as a structured, scannable block.

    The shape is deliberately fixed: a headline naming the event, one sentence
    saying what happened, then labelled fields. A prose blob was unreadable on a
    phone - "GIFT feed connected but no tick for 240s (limit 180s)" does not say
    which strategy is affected, on which instrument, or what to do about it, so
    the operator had to open the dashboard to find out whether it mattered.

    Plain text, not Markdown: Telegram rejects a message whose Markdown does not
    parse, and a symbol name or an error string can easily contain an unbalanced
    ``_`` or ``*``. Alignment is done with spaces for the same reason.
    """
    kind = str(payload.get("kind") or "ALERT")
    detail = str(payload.get("detail") or "").strip()
    severity = str(payload.get("severity") or "warning").lower()
    mark = SEVERITY_MARK.get(severity, SEVERITY_MARK["warning"])

    lines = [f"{mark} {kind}"]
    if detail:
        lines.append(detail)

    # Structured context. `fields` is the preferred source; the older flat keys
    # are still read so an alert raised by code that has not been updated yet
    # still carries its symbol and mode rather than losing them.
    fields: dict[str, str] = {}
    raw = payload.get("fields")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value is None or value == "":
                continue
            fields[str(key)] = _format_value(value)
    for legacy, key in (("mode", "venue"), ("symbol", "symbol"), ("at_ist", "at")):
        if key not in fields and payload.get(legacy):
            fields[key] = _format_value(payload[legacy])

    if fields:
        ordered = [k for k in FIELD_ORDER if k in fields]
        ordered += [k for k in fields if k not in FIELD_ORDER]
        width = max(len(k) for k in ordered)
        lines.append("")
        for key in ordered:
            lines.append(f"{key.ljust(width)}  {fields[key]}")
    return "\n".join(lines)


def _post_json(url: str, body: dict[str, Any], timeout: float = 8.0) -> bool:
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return 200 <= response.status < 300


def _post_text(url: str, text: str, timeout: float = 8.0) -> bool:
    request = urllib.request.Request(
        url,
        data=text.encode(),
        headers={"Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return 200 <= response.status < 300


# Chat ids already warned about, so a persistent misconfiguration logs once
# instead of on every alert. Bounded by the number of distinct bad values.
_invalid_chat_id_seen: set[str] = set()
_invalid_chat_id_lock = threading.Lock()


def _bot_own_id(token: Optional[str]) -> Optional[str]:
    """This bot's own user id, read from its token.

    A Telegram bot token is ``<bot_id>:<secret>``, so the id is available without
    a network call. Knowing it lets the validator reject the bot's own id, which
    Telegram always refuses with ``403 the bot can't send messages to the bot`` -
    another value that is non-empty, numeric, and therefore looked valid.
    """
    prefix, _, secret = (token or "").partition(":")
    if secret and prefix.isdigit():
        return prefix
    return None


def telegram_chat_id_valid(raw: Optional[str], token: Optional[str] = None) -> bool:
    """Whether ``raw`` is something Telegram will accept as a chat id.

    A chat id is the *numeric* id of the chat (negative for a group or channel),
    or ``@username`` for a public channel. It is never a bare bot username: the
    bot cannot message itself, and a bot token cannot post to another bot. The
    value ``belaku_bot`` therefore looked configured - both fields non-empty - so
    ``configured()`` answered yes, the dashboard showed "Armed", and every alert
    then failed with ``400 chat not found``. Validating here turns a silent,
    repeating failure into a single actionable warning and an honest status.

    ``@BotFather``-style usernames are also rejected when ``@`` is missing,
    because Telegram reads a bare word as a chat *id* and cannot resolve it.

    ``token`` is optional and defaults to the configured one. When supplied, the
    bot's own id - the ``<bot_id>`` prefix of the token - is rejected too: it is
    numeric and non-empty, so the shape check alone would call it valid while
    Telegram answers ``403``.
    """
    text = (raw or "").strip()
    if not text:
        return False
    if text.startswith("@"):
        # Public channel or supergroup username.
        name = text[1:]
        return 5 <= len(name) <= 32 and all(c.isalnum() or c == "_" for c in name)
    # Numeric chat id, negative for groups and channels.
    body = text[1:] if text.startswith("-") else text
    if not body or not body.isdigit():
        return False
    own_id = _bot_own_id(token if token is not None else settings.alert_telegram_bot_token)
    return own_id is None or body != own_id


def _warn_invalid_chat_id(chat_id: str) -> None:
    """Warn once about a chat id Telegram cannot resolve, not once per alert."""
    value = (chat_id or "").strip()
    with _invalid_chat_id_lock:
        if value in _invalid_chat_id_seen:
            return
        _invalid_chat_id_seen.add(value)
    log.error(
        "ALERT_TELEGRAM_CHAT_ID=%r is not a usable chat id. Telegram needs the numeric id "
        "of the chat that messaged the bot (a group or channel id is negative), or "
        "@channelname for a public channel. It must not be this bot's own username or "
        "its own id. Run scripts/telegram-setup.py --test. Alerts will not be delivered "
        "until it is fixed.",
        value,
    )


def _send_telegram(text: str, timeout: float = 8.0) -> bool:
    token = settings.alert_telegram_bot_token
    chat_id = settings.alert_telegram_chat_id
    if not token or not chat_id:
        log.warning(
            "ALERT_CHANNEL=telegram but ALERT_TELEGRAM_BOT_TOKEN / "
            "ALERT_TELEGRAM_CHAT_ID is not set; alert not sent"
        )
        return False
    if not telegram_chat_id_valid(chat_id):
        # Logged once per distinct bad value rather than per alert: the watchdog
        # and the lifecycle alerts would otherwise repeat this every 30s and bury
        # whatever else the log says.
        _warn_invalid_chat_id(chat_id)
        return False
    return _post_json(
        TELEGRAM_API.format(token=token),
        {
            "chat_id": chat_id,
            "text": text,
            # No parse_mode on purpose: see describe().
            "disable_web_page_preview": True,
        },
        timeout=timeout,
    )


# The runtime override, keyed in the settings table so a toggle survives a
# restart. Absent means "use ALERT_ENABLED from .env", which is why it is deleted
# rather than written as "0" when the operator wants the default back.
ENABLED_KEY = "alert_enabled"


def enabled() -> bool:
    """Whether alerts are switched on right now.

    The dashboard toggle wins over ``ALERT_ENABLED`` because it is the more
    recent, more specific statement of intent - an operator who just pressed
    "Enable" means it, whatever the file says. Stored in SQLite rather than
    rewritten into ``.env`` so a container restart does not silently revert it,
    and so the file stays the deployment's default rather than its live state.
    """
    try:
        import database as db

        stored = db.get_settings().get(ENABLED_KEY)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Could not read the alerting override: %s", exc)
        stored = None
    if stored is None:
        return bool(settings.alert_enabled)
    return str(stored).strip().lower() in {"1", "true", "yes", "on"}


def set_enabled(value: bool) -> bool:
    """Persist the toggle. Returns the state that is now in force."""
    import database as db

    db.set_settings({ENABLED_KEY: "1" if value else "0"})
    log.warning("Alerting %s by operator", "enabled" if value else "disabled")
    return enabled()


def clear_enabled() -> bool:
    """Drop the override so ``ALERT_ENABLED`` from .env applies again."""
    import database as db

    db.delete_settings([ENABLED_KEY])
    return enabled()


def send(payload: dict[str, Any], timeout: float = 8.0) -> bool:
    """Deliver one alert on the configured channel. Never raises.

    Nothing is sent unless ``ALERT_ENABLED`` is on. A configured channel is where
    a message *would* go, not permission to send one: with the setting off the
    bot is silent, which is what a development or soak run wants. Switch it on to
    check the channel end to end, then off again.

    Returns True when the service accepted it. A False return is logged with the
    reason, because "the alert did not arrive" is itself something the operator
    needs to be able to find out.
    """
    channel = (settings.alert_channel or "generic").strip().lower()
    url = (settings.alert_webhook_url or "").strip()
    text = describe(payload)

    if not enabled():
        log.debug("Alert %s not sent: alerting is off", payload.get("kind"))
        return False

    try:
        if channel == "telegram":
            return _send_telegram(text, timeout=timeout)

        if not url:
            # Logged rather than silent: an operator who set a channel but no URL
            # should find out here, not by noticing the absence of alerts.
            log.warning("ALERT_CHANNEL=%s but ALERT_WEBHOOK_URL is empty", channel)
            return False

        if channel == "slack":
            return _post_json(url, {"text": text}, timeout=timeout)
        if channel == "discord":
            return _post_json(url, {"content": text}, timeout=timeout)
        if channel == "ntfy":
            return _post_text(url, text, timeout=timeout)

        # generic: the raw payload, for an endpoint the operator controls.
        return _post_json(url, payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # The body carries the service's own explanation (a bad chat id, a
        # revoked token), which is the only way to debug a silent channel.
        try:
            body = exc.read().decode(errors="replace")[:300]
        except Exception:
            body = ""
        log.warning("Alert delivery failed (%s %s): %s %s", channel, exc.code, exc.reason, body)
        return False
    except Exception as exc:
        log.warning("Alert delivery failed (%s): %s", channel, exc)
        return False


def configured() -> bool:
    """True when alerting is switched on *and* the channel can deliver.

    Deliberately False while ``ALERT_ENABLED`` is off: every caller uses this to
    decide whether to bother, so one switch silences the whole bot instead of
    each site needing its own check.
    """
    if not enabled():
        return False
    return channel_ready()


def channel_ready() -> bool:
    """Whether the configured channel has everything it needs to deliver.

    Deliberately independent of the on/off switch. ``configured`` answers "would
    a message go out right now", which is False ``while`` alerting is switched
    off - correct for the send path, but it made the dashboard report "not
    configured" on a channel that was fully set up and merely switched off. The
    two questions are reported separately so the label can say which one is
    missing.

    A Telegram target is checked for *shape*, not only presence. A bot username
    in ``ALERT_TELEGRAM_CHAT_ID`` is non-empty but unusable, and reporting that as
    ready is what let a permanently failing channel look armed.
    """
    channel = (settings.alert_channel or "generic").strip().lower()
    if channel == "telegram":
        return bool(settings.alert_telegram_bot_token) and telegram_chat_id_valid(
            settings.alert_telegram_chat_id
        )
    return bool((settings.alert_webhook_url or "").strip())


def status() -> dict[str, Any]:
    """What the dashboard shows about alerting, without leaking the token."""
    channel = (settings.alert_channel or "generic").strip().lower()
    detail: dict[str, Any] = {
        "channel": channel,
        "enabled": enabled(),
        "configured": configured(),
        "channel_ready": channel_ready(),
    }
    if channel == "telegram":
        chat = settings.alert_telegram_chat_id
        # Show enough of the chat id to confirm the right one, never the token.
        detail["target"] = f"chat {chat[:4]}…{chat[-3:]}" if len(chat) > 8 else ("set" if chat else "unset")
        detail["token_set"] = bool(settings.alert_telegram_bot_token)
        # Says *why* the channel is not ready, so the dashboard can name the
        # problem instead of only reporting that something is missing.
        detail["chat_id_valid"] = telegram_chat_id_valid(chat)
        if chat and not detail["chat_id_valid"]:
            detail["hint"] = (
                "ALERT_TELEGRAM_CHAT_ID must be the numeric id of the chat that "
                "messaged the bot, or @channelname - not the bot's own username or id"
            )
            # Naming the specific mistake is the difference between a status line
            # and a fix.
            if chat.strip().lstrip("-").isdigit() and chat.strip().lstrip("-") == str(
                _bot_own_id(settings.alert_telegram_bot_token)
            ):
                detail["hint"] = (
                    "ALERT_TELEGRAM_CHAT_ID is this bot's own id; a bot cannot message "
                    "itself. Run scripts/telegram-setup.py --test"
                )
    else:
        url = (settings.alert_webhook_url or "").strip()
        if url:
            parsed = urllib.parse.urlparse(url)
            detail["target"] = f"{parsed.scheme}://{parsed.netloc}{parsed.path[:24]}"
        else:
            detail["target"] = "unset"
    return detail
