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
        """Median of what this itinerary *cost* on each past run, within ``days``.

        Each run stores several quotes for one search -- the cheapest plus the
        next couple -- and they all share one ``fetched_at``. Taking the median
        across every stored row would therefore measure the spread between
        options on a single day, not movement over time: the cheapest quote is
        always well below the median of its own siblings, so "cheaper than the
        30-day median" would be true on virtually every run forever.

        So collapse each run to its cheapest quote first, then take the median
        of those. A drop then means today is cheap against what this itinerary
        actually sold for on previous days.

        Returns ``None`` below three runs of history: two points are noise, and
        judging against them would alert on every route from day one.
        """
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        key = quote_key(quote)

        cheapest_per_run: dict[str, int] = {}
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
            if fetched < cutoff:
                continue
            # One search writes all its quotes with the same timestamp, which
            # is what makes it usable as a run identifier.
            stamp = row["fetched_at"]
            if stamp not in cheapest_per_run or price < cheapest_per_run[stamp]:
                cheapest_per_run[stamp] = price

        if len(cheapest_per_run) < 3:
            return None
        return statistics.median(cheapest_per_run.values())

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

    def top_n_per_route(self, *, n: int = 3, since: date | None = None) -> list[tuple[str, list[dict]]]:
        """Each route's own ``n`` cheapest itineraries, ranked separately.

        Unlike ``cheapest_per_pair`` (one winner across a whole comparison
        group), every tracked route stays visible here even when one route
        is much cheaper than another -- for ``/report``, which shows routes
        side by side rather than picking a single overall winner.

        Routes are returned cheapest-route-first, so the best deal leads.
        """
        by_route: dict[str, list[dict]] = {}
        for row in self.cheapest_per_key(since=since):
            bucket = by_route.setdefault(row["route_name"], [])
            if len(bucket) < n:
                bucket.append(row)
        return sorted(by_route.items(), key=lambda item: int(item[1][0]["price"]))

    def latest_per_route(self) -> list[dict]:
        """The cheapest quote from each route's most recent run, one row per route.

        For ``/status``: a snapshot of "what did we see last", not the
        all-time cheapest -- that's what ``/report`` is for.
        """
        latest_stamp: dict[str, str] = {}
        for row in self.rows():
            name = row.get("route_name", "")
            stamp = row.get("fetched_at", "")
            if name not in latest_stamp or stamp > latest_stamp[name]:
                latest_stamp[name] = stamp

        best: dict[str, dict] = {}
        for row in self.rows():
            name = row.get("route_name", "")
            if row.get("fetched_at") != latest_stamp.get(name):
                continue
            current = best.get(name)
            if current is None or int(row["price"]) < int(current["price"]):
                best[name] = row
        return sorted(best.values(), key=lambda r: r["route_name"])

    def route_history(self, route_name: str, *, since: date | None = None) -> list[dict]:
        """Cheapest price per run for one route, oldest first.

        A run can price several itineraries for one route (a date sweep, a
        multi-destination compare); collapsing each run to its cheapest quote
        keeps one point per run for a price-trend chart, instead of a noisy
        cluster of same-timestamp dots.
        """
        cheapest_per_run: dict[str, dict] = {}
        for row in self.rows():
            if row.get("route_name") != route_name:
                continue
            if since:
                try:
                    if date.fromisoformat(row["fetched_at"][:10]) < since:
                        continue
                except ValueError:
                    continue
            stamp = row["fetched_at"]
            current = cheapest_per_run.get(stamp)
            if current is None or int(row["price"]) < int(current["price"]):
                cheapest_per_run[stamp] = row
        return [cheapest_per_run[stamp] for stamp in sorted(cheapest_per_run)]

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
