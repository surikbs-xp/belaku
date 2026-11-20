#!/usr/bin/env python3
"""Resolve and write ``ALERT_TELEGRAM_CHAT_ID`` for the Belaku alert bot.

Telegram addresses a conversation by *chat id*, not by the bot's name. The bot
cannot message itself, so the id that matters is the one belonging to the chat
that messaged the bot - your own user id for a direct message, or a negative id
for a group or channel. Filling in the bot's own username, or the bot's own user
id, produces a channel that looks configured and delivers nothing:

* ``ALERT_TELEGRAM_CHAT_ID=belaku_bot`` -> ``400 Bad Request: chat not found``
* ``ALERT_TELEGRAM_CHAT_ID=<the bot's own id>`` -> ``403 Forbidden: the bot
  can't send messages to the bot``

This script closes that loop. It reads the token from ``bot/.env``, asks Telegram
which chats have talked to the bot, writes the answer back into ``bot/.env``, and
optionally sends a test message so the operator sees it arrive.

Telegram only records a chat after it has received a message from it, so the one
manual step is unavoidable: send the bot any message first.

Usage:
    scripts/telegram-setup.py              # resolve and write the chat id
    scripts/telegram-setup.py --list       # show what Telegram has seen
    scripts/telegram-setup.py --test       # also send a test message
    scripts/telegram-setup.py --chat -1001234567890   # set it explicitly

Nothing is printed that reveals the token.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / "bot" / ".env"

TOKEN_KEY = "ALERT_TELEGRAM_BOT_TOKEN"
CHAT_KEY = "ALERT_TELEGRAM_CHAT_ID"


def read_env(path: Path) -> dict[str, str]:
    """Parse the ``KEY=value`` pairs this script cares about.

    Deliberately not python-dotenv: this runs before the bot's dependencies are
    guaranteed to be installed, and it only needs two keys.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env_value(path: Path, key: str, value: str) -> bool:
    """Replace ``key`` in the file in place, or append it. True when changed.

    Edited line by line so every comment and every other setting survives - a
    regenerated .env would drop the operator's own notes.
    """
    text = path.read_text()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        updated = pattern.sub(f"{key}={value}", text, count=1)
    else:
        updated = text.rstrip("\n") + f"\n{key}={value}\n"
    if updated == text:
        return False
    path.write_text(updated)
    return True


def api(token: str, method: str, params: Optional[dict[str, Any]] = None) -> tuple[bool, Any]:
    """One Bot API call. Returns ``(ok, payload_or_description)``, never raises."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = None
    headers = {}
    if params is not None:
        data = json.dumps(params).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return True, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.load(exc)
            return False, body.get("description") or body
        except Exception:
            return False, f"HTTP {exc.code} {exc.reason}"
    except Exception as exc:
        return False, str(exc)


def describe_chat(chat: dict[str, Any]) -> str:
    """A readable label for a chat, for the picker output."""
    kind = chat.get("type") or "?"
    label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    handle = f" @{chat['username']}" if chat.get("username") and chat.get("title") else ""
    return f"{chat.get('id')}  {kind:<9} {label}{handle}".rstrip()


def find_chats(token: str) -> list[dict[str, Any]]:
    """Every distinct chat Telegram has seen message the bot, most recent first.

    ``getUpdates`` only returns updates that have not been confirmed, and a
    webhook (if one were set) would swallow them entirely - both are reported by
    the caller rather than silently producing an empty list.
    """
    ok, payload = api(token, "getUpdates", {"limit": 100, "timeout": 0})
    if not ok:
        raise SystemExit(f"getUpdates failed: {payload}")

    seen: dict[Any, dict[str, Any]] = {}
    for update in payload.get("result", []):
        message = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("edited_channel_post")
            or (update.get("my_chat_member") or {}).get("chat")
        )
        # `my_chat_member` carries the chat dict directly, the message keys carry
        # an envelope with `chat` inside it.
        chat = message.get("chat") if isinstance(message, dict) and "chat" in message else message
        if isinstance(chat, dict) and chat.get("id") is not None:
            seen[chat["id"]] = chat
    return list(seen.values())


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", type=Path, default=ENV_PATH, help="path to bot/.env")
    parser.add_argument("--chat", help="write this chat id directly, skipping discovery")
    parser.add_argument("--list", action="store_true", help="only show the chats Telegram has seen")
    parser.add_argument("--test", action="store_true", help="send a test message after writing")
    args = parser.parse_args(argv)

    env = read_env(args.env)
    token = env.get(TOKEN_KEY, "").strip()
    if not token:
        print(f"{TOKEN_KEY} is empty in {args.env}", file=sys.stderr)
        return 2

    ok, me = api(token, "getMe")
    if not ok:
        print(f"The token was rejected by Telegram: {me}", file=sys.stderr)
        return 2
    bot = me.get("result", {})
    bot_id, bot_name = bot.get("id"), bot.get("username")
    print(f"Bot: {bot.get('first_name')} @{bot_name} (id {bot_id})")

    if args.list:
        chats = find_chats(token)
        if not chats:
            print("No chats yet. Open Telegram and send this bot any message, then re-run.")
            return 1
        print("\nChats that have messaged the bot:")
        for chat in chats:
            print(f"  {describe_chat(chat)}")
        return 0

    if args.chat:
        chat_id = args.chat.strip()
    else:
        chats = find_chats(token)
        if not chats:
            print(
                "\nNo chats found. Telegram only records a chat after it has received a\n"
                f"message from it, so:\n\n"
                f"  1. Open Telegram and search for @{bot_name}\n"
                "  2. Send it any message (\"hi\" is fine)\n"
                "  3. Re-run this script\n",
                file=sys.stderr,
            )
            return 1
        if len(chats) > 1:
            print("\nSeveral chats have messaged the bot; pick one with --chat <id>:")
            for chat in chats:
                print(f"  {describe_chat(chat)}")
            return 1
        chat_id = str(chats[0]["id"])
        print(f"Found chat: {describe_chat(chats[0])}")

    # The two mistakes that produce a non-working channel, caught before writing.
    if bot_id is not None and str(chat_id).lstrip("-").strip() == str(bot_id):
        print(
            f"\n{chat_id} is this bot's own id. A bot cannot message itself - use the id of\n"
            "the chat that messaged it (or pass --chat explicitly).",
            file=sys.stderr,
        )
        return 2
    if not str(chat_id).lstrip("-").isdigit() and not str(chat_id).startswith("@"):
        print(
            f"\n{chat_id!r} is not a chat id. It must be a numeric id, or @channelname.",
            file=sys.stderr,
        )
        return 2

    changed = write_env_value(args.env, CHAT_KEY, chat_id)
    print(f"{CHAT_KEY}={chat_id} {'written to' if changed else 'already set in'} {args.env}")

    if args.test:
        ok, payload = api(
            token,
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Belaku: this is a test alert. If you can read this, alerting works.",
                "disable_web_page_preview": True,
            },
        )
        print("Test message sent" if ok else f"Test message failed: {payload}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
