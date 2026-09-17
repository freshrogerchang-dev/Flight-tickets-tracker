"""Core data types shared by the whole pipeline.

Every route shape in ``routes.yaml`` -- a plain two-point route, a many-to-many
comparison group, or a multi-city itinerary -- is normalised into a flat
:class:`SearchSpec` before anything else touches it. Providers, the CSV store
and the alert logic only ever see ``SearchSpec`` and :class:`Quote`, so they
never need to branch on the original YAML shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

TripType = Literal["round", "oneway", "multi"]
SeatType = Literal["economy", "premium-economy", "business", "first"]

#: Mapping from our trip vocabulary to the one fast-flights uses.
FAST_FLIGHTS_TRIP = {
    "round": "round-trip",
    "oneway": "one-way",
    "multi": "multi-city",
}


@dataclass(frozen=True)
class Leg:
    """One flown segment of an itinerary."""

    origin: str
    destination: str
    date: date

    def __str__(self) -> str:
        return f"{self.origin}>{self.destination}@{self.date.isoformat()}"


@dataclass(frozen=True)
class SearchOptions:
    """Passenger and cabin options. Shared by every leg of one search."""

    adults: int = 1
    children: int = 0
    infants_in_seat: int = 0
    infants_on_lap: int = 0
    seat: SeatType = "economy"
    max_stops: int | None = None
    currency: str = "TWD"


@dataclass(frozen=True)
class SearchSpec:
    """A single, fully resolved query: one itinerary on one set of dates."""

    legs: tuple[Leg, ...]
    trip: TripType
    options: SearchOptions
    route_name: str = ""
    #: Name of the comparison group this spec belongs to, for `compare: true`
    #: routes. Empty when the route is not part of a ranking group.
    group: str = ""

    @property
    def origin(self) -> str:
        return self.legs[0].origin

    @property
    def destination(self) -> str:
        return self.legs[0].destination

    @property
    def depart(self) -> date:
        return self.legs[0].date

    @property
    def ret(self) -> date | None:
        """Return date for a round trip, else ``None``."""
        return self.legs[1].date if self.trip == "round" and len(self.legs) > 1 else None

    @property
    def itinerary(self) -> str:
        """Human-readable airport path, e.g. ``TPE>NRT>KIX>TPE``.

        Multi-city legs need not be contiguous -- flying into NRT and out of
        KIX is the whole point -- so a gap between one leg's destination and
        the next leg's origin is shown as ``/``, not collapsed away.
        """
        parts = [self.legs[0].origin]
        for index, leg in enumerate(self.legs):
            if index and leg.origin != self.legs[index - 1].destination:
                parts.append(f"/{leg.origin}")
            parts.append(leg.destination)
        return ">".join(parts).replace(">/", " / ")

    @property
    def key(self) -> str:
        """Stable identity for dedup and alert bookkeeping.

        Deliberately excludes ``route_name`` so that renaming a route in
        ``routes.yaml`` does not resurrect alerts that were already sent.
        """
        legs = "|".join(str(leg) for leg in self.legs)
        opts = self.options
        return f"{self.trip}:{legs}:{opts.seat}:{opts.adults}a{opts.children}c"

    def __str__(self) -> str:
        dates = self.depart.isoformat()
        if self.ret:
            dates += f"~{self.ret.isoformat()}"
        elif self.trip == "multi":
            dates = " ".join(leg.date.isoformat() for leg in self.legs)
        return f"{self.itinerary} {dates}"


@dataclass
class Quote:
    """One priced itinerary returned by a provider."""

    route_name: str
    group: str
    itinerary: str
    origin: str
    destination: str
    depart: date
    ret: date | None
    price: int
    currency: str
    airlines: tuple[str, ...] = ()
    stops: int = 0
    duration_minutes: int = 0
    url: str = ""
    source: str = ""
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def airline_label(self) -> str:
        return ", ".join(self.airlines) if self.airlines else "?"

    @property
    def duration_label(self) -> str:
        if not self.duration_minutes:
            return "?"
        hours, minutes = divmod(self.duration_minutes, 60)
        return f"{hours}h{minutes:02d}m"

    @property
    def date_label(self) -> str:
        return f"{self.depart.isoformat()}~{self.ret.isoformat()}" if self.ret else self.depart.isoformat()


@dataclass
class Alert:
    """A quote that cleared the cheapness test, with the reason why."""

    quote: Quote
    reason: str
    #: Median price over the comparison window, when one could be computed.
    baseline: float | None = None
