"""Tests for ``scripts/telegram-setup.py``.

The script rewrites a real ``bot/.env``, so the part worth pinning is the file
edit and the two misconfigurations that made the channel look armed while every
alert failed: the bot's own username, and the bot's own numeric id.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SETUP_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "telegram-setup.py"


def _load():
    spec = importlib.util.spec_from_file_location("telegram_setup", SETUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


setup = _load()


def test_write_env_value_replaces_only_the_target_line(tmp_path) -> None:
    """The operator's own comments and every other setting must survive.

    Regenerating the file would silently drop notes and the Kite credentials, so
    the edit is line-scoped.
    """
    env = tmp_path / ".env"
    env.write_text(
        "# Alerting\n"
        "ALERT_ENABLED=0\n"
        "ALERT_TELEGRAM_BOT_TOKEN=TOK\n"
        "ALERT_TELEGRAM_CHAT_ID=belaku_bot\n"
        "KITE_API_KEY=keep-me\n"
    )

    assert setup.write_env_value(env, "ALERT_TELEGRAM_CHAT_ID", "42") is True

    text = env.read_text()
    assert "ALERT_TELEGRAM_CHAT_ID=42" in text
    assert "belaku_bot" not in text
    assert "KITE_API_KEY=keep-me" in text
    assert "# Alerting" in text
    # Idempotent: writing the same value again changes nothing.
    assert setup.write_env_value(env, "ALERT_TELEGRAM_CHAT_ID", "42") is False


def test_write_env_value_appends_a_missing_key(tmp_path) -> None:
    """An .env that predates the setting has to gain it, not silently no-op."""
    env = tmp_path / ".env"
    env.write_text("ALERT_ENABLED=0\n")

    assert setup.write_env_value(env, "ALERT_TELEGRAM_CHAT_ID", "-100123") is True
    assert "ALERT_TELEGRAM_CHAT_ID=-100123" in env.read_text()


def test_read_env_ignores_comments_and_blank_lines(tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "# ALERT_TELEGRAM_CHAT_ID=commented-out\n"
        "\n"
        "ALERT_TELEGRAM_CHAT_ID=42\n"
    )

    assert setup.read_env(env)["ALERT_TELEGRAM_CHAT_ID"] == "42"


def test_describe_chat_labels_a_direct_message_and_a_group() -> None:
    assert setup.describe_chat(
        {"id": 42, "type": "private", "first_name": "Suresh"}
    ).startswith("42")
    group = setup.describe_chat({"id": -100123, "type": "supergroup", "title": "Belaku"})
    assert "-100123" in group and "Belaku" in group and "supergroup" in group
