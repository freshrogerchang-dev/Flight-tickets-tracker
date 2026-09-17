"""Channel selection, payload shape, and fan-out isolation. No network."""

from __future__ import annotations

import pytest

from tracker.alerts import Alert
from tracker.notify import available_notifiers, deliver, format_alerts
from tracker.notify.line import LineNotifier
from tracker.notify.ntfy import NtfyNotifier, _encode_header, poll_messages

from conftest import make_quote


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")


def FakeGet(body: str) -> FakeResponse:
    """A successful poll response carrying `body`."""
    return FakeResponse(200, body)


@pytest.fixture
def captured(monkeypatch):
    """Record outgoing HTTP calls instead of making them."""
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse(kwargs.pop("_status", 200))

    monkeypatch.setattr("requests.post", fake_post)
    return calls


# ---------------------------------------------------------------- selection


def test_no_secrets_means_no_channels():
    assert available_notifiers(env={}) == []


def test_ntfy_is_selected_by_topic_alone():
    notifiers = available_notifiers(env={"NTFY_TOPIC": "secret-topic"})

    assert [n.name for n in notifiers] == ["ntfy"]


def test_line_needs_both_token_and_user_id():
    assert available_notifiers(env={"LINE_CHANNEL_TOKEN": "t"}) == []
    assert available_notifiers(env={"LINE_USER_ID": "U1"}) == []
    assert [n.name for n in available_notifiers(env={"LINE_CHANNEL_TOKEN": "t", "LINE_USER_ID": "U1"})] == ["line"]


def test_both_channels_can_run_together():
    notifiers = available_notifiers(
        env={"NTFY_TOPIC": "x", "LINE_CHANNEL_TOKEN": "t", "LINE_USER_ID": "U1"}
    )

    assert [n.name for n in notifiers] == ["ntfy", "line"]


def test_github_issue_is_only_a_fallback():
    env = {"GITHUB_TOKEN": "gh", "GITHUB_REPOSITORY": "o/r"}

    assert [n.name for n in available_notifiers(env=env)] == ["github_issue"]
    assert [n.name for n in available_notifiers(env={**env, "NTFY_TOPIC": "x"})] == ["ntfy"]


# ---------------------------------------------------------------- payloads


def test_ntfy_posts_the_body_as_utf8_bytes(captured):
    NtfyNotifier(topic="my-topic").send("標題", "台北飛東京 9,800")

    assert captured[0]["url"] == "https://ntfy.sh/my-topic"
    assert captured[0]["data"] == "台北飛東京 9,800".encode("utf-8")


def test_poll_parses_newline_delimited_json(monkeypatch):
    """ntfy answers a poll with one JSON object per line, not a JSON array."""
    body = (
        '{"id":"m1","event":"open","topic":"t"}\n'
        '{"id":"m2","event":"message","topic":"t","message":"hunter2 /add BNE"}\n'
        '{"id":"m3","event":"keepalive","topic":"t"}\n'
        '{"id":"m4","event":"message","topic":"t","message":"hunter2 /run"}\n'
    )
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeGet(body))

    messages = poll_messages("t")

    assert [m["id"] for m in messages] == ["m2", "m4"], "open/keepalive events are not messages"
    assert messages[0]["message"] == "hunter2 /add BNE"


def test_poll_survives_a_truncated_line(monkeypatch):
    body = '{"id":"m1","event":"message","message":"ok"}\n{"id":"m2","event":"mess\n'
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeGet(body))

    assert [m["id"] for m in poll_messages("t")] == ["m1"]


def test_poll_passes_the_cursor_through(monkeypatch):
    seen = {}

    def fake_get(url, **kwargs):
        seen.update({"url": url, **kwargs})
        return FakeGet("")

    monkeypatch.setattr("requests.get", fake_get)
    poll_messages("mytopic", since="m42")

    assert seen["url"] == "https://ntfy.sh/mytopic/json"
    assert seen["params"] == {"poll": "1", "since": "m42"}


def test_ntfy_honours_a_self_hosted_server():
    assert NtfyNotifier(topic="t", server="https://ntfy.example.com/").url == "https://ntfy.example.com/t"


def test_ntfy_title_survives_non_latin1_text(captured):
    """A raw Chinese title would raise UnicodeEncodeError inside requests."""
    NtfyNotifier(topic="t").send("✈️ 便宜機票", "body")

    title = captured[0]["headers"]["Title"]
    assert title.encode("latin-1"), "header must be latin-1 safe"
    assert title.startswith("=?utf-8?")


def test_ascii_titles_are_left_alone():
    assert _encode_header("Cheap flight TPE-NRT") == "Cheap flight TPE-NRT"


# The real subject that silently lost a notification in production: short
# Chinese titles fit on one line, but this one got folded across two and
# requests refused to send a header containing a newline.
REAL_FAILING_SUBJECT = "✈️ 便宜機票 TPE>SYD>TPE 22,158 TWD（共 3 筆）"


@pytest.mark.parametrize(
    "subject",
    [
        REAL_FAILING_SUBJECT,
        "✈️ 機票追蹤器測試訊息",
        "⚠️ 機票追蹤器查不到任何票價",
        "✈️ 便宜機票 TPE>SYD / OOL>TPE 123,456 TWD（共 99 筆）" * 4,
    ],
)
def test_encoded_titles_stay_on_one_line_and_decode_back(subject):
    from email.header import decode_header, make_header

    encoded = _encode_header(subject)

    assert "\n" not in encoded and "\r" not in encoded, "a folded header is rejected by requests"
    encoded.encode("latin-1")  # exactly what requests requires
    assert str(make_header(decode_header(encoded))) == subject, "must survive the round trip"


def test_a_long_chinese_title_is_actually_sent(captured):
    """End to end through the notifier, not just the encoder."""
    NtfyNotifier(topic="t").send(REAL_FAILING_SUBJECT, "body")

    assert len(captured) == 1
    captured[0]["headers"]["Title"].encode("latin-1")


def test_no_tags_header_is_sent(captured):
    """ntfy turns an emoji-shortcode tag into a prefix on the title.

    With one, every alert reads '✈️ ✈️ ...' and the ⚠️ failure notice reads
    '✈️ ⚠️ ...', which dresses a breakage up as a flight deal.
    """
    NtfyNotifier(topic="t").send("⚠️ 機票追蹤器查不到任何票價", "body")

    assert "Tags" not in captured[0]["headers"]


def test_priority_is_high_so_alerts_break_through(captured):
    NtfyNotifier(topic="t").send("subject", "body")

    assert captured[0]["headers"]["Priority"] == "high"


def test_line_pushes_to_the_configured_user(captured):
    LineNotifier(token="tok", user_id="U123").send("標題", "內文")

    call = captured[0]
    assert call["url"] == "https://api.line.me/v2/bot/message/push"
    assert call["json"]["to"] == "U123"
    assert call["json"]["messages"][0] == {"type": "text", "text": "標題\n\n內文"}
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_line_truncates_over_long_messages(captured):
    LineNotifier(token="t", user_id="U1").send("標題", "x" * 6000)

    text = captured[0]["json"]["messages"][0]["text"]
    assert len(text) == 5000
    assert text.endswith("…")


def test_line_surfaces_the_api_error_body(monkeypatch):
    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResponse(400, '{"message":"Invalid to"}'))

    with pytest.raises(Exception, match="Invalid to"):
        LineNotifier(token="t", user_id="bad").send("s", "b")


# ---------------------------------------------------------------- fan-out


class Boom:
    name = "boom"

    def send(self, subject, body):
        raise RuntimeError("channel down")


class Fine:
    name = "fine"

    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))


def test_one_failing_channel_does_not_block_the_others():
    good = Fine()

    results = deliver([Boom(), good], "s", "b")

    assert [(r.channel, r.ok) for r in results] == [("boom", False), ("fine", True)]
    assert good.sent == [("s", "b")]
    assert "channel down" in results[0].detail


# ---------------------------------------------------------------- formatting


def test_subject_names_the_cheapest_fare():
    alerts = [
        Alert(quote=make_quote(12000), reason="低於門檻"),
        Alert(quote=make_quote(9800, itinerary="TPE>KIX>TPE"), reason="低於門檻"),
    ]

    subject, body = format_alerts(alerts)

    assert "TPE>KIX>TPE" in subject
    assert "9,800" in subject
    assert "共 2 筆" in subject
    assert body.index("9,800") < body.index("12,000"), "cheapest first"


def test_single_alert_subject_has_no_count():
    subject, _ = format_alerts([Alert(quote=make_quote(9800), reason="低於門檻")])

    assert "共" not in subject


def test_body_includes_the_booking_link_and_the_reason():
    _, body = format_alerts([Alert(quote=make_quote(9800), reason="比中位數便宜 20%")])

    assert "https://example.invalid/flight" in body
    assert "比中位數便宜 20%" in body
    assert "2026-12-20~2026-12-27" in body
