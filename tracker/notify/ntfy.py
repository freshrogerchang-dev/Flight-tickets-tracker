"""ntfy.sh push notifications.

The simplest channel there is: POST plain text to a topic URL, and any phone
subscribed to that topic buzzes. No account, no API key.

Security note: on the public ntfy.sh server the topic name *is* the credential.
Anyone who knows it can read your alerts and send you their own, so the topic
must be a long random string, not ``flights``. See the README for how to
generate one.
"""

from __future__ import annotations

import json

import requests

from . import NotifyError

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_SECONDS = 15
POLL_TIMEOUT_SECONDS = 30


class NtfyNotifier:
    name = "ntfy"

    def __init__(self, topic: str, server: str | None = None, token: str | None = None):
        if not topic:
            raise NotifyError("NTFY_TOPIC 是空的")
        self.topic = topic.strip()
        self.server = (server or DEFAULT_SERVER).rstrip("/")
        self.token = token

    @property
    def url(self) -> str:
        return f"{self.server}/{self.topic}"

    def send(self, subject: str, body: str) -> None:
        headers = {
            # ntfy reads these headers as message metadata. They must be
            # latin-1 encodable, and a Chinese subject is not, so the title is
            # sent RFC 2047 encoded, which the ntfy clients decode.
            "Title": _encode_header(subject),
            "Priority": "high",
            # No Tags header on purpose. ntfy renders a tag that matches an
            # emoji shortcode by prepending that emoji to the title, which
            # would double the one each subject already carries -- and worse,
            # would stamp a cheerful ✈️ onto the ⚠️ "tracker is broken" notice.
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            response = requests.post(
                self.url,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise NotifyError(f"ntfy 發送失敗: {exc}") from exc


def poll_messages(
    topic: str,
    *,
    server: str | None = None,
    since: str = "30m",
    token: str | None = None,
) -> list[dict]:
    """Read messages already sitting in a topic, without holding a connection open.

    ntfy is pub/sub in both directions, which is what makes a command channel
    possible with no server of our own: a scheduled job just polls the topic.

    ``since`` takes a message id (everything after that message), a duration
    like ``30m``, or ``all``. Returns oldest-first message objects.

    Note: ntfy.sh caches messages for about 12 hours, so a command sent while
    the poller is down for longer than that is simply lost, never applied late.
    """
    base = (server or DEFAULT_SERVER).rstrip("/")
    url = f"{base}/{topic}/json"
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    try:
        response = requests.get(
            url,
            params={"poll": "1", "since": since},
            headers=headers,
            timeout=POLL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise NotifyError(f"ntfy 讀取失敗: {exc}") from exc

    messages = []
    # The poll response is newline-delimited JSON, one object per line.
    for line in response.text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Topics also carry keepalive and open events, which are not messages.
        if event.get("event") == "message" and event.get("message"):
            messages.append(event)
    return messages


def _encode_header(value: str) -> str:
    """Encode a header value so non-latin-1 text survives the HTTP round trip."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        from email.header import Header

        return Header(value, "utf-8").encode()
