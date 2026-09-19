"""Provider result mapping, tested against recorded shapes instead of the network.

fast-flights is an unofficial scraper, so the mapping from its objects to our
Quote is exactly where a Google change would show up. These fakes mirror the
real dataclasses (fast_flights.model.Flights / SingleFlight / Airport).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from tracker.models import Leg, Quote, SearchOptions, SearchSpec
from tracker.providers import ProviderError, get_provider
from tracker.providers.fast_flights_provider import FastFlightsProvider


@dataclass
class FakeAirport:
    code: str
    name: str = ""


@dataclass
class FakeDatetime:
    date: tuple
    time: tuple = (10, 0)


@dataclass
class FakeSegment:
    from_airport: FakeAirport
    to_airport: FakeAirport
    departure: FakeDatetime
    duration: int = 100
    arrival: FakeDatetime | None = None
    plane_type: str = ""


@dataclass
class FakeFlights:
    price: int
    airlines: list
    flights: list
    type: str = "round"
    carbon: object = None


def segment(origin: str, destination: str, on: date, duration: int = 100) -> FakeSegment:
    return FakeSegment(
        from_airport=FakeAirport(origin),
        to_airport=FakeAirport(destination),
        departure=FakeDatetime(date=(on.year, on.month, on.day)),
        duration=duration,
    )


def round_trip_spec(**overrides) -> SearchSpec:
    defaults = dict(
        legs=(
            Leg("TPE", "NRT", date(2026, 12, 20)),
            Leg("NRT", "TPE", date(2026, 12, 27)),
        ),
        trip="round",
        options=SearchOptions(currency="TWD"),
        route_name="台北-東京",
        group="日本",
    )
    defaults.update(overrides)
    return SearchSpec(**defaults)


@pytest.fixture
def provider() -> FastFlightsProvider:
    return FastFlightsProvider()


def to_quote(provider, flight, spec):
    return provider._to_quote(flight, spec, "https://example.invalid", __import__("datetime").datetime.now())


def test_quote_carries_route_identity_and_price(provider):
    spec = round_trip_spec()
    flight = FakeFlights(
        price=12345,
        airlines=["China Airlines"],
        flights=[segment("TPE", "NRT", date(2026, 12, 20), 185), segment("NRT", "TPE", date(2026, 12, 27), 200)],
    )

    quote = to_quote(provider, flight, spec)

    assert quote.price == 12345
    assert quote.currency == "TWD"
    assert quote.route_name == "台北-東京"
    assert quote.group == "日本"
    assert quote.itinerary == "TPE>NRT>TPE"
    assert quote.duration_minutes == 385, "duration covers the whole itinerary"


def test_rows_without_a_usable_price_are_dropped(provider):
    spec = round_trip_spec()

    for bad in (None, 0, -1, "12,345"):
        flight = FakeFlights(price=bad, airlines=[], flights=[segment("TPE", "NRT", date(2026, 12, 20))])
        assert to_quote(provider, flight, spec) is None


def test_stops_count_the_outbound_leg_only(provider):
    """A nonstop round trip is 0 stops, not 1 -- the return must not be counted."""
    spec = round_trip_spec()
    flight = FakeFlights(
        price=10000,
        airlines=["CI"],
        flights=[segment("TPE", "NRT", date(2026, 12, 20)), segment("NRT", "TPE", date(2026, 12, 27))],
    )

    assert to_quote(provider, flight, spec).stops == 0


def test_a_connecting_outbound_counts_one_stop(provider):
    spec = round_trip_spec()
    flight = FakeFlights(
        price=10000,
        airlines=["CI"],
        flights=[
            segment("TPE", "HKG", date(2026, 12, 20)),
            segment("HKG", "NRT", date(2026, 12, 20)),
            segment("NRT", "TPE", date(2026, 12, 27)),
        ],
    )

    assert to_quote(provider, flight, spec).stops == 1


def test_an_outbound_connection_past_midnight_is_still_outbound(provider):
    """Splitting on the first leg's date exactly would undercount this to 0 stops."""
    spec = round_trip_spec()
    flight = FakeFlights(
        price=10000,
        airlines=["CI"],
        flights=[
            segment("TPE", "SIN", date(2026, 12, 20)),
            segment("SIN", "NRT", date(2026, 12, 21)),  # red-eye connection
            segment("NRT", "TPE", date(2026, 12, 27)),
        ],
    )

    assert to_quote(provider, flight, spec).stops == 1


def test_one_way_counts_every_segment(provider):
    spec = round_trip_spec(legs=(Leg("TPE", "NRT", date(2026, 12, 20)),), trip="oneway")
    flight = FakeFlights(
        price=5000,
        airlines=["CI"],
        flights=[segment("TPE", "HKG", date(2026, 12, 20)), segment("HKG", "NRT", date(2026, 12, 20))],
    )

    quote = to_quote(provider, flight, spec)

    assert quote.stops == 1
    assert quote.ret is None


def test_unrecognised_segment_shape_falls_back_rather_than_reporting_zero(provider):
    spec = round_trip_spec()
    broken = FakeSegment(
        from_airport=FakeAirport("TPE"),
        to_airport=FakeAirport("NRT"),
        departure=FakeDatetime(date="2026-12-20"),  # a string, not the expected tuple
    )
    flight = FakeFlights(price=10000, airlines=["CI"], flights=[broken, broken])

    assert to_quote(provider, flight, spec).stops == 1, "counts all segments rather than silently saying 0"


def test_duplicate_airlines_are_collapsed_in_order(provider):
    spec = round_trip_spec()
    flight = FakeFlights(
        price=10000,
        airlines=["EVA Air", "China Airlines", "EVA Air"],
        flights=[segment("TPE", "NRT", date(2026, 12, 20))],
    )

    assert to_quote(provider, flight, spec).airlines == ("EVA Air", "China Airlines")


def test_booking_url_is_a_google_flights_link(provider):
    url = provider.booking_url(round_trip_spec())

    assert url.startswith("https://www.google.com/travel/flights")
    assert "curr=TWD" in url


# ---------------------------------------------------------------- multi-city split


def multi_spec(**overrides) -> SearchSpec:
    defaults = dict(
        legs=(Leg("TPE", "SYD", date(2027, 6, 5)), Leg("BNE", "TPE", date(2027, 6, 16))),
        trip="multi",
        options=SearchOptions(currency="TWD"),
        route_name="布里斯本進雪梨出",
    )
    defaults.update(overrides)
    return SearchSpec(**defaults)


def _leg_quote(leg: Leg, price: int, url: str) -> Quote:
    return Quote(
        route_name="布里斯本進雪梨出",
        group="",
        itinerary=f"{leg.origin}>{leg.destination}",
        origin=leg.origin,
        destination=leg.destination,
        depart=leg.date,
        ret=None,
        price=price,
        currency="TWD",
        airlines=("China Airlines",),
        stops=0,
        duration_minutes=300,
        url=url,
    )


def test_search_routes_a_multi_city_spec_to_the_per_leg_split(provider, monkeypatch):
    """fast-flights' own multi-city query mode crashes parsing the response
    (real observed failure: IndexError) -- search() must never reach it."""
    seen = []
    monkeypatch.setattr(
        provider, "_search_multi_as_separate_legs", lambda spec: seen.append(spec) or ["sentinel"]
    )

    result = provider.search(multi_spec())

    assert result == ["sentinel"]
    assert len(seen) == 1


def test_multi_city_splits_into_one_way_legs_and_sums_the_price(provider, monkeypatch):
    def fake_search(spec):
        leg = spec.legs[0]
        assert spec.trip == "oneway", "each leg must be queried as its own one-way search"
        if leg.origin == "TPE":
            return [_leg_quote(leg, 10000, "https://example.invalid/leg1")]
        return [_leg_quote(leg, 8000, "https://example.invalid/leg2")]

    monkeypatch.setattr(provider, "search", fake_search)

    quotes = provider._search_multi_as_separate_legs(multi_spec())

    assert len(quotes) == 1
    quote = quotes[0]
    assert quote.price == 18000, "sum of both legs' cheapest one-way fare"
    assert quote.origin == "TPE" and quote.destination == "SYD", "the spec's overall origin/destination"
    assert quote.depart == date(2027, 6, 5), "the first leg's date"
    assert quote.ret is None, "multi-city has no single return date"
    assert quote.itinerary == multi_spec().itinerary
    assert quote.stops == 0
    assert quote.duration_minutes == 600
    assert "https://example.invalid/leg1" in quote.url
    assert "https://example.invalid/leg2" in quote.url


def test_multi_city_split_collects_airlines_from_every_leg(provider, monkeypatch):
    def fake_search(spec):
        leg = spec.legs[0]
        airline = "China Airlines" if leg.origin == "TPE" else "EVA Air"
        quote = _leg_quote(leg, 10000, "")
        quote.airlines = (airline,)
        return [quote]

    monkeypatch.setattr(provider, "search", fake_search)

    quote = provider._search_multi_as_separate_legs(multi_spec())[0]

    assert quote.airlines == ("China Airlines", "EVA Air")


def test_multi_city_split_propagates_a_failing_legs_error(provider, monkeypatch):
    def fake_search(spec):
        leg = spec.legs[0]
        if leg.origin == "BNE":
            raise ProviderError("fast-flights 查詢失敗: TypeError: boom")
        return [_leg_quote(leg, 10000, "")]

    monkeypatch.setattr(provider, "search", fake_search)

    with pytest.raises(ProviderError, match="BNE>TPE 這段查詢失敗"):
        provider._search_multi_as_separate_legs(multi_spec())


def test_unknown_provider_name_is_rejected():
    with pytest.raises(ProviderError, match="未知的 provider"):
        get_provider("expedia")


def test_serpapi_without_a_key_reports_itself_as_unconfigured(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    provider = get_provider("serpapi")

    with pytest.raises(ProviderError, match="SERPAPI_KEY 未設定"):
        provider.search(round_trip_spec())
