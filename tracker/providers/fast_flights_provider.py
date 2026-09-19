"""Primary price source: Google Flights via the ``fast-flights`` package.

fast-flights reverse-engineers the protobuf query Google Flights uses, so it
needs no API key and returns real bookable fares. It is unofficial, though:
Google changing its response shape will break it. Every failure mode here is
converted into :class:`ProviderError` so the runner can fall through to
SerpApi (when configured) rather than crashing the whole run.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import FAST_FLIGHTS_TRIP, Quote, SearchSpec
from . import ProviderError


class FastFlightsProvider:
    name = "fast_flights"

    def __init__(self, proxy: str | None = None):
        self.proxy = proxy

    def _build_query(self, spec: SearchSpec):
        try:
            import fast_flights as ff
        except ImportError as exc:  # pragma: no cover - import guard
            raise ProviderError("fast-flights 未安裝，請執行 pip install fast-flights") from exc

        options = spec.options
        legs = [
            ff.FlightQuery(
                date=leg.date.isoformat(),
                from_airport=leg.origin,
                to_airport=leg.destination,
                max_stops=options.max_stops,
            )
            for leg in spec.legs
        ]
        return ff, ff.create_query(
            flights=legs,
            trip=FAST_FLIGHTS_TRIP[spec.trip],
            seat=options.seat,
            currency=options.currency,
            max_stops=options.max_stops,
            passengers=ff.Passengers(
                adults=options.adults,
                children=options.children,
                infants_in_seat=options.infants_in_seat,
                infants_on_lap=options.infants_on_lap,
            ),
        )

    def booking_url(self, spec: SearchSpec) -> str:
        """The Google Flights deep link for this search.

        Useful on its own: alerts carry it so the fare can be booked with one
        tap, and it still resolves even when parsing the results failed.
        """
        try:
            _, query = self._build_query(spec)
            return query.url()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"無法組出 Google Flights 連結: {exc}") from exc

    def search(self, spec: SearchSpec) -> list[Quote]:
        if spec.trip == "multi" and len(spec.legs) > 1:
            return self._search_multi_as_separate_legs(spec)

        ff, query = self._build_query(spec)

        try:
            url = query.url()
        except Exception:  # noqa: BLE001 - a missing link must not lose the prices
            url = ""

        try:
            results = ff.get_flights(query, proxy=self.proxy)
        except ff.FlightsNotFound as exc:
            # A genuinely empty result, not a breakage: no flights match the
            # filters (too few stops allowed, sold out, route does not exist).
            raise ProviderError(f"查無航班: {spec} ({exc})") from exc
        except Exception as exc:
            raise ProviderError(f"fast-flights 查詢失敗: {type(exc).__name__}: {exc}") from exc

        fetched_at = datetime.now(timezone.utc)
        quotes = [q for q in (self._to_quote(f, spec, url, fetched_at) for f in results) if q]
        if not quotes:
            raise ProviderError(f"fast-flights 回傳 {len(results)} 筆結果但沒有可用票價: {spec}")

        quotes.sort(key=lambda q: q.price)
        return quotes

    def _search_multi_as_separate_legs(self, spec: SearchSpec) -> list[Quote]:
        """Work around fast-flights' own multi-city query mode, which crashes
        parsing Google's response for a genuine open-jaw route (observed:
        ``IndexError: list index out of range`` on TPE>SYD / BNE>TPE) -- a bug
        inside the unofficial library's handling of that response shape, not
        something fixable from here.

        Query each leg as its own one-way search instead and add the cheapest
        fare from each together. This is an approximation, not a real
        combined-ticket price -- an airline's own multi-city fare can be
        cheaper than two one-ways stitched together -- but it is real, current
        data instead of nothing.
        """
        leg_quotes: list[Quote] = []
        for leg in spec.legs:
            leg_spec = SearchSpec(
                legs=(leg,),
                trip="oneway",
                options=spec.options,
                route_name=spec.route_name,
                group=spec.group,
            )
            try:
                leg_quotes.append(self.search(leg_spec)[0])
            except ProviderError as exc:
                raise ProviderError(f"多段行程 {leg.origin}>{leg.destination} 這段查詢失敗：{exc}") from exc

        combined = Quote(
            route_name=spec.route_name,
            group=spec.group,
            itinerary=spec.itinerary,
            origin=spec.origin,
            destination=spec.destination,
            depart=spec.depart,
            ret=spec.ret,
            price=sum(q.price for q in leg_quotes),
            currency=spec.options.currency,
            airlines=tuple(dict.fromkeys(a for q in leg_quotes for a in q.airlines)),
            stops=sum(q.stops for q in leg_quotes),
            duration_minutes=sum(q.duration_minutes for q in leg_quotes),
            url="\n".join(q.url for q in leg_quotes if q.url),
            source=self.name,
            fetched_at=datetime.now(timezone.utc),
        )
        return [combined]

    def _to_quote(self, flight, spec: SearchSpec, url: str, fetched_at: datetime) -> Quote | None:
        price = getattr(flight, "price", None)
        if not isinstance(price, int) or price <= 0:
            # Google shows some rows without a price (e.g. "price unavailable").
            return None

        segments = list(getattr(flight, "flights", []) or [])
        duration = sum(int(getattr(s, "duration", 0) or 0) for s in segments)

        # Stops are counted on the outbound leg only, which is what a traveller
        # reads off the search page. Summing every segment across a round trip
        # would double it and make `max_stops` comparisons meaningless.
        outbound = self._outbound_segments(segments, spec)
        stops = max(len(outbound) - 1, 0)

        airlines = tuple(dict.fromkeys(str(a) for a in getattr(flight, "airlines", []) or []))

        return Quote(
            route_name=spec.route_name,
            group=spec.group,
            itinerary=spec.itinerary,
            origin=spec.origin,
            destination=spec.destination,
            depart=spec.depart,
            ret=spec.ret,
            price=price,
            currency=spec.options.currency,
            airlines=airlines,
            stops=stops,
            duration_minutes=duration,
            url=url,
            source=self.name,
            fetched_at=fetched_at,
        )

    @staticmethod
    def _outbound_segments(segments, spec: SearchSpec) -> list:
        """Segments belonging to the first leg.

        Split on the second leg's departure date rather than matching the first
        leg's date exactly: an outbound connection that lands after midnight is
        still outbound, and it always departs before the return does.
        """
        if len(spec.legs) < 2:
            return list(segments)

        cutoff = spec.legs[1].date
        outbound = []
        for segment in segments:
            raw = getattr(getattr(segment, "departure", None), "date", None)
            if not (isinstance(raw, (tuple, list)) and len(raw) == 3):
                # Unknown shape: treat everything as outbound rather than
                # silently reporting zero stops.
                return list(segments)
            if tuple(raw) < (cutoff.year, cutoff.month, cutoff.day):
                outbound.append(segment)
        # A same-day turnaround puts every segment on or after the cutoff.
        return outbound or list(segments)
