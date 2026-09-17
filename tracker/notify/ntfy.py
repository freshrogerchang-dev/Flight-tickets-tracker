"""ntfy.sh push notifications.

The simplest channel there is: POST plain text to a topic URL, and any phone
subscribed to that topic buzzes. No account, no API key.

Security note: on the public ntfy.sh server the topic name *is* the credential.
Anyone who knows it can read your alerts and send you their own, so the topic
must be a long random string, not ``flights``. See the README for how to
generate one.
"""

from __future__ import annotations

import requests

from . import NotifyError

DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT_SECONDS = 15


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
            "Tags": "airplane",
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


def _encode_header(value: str) -> str:
    """Encode a header value so non-latin-1 text survives the HTTP round trip."""
    try:
        value.encode("latin-1")
        return value
    except UnicodeEncodeError:
        from email.header import Header

        return Header(value, "utf-8").encode()
