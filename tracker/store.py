"""Append-only price history in CSV, plus the statistics alerts are judged against.

CSV under git is the whole database: every run appends rows and commits them, so
the history is diffable, needs no server, and survives the runner being thrown
away. Rows are never rewritten -- a wrong price stays visible in history rather
than being quietly corrected.
"""

from __future__ import annotations

import csv
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .models import Quote

FIELDNAMES = [
    "fetched_at",
    "route_name",
    "group",
    "itinerary",
    "origin",
    "destination",
    "depart",
    "ret",
    "price",
    "currency",
    "airlines",
    "stops",
    "duration_minutes",
    "source",
    "url",
]


def _row_key(row: dict) -> str:
    """Identity of a priced itinerary, independent of when it was fetched."""
    return f"{row['itinerary']}|{row['depart']}|{row['ret']}|{row['currency']}"


def quote_key(quote: Quote) -> str:
    return f"{quote.itinerary}|{quote.depart.isoformat()}|{quote.ret.isoformat() if quote.ret else ''}|{quote.currency}"


class PriceStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._cache: list[dict] | None = None

    def append(self, quotes: list[Quote]) -> int:
        """Append quotes, creating the file with a header if needed. Returns rows written."""
        if not quotes:
            return 0
        self._cache = None

        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists() or self.path.stat().st_size == 0

        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
            if is_new:
                writer.writeheader()
            for quote in quotes:
                writer.writerow(
                    {
                        "fetched_at": quote.fetched_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
                        "route_name": quote.route_name,
                        "group": quote.group,
                        "itinerary": quote.itinerary,
                        "origin": quote.origin,
                        "destination": quote.destination,
                        "depart": quote.depart.isoformat(),
                        "ret": quote.ret.isoformat() if quote.ret else "",
                        "price": quote.price,
                        "currency": quote.currency,
                        "airlines": "/".join(quote.airlines),
                        "stops": quote.stops,
                        "duration_minutes": quote.duration_minutes,
                        "source": quote.source,
                        "url": quote.url,
                    }
                )
        return len(quotes)

    def rows(self) -> list[dict]:
        """All history rows, oldest first. Malformed rows are skipped, not fatal.

        Cached, because a run computes a baseline per search and would otherwise
        re-read the whole file dozens of times per run.
        """
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            self._cache = []
            return self._cache
        with self.path.open(newline="", encoding="utf-8") as handle:
            self._cache = [row for row in csv.DictReader(handle) if row.get("price")]
        return self._cache

    def baseline(self, quote: Quote, *, days: int, now: datetime | None = None) -> float | None:
        """Median historical price for this exact itinerary within ``days``.

        Returns ``None`` when there is not enough history to judge against --
        two data points can be noise, and alerting off them would fire on the
        very first run for every route.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        key = quote_key(quote)

        prices = []
        for row in self.rows():
            if _row_key(row) != key:
                continue
            try:
                fetched = datetime.fromisoformat(row["fetched_at"])
                price = int(row["price"])
            except (ValueError, KeyError):
                continue
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if fetched >= cutoff:
                prices.append(price)

        if len(prices) < 3:
            return None
        return statistics.median(prices)

    def cheapest_per_key(self, *, group: str | None = None, since: date | None = None) -> list[dict]:
        """Latest-run cheapest row per itinerary, for the report command."""
        best: dict[str, dict] = {}
        for row in self.rows():
            if group and row.get("group") != group:
                continue
            if since:
                try:
                    if date.fromisoformat(row["fetched_at"][:10]) < since:
                        continue
                except ValueError:
                    continue
            key = _row_key(row)
            current = best.get(key)
            if current is None or int(row["price"]) < int(current["price"]):
                best[key] = row
        return sorted(best.values(), key=lambda r: int(r["price"]))

    def cheapest_per_pair(self, *, group: str | None = None, since: date | None = None) -> list[dict]:
        """Cheapest row per origin-destination pair, whatever the dates.

        This is what a ``compare: true`` group is for: "which of these places is
        cheapest to fly to", collapsing the date sweep that found it.
        """
        best: dict[tuple[str, str], dict] = {}
        for row in self.cheapest_per_key(group=group, since=since):
            pair = (row["origin"], row["destination"])
            current = best.get(pair)
            if current is None or int(row["price"]) < int(current["price"]):
                best[pair] = row
        return sorted(best.values(), key=lambda r: int(r["price"]))
