"""Channel selection for the inbound command poller (``cmd_commands``).

Telegram and ntfy share the same downstream engine (``tracker.commands``,
already covered by ``test_commands.py``); what's specific to the CLI layer is
picking which one to poll and enforcing the shared secret, so that's all this
file checks -- no real HTTP, no real routes.yaml.
"""

from __future__ import annotations

import argparse
import json

import pytest

from tracker.cli import _command_source, cmd_commands
from tracker.commands import Cursor


def _args(tmp_path, **overrides):
    defaults = {
        "config": str(tmp_path / "routes.yaml"),
        "cursor": str(tmp_path / "cursor.json"),
        "prices": str(tmp_path / "prices.csv"),
        "state": str(tmp_path / "alerts.json"),
        "message": None,
        "message_id": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------- _command_source


def test_no_channel_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)

    channel, poll, secret = _command_source()

    assert channel is None
    assert poll is None


def test_telegram_wins_when_both_are_configured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("NTFY_COMMAND_TOPIC", "cmd-topic")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")

    channel, poll, secret = _command_source()

    assert channel == "telegram"
    assert secret == "s3cret"


def test_ntfy_used_when_telegram_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("NTFY_COMMAND_TOPIC", "cmd-topic")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")

    channel, poll, secret = _command_source()

    assert channel == "ntfy"


def test_command_secret_falls_back_to_ntfy_command_secret(monkeypatch):
    monkeypatch.delenv("COMMAND_SECRET", raising=False)
    monkeypatch.setenv("NTFY_COMMAND_SECRET", "old-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    _, _, secret = _command_source()

    assert secret == "old-secret"


def test_command_secret_prefers_the_new_name(monkeypatch):
    monkeypatch.setenv("COMMAND_SECRET", "new-secret")
    monkeypatch.setenv("NTFY_COMMAND_SECRET", "old-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    _, _, secret = _command_source()

    assert secret == "new-secret"


def test_telegram_poll_uses_the_cursors_last_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)

    seen = {}

    def fake_poll_updates(token, *, since=""):
        seen["token"] = token
        seen["since"] = since
        return []

    monkeypatch.setattr("tracker.notify.telegram.poll_updates", fake_poll_updates)

    _, poll, _ = _command_source()
    cursor = Cursor.__new__(Cursor)
    cursor.last_id = "99"
    poll(cursor)

    assert seen == {"token": "tok", "since": "99"}


# ---------------------------------------------------------------- cmd_commands


def test_cmd_commands_without_any_channel_is_a_noop(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)

    assert cmd_commands(_args(tmp_path)) == 0
    assert "沒有設定指令頻道" in capsys.readouterr().err


def test_cmd_commands_refuses_without_a_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.delenv("COMMAND_SECRET", raising=False)
    monkeypatch.delenv("NTFY_COMMAND_SECRET", raising=False)

    assert cmd_commands(_args(tmp_path)) == 2
    assert "COMMAND_SECRET" in capsys.readouterr().err


def test_cmd_commands_applies_a_telegram_command_and_replies_through_every_channel(
    tmp_path, monkeypatch, capsys
):
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)

    monkeypatch.setattr(
        "tracker.notify.telegram.poll_updates",
        lambda token, since="": [{"id": "1", "message": "s3cret /price 24000"}],
    )

    sent = []
    monkeypatch.setattr(
        "tracker.notify.telegram.TelegramNotifier.send",
        lambda self, subject, body: sent.append((subject, body)),
    )

    rc = cmd_commands(_args(tmp_path, config=str(config_path)))

    assert rc == 0
    assert "alert_below: 24000" in config_path.read_text(encoding="utf-8")
    assert len(sent) == 1
    assert "改為 24,000" in sent[0][1]

    cursor_state = json.loads((tmp_path / "cursor.json").read_text(encoding="utf-8"))
    assert cursor_state == {"last_id": "1"}


def _configure_telegram(monkeypatch, message: str, *, sent: list):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")
    monkeypatch.delenv("NTFY_COMMAND_TOPIC", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)
    monkeypatch.setattr(
        "tracker.notify.telegram.poll_updates",
        lambda token, since="": [{"id": "1", "message": message}],
    )
    monkeypatch.setattr(
        "tracker.notify.telegram.TelegramNotifier.send",
        lambda self, subject, body: sent.append((subject, body)),
    )


def test_cmd_commands_status_combines_config_and_latest_prices(tmp_path, monkeypatch):
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")

    prices_path = tmp_path / "prices.csv"
    prices_path.write_text(
        "fetched_at,route_name,group,itinerary,origin,destination,depart,ret,price,currency,"
        "airlines,stops,duration_minutes,source,url\n"
        "2026-09-17T01:00:00+00:00,台北-布里斯本,,TPE>BNE>TPE,TPE,BNE,2027-06-05,2027-06-16,"
        "18928,TWD,China Airlines,0,600,fast_flights,https://example.invalid\n",
        encoding="utf-8",
    )

    sent = []
    _configure_telegram(monkeypatch, "s3cret /status", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path), prices=str(prices_path)))

    assert rc == 0
    assert len(sent) == 1
    body = sent[0][1]
    assert "台北-布里斯本" in body
    assert "最新查到" in body
    assert "18,928" in body


def test_cmd_commands_status_without_price_history(tmp_path, monkeypatch):
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")

    sent = []
    _configure_telegram(monkeypatch, "s3cret /status", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path)))

    assert rc == 0
    assert "還沒有任何查價紀錄" in sent[0][1]


def test_cmd_commands_reset_clears_alert_state(tmp_path, monkeypatch):
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")

    state_path = tmp_path / "alerts.json"
    state_path.write_text(
        '{"TPE>BNE>TPE|2027-06-05|2027-06-16|TWD": {"sent_at": "2026-09-17T01:00:00+00:00", '
        '"price": 18928, "currency": "TWD"}}',
        encoding="utf-8",
    )

    sent = []
    _configure_telegram(monkeypatch, "s3cret /reset", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path), state=str(state_path)))

    assert rc == 0
    assert "已清空 1 筆已通知紀錄" in sent[0][1]
    assert json.loads(state_path.read_text(encoding="utf-8")) == {}


# ---------------------------------------------------------------- --message (webhook relay)


def test_message_flag_applies_without_polling(tmp_path, monkeypatch):
    """The Cloudflare Worker relay's whole reason to exist: no getUpdates call.

    Once a Telegram webhook is registered, getUpdates is rejected outright, so
    a run driven by --message must never touch poll_updates at all.
    """
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")

    monkeypatch.setenv("COMMAND_SECRET", "s3cret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)

    def poll_should_never_be_called(token, since=""):
        raise AssertionError("--message must not poll Telegram")

    monkeypatch.setattr("tracker.notify.telegram.poll_updates", poll_should_never_be_called)

    sent = []
    monkeypatch.setattr(
        "tracker.notify.telegram.TelegramNotifier.send",
        lambda self, subject, body: sent.append((subject, body)),
    )

    rc = cmd_commands(
        _args(
            tmp_path,
            config=str(config_path),
            message="s3cret /price 24000",
            message_id="42",
        )
    )

    assert rc == 0
    assert "alert_below: 24000" in config_path.read_text(encoding="utf-8")
    assert len(sent) == 1


def test_message_flag_leaves_the_poll_cursor_untouched(tmp_path, monkeypatch):
    """No polling happened, so there is nothing for a cursor to track."""
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")

    monkeypatch.setenv("COMMAND_SECRET", "s3cret")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    cmd_commands(_args(tmp_path, config=str(config_path), message="s3cret /price 24000"))

    assert not (tmp_path / "cursor.json").exists()


def test_message_flag_refuses_without_a_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("COMMAND_SECRET", raising=False)
    monkeypatch.delenv("NTFY_COMMAND_SECRET", raising=False)

    rc = cmd_commands(_args(tmp_path, message="whatever /price 24000"))

    assert rc == 2
    assert "COMMAND_SECRET" in capsys.readouterr().err


def test_message_flag_silently_ignores_a_message_without_the_secret(tmp_path, monkeypatch, capsys):
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-布里斯本
    from: TPE
    to: BNE
    trip: round
    windows:
      - depart: 2027-06-05
        nights: 11
    alert_below: 30000
"""
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(routes_yaml, encoding="utf-8")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")

    rc = cmd_commands(_args(tmp_path, config=str(config_path), message="just chatting, no secret here"))

    assert rc == 0
    assert "alert_below: 24000" not in config_path.read_text(encoding="utf-8")
    assert "沒有新指令" in capsys.readouterr().err


def test_cmd_commands_reports_a_poll_failure_without_raising(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")

    def boom(token, since=""):
        raise RuntimeError("network is down")

    monkeypatch.setattr("tracker.notify.telegram.poll_updates", boom)

    assert cmd_commands(_args(tmp_path)) == 1
    assert "network is down" in capsys.readouterr().err
