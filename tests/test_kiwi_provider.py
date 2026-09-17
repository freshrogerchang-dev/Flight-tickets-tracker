"""Kiwi provider, tested against a recorded response from the live endpoint.

The fixture is a trimmed copy of a real ``search-flight`` result for
TPE→OOL — the route fast-flights cannot handle — so the mapping is checked
against the shape Kiwi actually returns, not one invented here.

The network layer (`tracker.mcp_client`) is stubbed: what these tests cover is
the request we build and the quotes we derive.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from tracker.mcp_client import McpError
from tracker.models import Leg, SearchOptions, SearchSpec
from tracker.providers import ProviderError, get_provider
from tracker.providers.kiwi_provider import KiwiProvider

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "kiwi_tpe_ool.json").read_text(encoding="utf-8"))


def spec(**overrides) -> SearchSpec:
    defaults = dict(
        legs=(
            Leg("TPE", "OOL", date(2026, 12, 24)),
            Leg("OOL", "TPE", date(2027, 1, 6)),
        ),
        trip="round",
        options=SearchOptions(currency="TWD", max_stops=3),
        route_name="台北-黃金海岸",
        group="台北-澳洲東岸",
    )
    defaults.update(overrides)
    return SearchSpec(**defaults)


@pytest.fixture
def captured(monkeypatch):
    """Record the tool call and answer with the recorded response."""
    calls = []

    def fake_call_tool(url, name, arguments, *, token=None):
        calls.append({"url": url, "name": name, "arguments": arguments})
        return FIXTURE

    monkeypatch.setattr("tracker.providers.kiwi_provider.call_tool", fake_call_tool)
    return calls


# ---------------------------------------------------------------- request


def test_the_request_uses_kiwi_date_format(captured):
    KiwiProvider().search(spec())

    arguments = captured[0]["arguments"]
    assert arguments["departureDate"] == "24/12/2026", "dd/mm/yyyy, not ISO"
    assert arguments["returnDate"] == "06/01/2027"


def test_the_request_carries_route_and_cabin_settings(captured):
    KiwiProvider().search(spec())

    arguments = captured[0]["arguments"]
    assert (arguments["flyFrom"], arguments["flyTo"]) == ("TPE", "OOL")
    assert arguments["currency"] == "TWD"
    assert arguments["cabinClass"] == "M"
    assert arguments["max_sector_stopovers"] == 3
    assert arguments["adults"] == 1
    assert captured[0]["name"] == "search-flight"


def test_a_one_way_search_sends_no_return_date(captured):
    KiwiProvider().search(spec(legs=(Leg("TPE", "OOL", date(2026, 12, 24)),), trip="oneway"))

    assert "returnDate" not in captured[0]["arguments"]


def test_self_transfer_can_be_switched_off(captured):
    KiwiProvider(allow_self_transfer=False).search(spec())

    assert captured[0]["arguments"]["allow_self_transfer"] is False


def test_infants_in_seat_and_on_lap_are_combined(captured):
    KiwiProvider().search(spec(options=SearchOptions(currency="TWD", infants_in_seat=1, infants_on_lap=1)))

    assert captured[0]["arguments"]["infants"] == 2


# ---------------------------------------------------------------- quotes


def test_quotes_come_back_cheapest_first(captured):
    quotes = KiwiProvider().search(spec())

    assert [q.price for q in quotes] == [23645, 24135]


def test_a_quote_carries_the_route_identity_and_booking_link(captured):
    cheapest = KiwiProvider().search(spec())[0]

    assert cheapest.route_name == "台北-黃金海岸"
    assert cheapest.group == "台北-澳洲東岸"
    assert cheapest.itinerary == "TPE>OOL>TPE"
    assert cheapest.currency == "TWD"
    assert cheapest.source == "kiwi"
    assert cheapest.url == "https://kiwi.com/u/bjyx3m"


def test_stops_and_duration_come_from_the_outbound_leg(captured):
    cheapest = KiwiProvider().search(spec())[0]

    assert cheapest.stops == 2
    assert cheapest.duration_minutes == 81000 // 60


def test_airlines_are_named_and_deduplicated_in_order(captured):
    cheapest = KiwiProvider().search(spec())[0]

    assert cheapest.airlines == ("Shenzhen Airlines", "Jetstar Airways")


def test_rows_without_a_price_are_dropped(captured):
    quotes = KiwiProvider().search(spec())

    assert all(q.price > 0 for q in quotes)
    assert len(quotes) == 2, "the null-price row and the wrong-date row are both excluded"


def test_results_departing_on_another_day_are_discarded(captured):
    """Kiwi can answer with nearby dates; storing those would corrupt the history."""
    quotes = KiwiProvider().search(spec())

    assert all(q.depart == date(2026, 12, 24) for q in quotes)
    assert 26730 not in [q.price for q in quotes], "that one departs 12-25"


# ---------------------------------------------------------------- failures


def test_multi_city_is_refused_rather_than_silently_searched(monkeypatch):
    called = []
    monkeypatch.setattr(
        "tracker.providers.kiwi_provider.call_tool",
        lambda *a, **k: called.append(1) or FIXTURE,
    )
    open_jaw = spec(
        legs=(Leg("TPE", "SYD", date(2026, 12, 12)), Leg("OOL", "TPE", date(2026, 12, 24))),
        trip="multi",
    )

    with pytest.raises(ProviderError, match="不支援多段行程"):
        KiwiProvider().search(open_jaw)
    assert called == [], "must not fire a request it cannot express"


def test_an_empty_result_is_a_provider_error(monkeypatch):
    monkeypatch.setattr(
        "tracker.providers.kiwi_provider.call_tool",
        lambda *a, **k: {"currency": "TWD", "itineraries": []},
    )

    with pytest.raises(ProviderError, match="查無航班"):
        KiwiProvider().search(spec())


def test_a_transport_failure_becomes_a_provider_error(monkeypatch):
    """So the runner falls through instead of crashing the whole batch."""

    def boom(*args, **kwargs):
        raise McpError("連線 https://mcp.kiwi.com 失敗: timeout")

    monkeypatch.setattr("tracker.providers.kiwi_provider.call_tool", boom)

    with pytest.raises(ProviderError, match="kiwi 查詢失敗"):
        KiwiProvider().search(spec())


def test_a_prose_answer_is_reported_not_parsed(monkeypatch):
    monkeypatch.setattr(
        "tracker.providers.kiwi_provider.call_tool",
        lambda *a, **k: "Sorry, I could not find flights.",
    )

    with pytest.raises(ProviderError, match="不是結構化結果"):
        KiwiProvider().search(spec())


def test_every_date_filtered_out_is_reported_clearly(monkeypatch):
    monkeypatch.setattr("tracker.providers.kiwi_provider.call_tool", lambda *a, **k: FIXTURE)

    with pytest.raises(ProviderError, match="沒有一筆的出發日"):
        KiwiProvider().search(spec(legs=(Leg("TPE", "OOL", date(2026, 3, 1)),), trip="oneway"))


def test_the_provider_is_reachable_by_name():
    assert get_provider("kiwi").name == "kiwi"
