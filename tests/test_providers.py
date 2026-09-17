"""Provider result mapping, tested against recorded shapes instead of the network.

fast-flights is an unofficial scraper, so the mapping from its objects to our
Quote is exactly where a Google change would show up. These fakes mirror the
real dataclasses (fast_flights.model.Flights / SingleFlight / Airport).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from tracker.models import Leg, SearchOptions, SearchSpec
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


def test_unknown_provider_name_is_rejected():
    with pytest.raises(ProviderError, match="未知的 provider"):
        get_provider("expedia")


def test_serpapi_without_a_key_reports_itself_as_unconfigured(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    provider = get_provider("serpapi")

    with pytest.raises(ProviderError, match="SERPAPI_KEY 未設定"):
        provider.search(round_trip_spec())
