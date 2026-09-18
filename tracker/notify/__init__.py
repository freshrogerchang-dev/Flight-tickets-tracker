"""Notification channels.

Channels are opt-in by environment variable: set the secret, get the channel.
Nothing is configured in ``routes.yaml``, so a token never ends up committed.

Delivery fans out across every configured channel and one channel failing never
stops the others -- a LINE token expiring should not cost you the ntfy push.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..models import Alert


class NotifyError(RuntimeError):
    """A channel could not deliver. Reported, never fatal."""


@runtime_checkable
class Notifier(Protocol):
    name: str

    def send(self, subject: str, body: str) -> None:
        """Deliver one message. Raise NotifyError on failure."""
        ...


@dataclass
class DeliveryResult:
    channel: str
    ok: bool
    detail: str = ""


def available_notifiers(env: dict[str, str] | None = None) -> list[Notifier]:
    """Build the notifier list from whichever secrets are present.

    Telegram, LINE and ntfy are all opt-in this way. GitHub Issue is the
    fallback so that a misconfigured run still leaves a visible trace instead
    of dropping a cheap fare on the floor.
    """
    env = env if env is not None else dict(os.environ)
    notifiers: list[Notifier] = []

    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        from .telegram import TelegramNotifier

        notifiers.append(TelegramNotifier(token=env["TELEGRAM_BOT_TOKEN"], chat_id=env["TELEGRAM_CHAT_ID"]))

    if env.get("NTFY_TOPIC"):
        from .ntfy import NtfyNotifier

        notifiers.append(NtfyNotifier(topic=env["NTFY_TOPIC"], server=env.get("NTFY_SERVER")))

    if env.get("LINE_CHANNEL_TOKEN") and env.get("LINE_USER_ID"):
        from .line import LineNotifier

        notifiers.append(LineNotifier(token=env["LINE_CHANNEL_TOKEN"], user_id=env["LINE_USER_ID"]))

    if not notifiers and env.get("GITHUB_TOKEN") and env.get("GITHUB_REPOSITORY"):
        from .github_issue import GitHubIssueNotifier

        notifiers.append(
            GitHubIssueNotifier(token=env["GITHUB_TOKEN"], repository=env["GITHUB_REPOSITORY"])
        )

    return notifiers


def deliver(notifiers: list[Notifier], subject: str, body: str) -> list[DeliveryResult]:
    """Send to every channel, collecting per-channel outcomes."""
    results = []
    for notifier in notifiers:
        try:
            notifier.send(subject, body)
            results.append(DeliveryResult(notifier.name, True))
        except Exception as exc:  # noqa: BLE001 - one bad channel must not sink the rest
            results.append(DeliveryResult(notifier.name, False, f"{type(exc).__name__}: {exc}"))
    return results


def format_alerts(alerts: list[Alert]) -> tuple[str, str]:
    """Render alerts into a (subject, body) pair that reads well everywhere.

    Plain text with no markup: LINE renders none, ntfy renders none, and the
    GitHub issue fallback is readable either way.
    """
    cheapest = min(alerts, key=lambda a: a.quote.price)
    subject = f"✈️ 便宜機票 {cheapest.quote.itinerary} {cheapest.quote.price:,} {cheapest.quote.currency}"
    if len(alerts) > 1:
        subject += f"（共 {len(alerts)} 筆）"

    lines = []
    for alert in sorted(alerts, key=lambda a: a.quote.price):
        quote = alert.quote
        lines.append(f"{quote.itinerary}  {quote.price:,} {quote.currency}")
        lines.append(f"  {quote.date_label} · {quote.airline_label} · 轉機 {quote.stops} · {quote.duration_label}")
        lines.append(f"  {alert.reason}")
        if quote.url:
            lines.append(f"  {quote.url}")
        lines.append("")

    return subject, "\n".join(lines).rstrip()
