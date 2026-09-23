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

from tracker.cli import _command_source, cmd_chart, cmd_commands, cmd_digest
from tracker.commands import Cursor
from tracker.notify import NotifyError
from tracker.store import PriceStore

from conftest import make_quote


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


def test_cmd_commands_report_includes_dates_airline_and_link(tmp_path, monkeypatch):
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
        "18928,TWD,China Airlines,0,600,fast_flights,https://example.invalid/flight\n",
        encoding="utf-8",
    )

    sent = []
    _configure_telegram(monkeypatch, "s3cret /report", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path), prices=str(prices_path)))

    assert rc == 0
    body = sent[0][1]
    assert "[台北-布里斯本]" in body
    assert "18,928" in body
    assert "2027-06-05~2027-06-16" in body, "shows both outbound and return dates"
    assert "China Airlines" in body
    assert "https://example.invalid/flight" in body


def test_cmd_commands_report_separates_routes_and_caps_each_at_three(tmp_path, monkeypatch):
    """Each route gets its own [名稱] section with at most its 3 cheapest itineraries."""
    routes_yaml = """\
currency: TWD
max_queries: 60
defaults:
  adults: 1
  seat: economy
  max_stops: 0
routes:
  - name: 台北-雪梨
    from: TPE
    to: SYD
    trip: round
    windows:
      - depart_range: [2027-06-01, 2027-06-05]
        nights: 11
    alert_below: 30000
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

    header = (
        "fetched_at,route_name,group,itinerary,origin,destination,depart,ret,price,currency,"
        "airlines,stops,duration_minutes,source,url\n"
    )
    # 台北-雪梨: four distinct itineraries (different depart dates), so the
    # 4th-cheapest must be left out once capped at 3.
    syd_rows = "".join(
        f"2026-09-17T0{i}:00:00+00:00,台北-雪梨,,TPE>SYD>TPE,TPE,SYD,2027-06-0{i},2027-06-1{i},"
        f"{25000 + i * 1000},TWD,China Airlines,0,600,fast_flights,https://example.invalid/syd{i}\n"
        for i in range(1, 5)
    )
    bne_row = (
        "2026-09-17T01:00:00+00:00,台北-布里斯本,,TPE>BNE>TPE,TPE,BNE,2027-06-05,2027-06-16,"
        "18928,TWD,EVA Air,0,600,fast_flights,https://example.invalid/bne\n"
    )
    prices_path = tmp_path / "prices.csv"
    prices_path.write_text(header + syd_rows + bne_row, encoding="utf-8")

    sent = []
    _configure_telegram(monkeypatch, "s3cret /report", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path), prices=str(prices_path)))

    assert rc == 0
    body = sent[0][1]
    assert "[台北-布里斯本]" in body
    assert "[台北-雪梨]" in body
    # cheapest route's section comes first
    assert body.index("[台北-布里斯本]") < body.index("[台北-雪梨]")
    # capped at 3 for 台北-雪梨 even though 4 itineraries exist
    syd_section = body[body.index("[台北-雪梨]") :]
    assert syd_section.count("TPE>SYD>TPE") == 3
    assert "syd4" not in syd_section, "the 4th-cheapest (most expensive) itinerary must be dropped"
    # 台北-布里斯本 only ever had one itinerary
    assert body.count("TPE>BNE>TPE") == 1


def test_cmd_commands_report_omits_a_missing_link(tmp_path, monkeypatch):
    """A quote with no url (e.g. from a provider that doesn't supply one) must not print a blank line."""
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
        "18928,TWD,China Airlines,0,600,fast_flights,\n",
        encoding="utf-8",
    )

    sent = []
    _configure_telegram(monkeypatch, "s3cret /report", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path), prices=str(prices_path)))

    assert rc == 0
    body = sent[0][1]
    assert "[台北-布里斯本]\n1. TPE>BNE>TPE" in body, "no blank line between the header and the price line"
    section = body[body.index("[台北-布里斯本]") :]
    assert not any(line.strip() == "" for line in section.splitlines()[1:]), "no blank line where the url would go"


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


# ---------------------------------------------------------------- /chart


CHART_ROUTES_YAML = """\
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


def _configure_ntfy(monkeypatch, message: str, *, sent: list):
    """A command channel with no Telegram configured -- /chart has nowhere to send a photo.

    Inbound polling (``NTFY_COMMAND_TOPIC``) and the outbound reply channel
    (``NTFY_TOPIC``) are separate settings; both need to be set for a reply to
    actually go anywhere, same as Telegram's one token covering both directions.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("NTFY_COMMAND_TOPIC", "cmd-topic")
    monkeypatch.setenv("COMMAND_SECRET", "s3cret")
    monkeypatch.setenv("NTFY_TOPIC", "alert-topic")
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)
    monkeypatch.setattr(
        "tracker.notify.ntfy.poll_messages",
        lambda topic, server=None, since="": [{"id": "1", "message": message}],
    )
    monkeypatch.setattr(
        "tracker.notify.ntfy.NtfyNotifier.send",
        lambda self, subject, body: sent.append((subject, body)),
    )


def test_cmd_commands_chart_without_telegram_configured_replies_with_a_note(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    sent = []
    _configure_ntfy(monkeypatch, "s3cret /chart", sent=sent)

    rc = cmd_commands(_args(tmp_path, config=str(config_path)))

    assert rc == 0
    assert "只能用 Telegram" in sent[0][1]


def test_cmd_commands_chart_renders_and_sends_a_photo(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    sent = []
    _configure_telegram(monkeypatch, "s3cret /chart", sent=sent)

    rendered = []
    photos = []
    monkeypatch.setattr(
        "tracker.chart.render_price_trend",
        lambda rows, *, route_name, out_path: rendered.append(route_name) or out_path,
    )
    monkeypatch.setattr(
        "tracker.notify.telegram.send_photo",
        lambda token, chat_id, path, *, caption="": photos.append((token, chat_id, caption)),
    )

    rc = cmd_commands(_args(tmp_path, config=str(config_path)))

    assert rc == 0
    assert rendered == ["台北-布里斯本"]
    assert photos == [("tok", "123", "台北-布里斯本 近 90 天價格趨勢")]
    assert sent == [], "a successful chart send has nothing left to say as text"


def test_cmd_commands_chart_reports_a_render_failure_as_text(tmp_path, monkeypatch):
    from tracker.chart import ChartError

    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    sent = []
    _configure_telegram(monkeypatch, "s3cret /chart", sent=sent)
    monkeypatch.setattr(
        "tracker.chart.render_price_trend",
        lambda rows, *, route_name, out_path: (_ for _ in ()).throw(
            ChartError(f"[{route_name}] 歷史紀錄不到兩筆，還畫不出趨勢圖")
        ),
    )

    rc = cmd_commands(_args(tmp_path, config=str(config_path)))

    assert rc == 0
    assert "歷史紀錄不到兩筆" in sent[0][1]


def test_cmd_commands_chart_reports_a_send_failure_as_text(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    sent = []
    _configure_telegram(monkeypatch, "s3cret /chart", sent=sent)
    monkeypatch.setattr(
        "tracker.chart.render_price_trend", lambda rows, *, route_name, out_path: out_path
    )
    monkeypatch.setattr(
        "tracker.notify.telegram.send_photo",
        lambda *a, **k: (_ for _ in ()).throw(NotifyError("Telegram 傳圖失敗: boom")),
    )

    rc = cmd_commands(_args(tmp_path, config=str(config_path)))

    assert rc == 0
    assert "傳圖失敗" in sent[0][1]


def test_cmd_chart_saves_a_png_from_real_price_history(tmp_path, now, days_ago):
    prices_path = tmp_path / "prices.csv"
    store = PriceStore(prices_path)
    store.append([make_quote(9000, route_name="台北-東京", fetched_at=days_ago(5))])
    store.append([make_quote(8500, route_name="台北-東京", fetched_at=now)])

    out_path = tmp_path / "out.png"
    args = argparse.Namespace(route="台北-東京", prices=str(prices_path), days=90, out=str(out_path))

    rc = cmd_chart(args)

    assert rc == 0
    assert out_path.exists()
    assert out_path.stat().st_size > 0


def test_cmd_chart_reports_missing_history_without_crashing(tmp_path, capsys):
    args = argparse.Namespace(
        route="沒有紀錄", prices=str(tmp_path / "prices.csv"), days=90, out=str(tmp_path / "out.png")
    )

    rc = cmd_chart(args)

    assert rc == 1
    assert "不到兩筆" in capsys.readouterr().err


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


# ---------------------------------------------------------------- digest (periodic survival summary)


def test_digest_reports_failure_when_no_channel_is_configured(tmp_path, monkeypatch, capsys):
    for key in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "NTFY_TOPIC",
        "LINE_CHANNEL_TOKEN",
        "LINE_USER_ID",
        "GITHUB_TOKEN",
        "GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(key, raising=False)
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    rc = cmd_digest(argparse.Namespace(config=str(config_path), prices=str(tmp_path / "prices.csv")))

    assert rc == 1
    assert "沒有設定任何通知管道" in capsys.readouterr().err


def test_digest_sends_route_settings_and_latest_prices(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    prices_path = tmp_path / "prices.csv"
    store = PriceStore(prices_path)
    store.append([make_quote(18928, route_name="台北-布里斯本", itinerary="TPE>BNE>TPE")])

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)
    sent = []
    monkeypatch.setattr(
        "tracker.notify.telegram.TelegramNotifier.send",
        lambda self, subject, body: sent.append((subject, body)),
    )

    rc = cmd_digest(argparse.Namespace(config=str(config_path), prices=str(prices_path)))

    assert rc == 0
    subject, body = sent[0]
    assert "存活摘要" in subject
    assert "[台北-布里斯本]" in body
    assert "18,928" in body


def test_digest_fails_when_every_channel_fails_to_send(tmp_path, monkeypatch):
    config_path = tmp_path / "routes.yaml"
    config_path.write_text(CHART_ROUTES_YAML, encoding="utf-8")

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    monkeypatch.delenv("LINE_CHANNEL_TOKEN", raising=False)

    def boom(self, subject, body):
        raise NotifyError("bot blocked")

    monkeypatch.setattr("tracker.notify.telegram.TelegramNotifier.send", boom)

    rc = cmd_digest(argparse.Namespace(config=str(config_path), prices=str(tmp_path / "prices.csv")))

    assert rc == 1
