"""Decide which quotes are worth waking someone up for, and suppress repeats.

Two independent tests, either of which fires:

* ``alert_below`` -- an absolute price you would book on sight.
* ``alert_drop_pct`` -- cheaper than this itinerary's own recent median, which
  catches a good deal on a route whose normal price you never pinned down.

Dedup state lives in ``data/alerts.json`` and is committed alongside the price
history, so a daily cron does not re-send yesterday's alert every morning.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import RouteConfig
from .models import Alert, Quote
from .store import PriceStore, quote_key

#: Don't repeat an alert for the same itinerary within this window...
REPEAT_WINDOW_HOURS = 24
#: ...unless the price dropped by at least this much again, which is news.
RENOTIFY_DROP_PCT = 5.0


class AlertState:
    """Remembers what was already announced, so alerts stay signal not spam."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._state: dict[str, dict] = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self._state = loaded
            except (json.JSONDecodeError, OSError):
                # Corrupt state costs at most one duplicate notification;
                # refusing to run would cost the whole day's tracking.
                self._state = {}

    def should_send(self, quote: Quote, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        previous = self._state.get(quote_key(quote))
        if not previous:
            return True

        try:
            sent_at = datetime.fromisoformat(previous["sent_at"])
            last_price = int(previous["price"])
        except (KeyError, ValueError):
            return True
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)

        if now - sent_at >= timedelta(hours=REPEAT_WINDOW_HOURS):
            return True
        # Still inside the quiet window, but a further real drop is worth saying.
        return quote.price <= last_price * (1 - RENOTIFY_DROP_PCT / 100)

    def record(self, quote: Quote, *, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._state[quote_key(quote)] = {
            "sent_at": now.isoformat(timespec="seconds"),
            "price": quote.price,
            "currency": quote.currency,
        }

    def prune(self, *, now: datetime | None = None, keep_days: int = 180) -> None:
        """Drop entries for itineraries whose departure is long past."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=keep_days)
        kept = {}
        for key, value in self._state.items():
            try:
                sent_at = datetime.fromisoformat(value["sent_at"])
            except (KeyError, ValueError):
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            if sent_at >= cutoff:
                kept[key] = value
        self._state = kept

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def evaluate(
    quote: Quote,
    route: RouteConfig,
    store: PriceStore,
    *,
    baseline_days: int = 30,
    now: datetime | None = None,
) -> Alert | None:
    """Return an :class:`Alert` if ``quote`` is cheap enough, else ``None``."""
    if route.alert_below is not None and quote.price <= route.alert_below:
        return Alert(
            quote=quote,
            reason=f"低於門檻 {route.alert_below:,} {quote.currency}",
        )

    if route.alert_drop_pct is not None:
        baseline = store.baseline(quote, days=baseline_days, now=now)
        if baseline is not None:
            threshold = baseline * (1 - route.alert_drop_pct / 100)
            if quote.price <= threshold:
                drop = (1 - quote.price / baseline) * 100
                return Alert(
                    quote=quote,
                    reason=f"比近 {baseline_days} 天中位數 {baseline:,.0f} 便宜 {drop:.0f}%",
                    baseline=baseline,
                )

    return None
