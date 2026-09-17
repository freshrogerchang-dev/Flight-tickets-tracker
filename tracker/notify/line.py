"""LINE push messages via the Messaging API.

LINE Notify shut down on 2025-03-31, so this goes through a LINE Official
Account instead: create a Messaging API channel, issue a long-lived channel
access token, add the account as a friend, and push to your own user id. The
README walks through getting both values.

Push messages count against the account's monthly free quota; a price tracker
sending a handful of alerts a month will not come close to it.
"""

from __future__ import annotations

import requests

from . import NotifyError

ENDPOINT = "https://api.line.me/v2/bot/message/push"
TIMEOUT_SECONDS = 15
#: LINE rejects text messages longer than this.
MAX_TEXT_LENGTH = 5000


class LineNotifier:
    name = "line"

    def __init__(self, token: str, user_id: str):
        if not token or not user_id:
            raise NotifyError("LINE_CHANNEL_TOKEN 或 LINE_USER_ID 是空的")
        self.token = token.strip()
        self.user_id = user_id.strip()

    def send(self, subject: str, body: str) -> None:
        text = f"{subject}\n\n{body}".strip()
        if len(text) > MAX_TEXT_LENGTH:
            text = text[: MAX_TEXT_LENGTH - 1] + "…"

        payload = {"to": self.user_id, "messages": [{"type": "text", "text": text}]}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        try:
            response = requests.post(ENDPOINT, json=payload, headers=headers, timeout=TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise NotifyError(f"LINE 發送失敗: {exc}") from exc

        if response.status_code != 200:
            # LINE puts the actual reason in the body; the status alone
            # ("400 Bad Request") is useless for debugging a bad user id.
            raise NotifyError(f"LINE 發送失敗 (HTTP {response.status_code}): {response.text[:300]}")
