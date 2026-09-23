"""Telegram: notifications, and (like ntfy) a two-way command channel.

Telegram's Bot API is REST and pull-based on the inbound side -- ``getUpdates``
long-polls for whatever the user typed to the bot, the same shape of trick
ntfy's pub/sub gives us. So exactly like ntfy, no server of our own has to
exist for the phone-to-tracker direction either: a scheduled job just polls.

Setup is one bot via @BotFather plus the numeric chat id of whoever talks to
it -- see the README for the walkthrough. Everything here talks to a single
bot; multiple people would need multiple bots (or a shared chat_id, like a
group chat added as the bot's peer).
"""

from __future__ import annotations

from pathlib import Path

import requests

from . import NotifyError

API_ROOT = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 15
POLL_TIMEOUT_SECONDS = 20
#: Telegram silently truncates or rejects longer messages.
MAX_TEXT_LENGTH = 4096


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str):
        if not token or not chat_id:
            raise NotifyError("TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 是空的")
        self.token = token.strip()
        self.chat_id = chat_id.strip()

    def send(self, subject: str, body: str) -> None:
        text = f"{subject}\n\n{body}".strip()
        if len(text) > MAX_TEXT_LENGTH:
            text = text[: MAX_TEXT_LENGTH - 1] + "…"

        # Plain text, no parse_mode: alert bodies contain '.', '-', '(', ')'
        # and the like, every one a Markdown/HTML metacharacter that would
        # need escaping. Sending as-is avoids a whole class of "message
        # silently mangled or rejected" failures for a feature nothing here needs.
        try:
            response = requests.post(
                f"{API_ROOT}/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text},
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise NotifyError(f"Telegram 發送失敗: {exc}") from exc

        if response.status_code != 200:
            # Telegram's error detail (bad chat id, bot blocked, ...) is in the
            # body; the status code alone is not enough to debug from.
            raise NotifyError(f"Telegram 發送失敗 (HTTP {response.status_code}): {response.text[:300]}")


#: Telegram truncates or rejects a longer photo caption.
MAX_CAPTION_LENGTH = 1024


def send_photo(token: str, chat_id: str, photo_path: str | Path, *, caption: str = "") -> None:
    """Send one local image file as a Telegram photo message.

    Used by ``/chart``: a price-trend PNG is more useful as an actual picture
    than as a link, and ``sendPhoto`` is the one Bot API call that needs a
    multipart body instead of the plain JSON every other notifier here sends.
    """
    token = token.strip()
    if len(caption) > MAX_CAPTION_LENGTH:
        caption = caption[: MAX_CAPTION_LENGTH - 1] + "…"

    try:
        with open(photo_path, "rb") as handle:
            response = requests.post(
                f"{API_ROOT}/bot{token}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": handle},
                timeout=SEND_TIMEOUT_SECONDS,
            )
    except (requests.RequestException, OSError) as exc:
        raise NotifyError(f"Telegram 傳圖失敗: {exc}") from exc

    if response.status_code != 200:
        raise NotifyError(f"Telegram 傳圖失敗 (HTTP {response.status_code}): {response.text[:300]}")


def poll_updates(token: str, *, since: str = "") -> list[dict]:
    """Read messages sent to the bot since the last consumed update.

    ``since`` is the last update id already handled (``Cursor.last_id``), as a
    plain string; empty means "whatever is currently pending". Telegram's own
    ``offset`` is exclusive-of-earlier via ``last_id + 1``, computed here so
    callers never have to reason about Telegram's off-by-one.

    Every pending update is returned, one entry per ``update_id`` -- not just
    text messages -- so the caller's cursor advances past non-text updates
    (a sticker, someone else joining the chat) instead of re-fetching them
    forever. Non-text updates carry an empty ``message``, which never matches
    a command's secret prefix and is silently skipped downstream.
    """
    # A GitHub secret pasted with a trailing newline is common enough that it
    # has actually happened here: an un-stripped token turns into a %0A in the
    # URL, which Telegram answers with a plain 404 -- no hint it was whitespace.
    token = token.strip()
    params: dict = {}
    if since:
        try:
            params["offset"] = int(since) + 1
        except ValueError:
            pass  # a corrupt cursor should not stop polling; refetch from scratch

    try:
        response = requests.get(
            f"{API_ROOT}/bot{token}/getUpdates",
            params=params,
            timeout=POLL_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise NotifyError(f"Telegram 讀取失敗: {exc}") from exc
    except ValueError as exc:
        raise NotifyError(f"Telegram 回傳的不是 JSON: {exc}") from exc

    if not payload.get("ok"):
        raise NotifyError(f"Telegram 錯誤: {payload.get('description', payload)}")

    messages = []
    for update in payload.get("result") or []:
        update_id = update.get("update_id")
        if update_id is None:
            continue
        text = ((update.get("message") or {}).get("text")) or ""
        messages.append({"id": str(update_id), "message": text})
    return messages
