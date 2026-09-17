"""Expansion is pure arithmetic over the config, so it is tested exhaustively.

Getting this wrong is expensive in a way the other modules are not: an
off-by-one here means either silently skipping routes or firing hundreds of
queries at Google and getting the IP blocked.
"""

from __future__ import annotations

from datetime import date

import pytest

from tracker.config import RouteConfig, Window
from tracker.expand import TooManyQueries, drop_past, expand_all, expand_route, summarise
from tracker.models import SearchOptions

from conftest import make_config


def simple_route(**overrides) -> RouteConfig:
    defaults = dict(
        name="test",
        origins=("TPE",),
        destinations=("NRT",),
        trip="round",
        windows=(Window(departs=(date(2026, 12, 20),), nights=7),),
        options=SearchOptions(),
    )
    defaults.update(overrides)
    return RouteConfig(**defaults)


def test_single_pair_round_trip_builds_two_legs():
    specs = expand_route(simple_route())

    assert len(specs) == 1
    spec = specs[0]
    assert spec.trip == "round"
    assert [(leg.origin, leg.destination, leg.date) for leg in spec.legs] == [
        ("TPE", "NRT", date(2026, 12, 20)),
        ("NRT", "TPE", date(2026, 12, 27)),
    ]
    assert spec.ret == date(2026, 12, 27)


def test_oneway_has_a_single_leg_and_no_return():
    specs = expand_route(simple_route(trip="oneway", windows=(Window(departs=(date(2026, 12, 20),)),)))

    assert len(specs[0].legs) == 1
    assert specs[0].ret is None


def test_many_to_many_expands_cartesian():
    specs = expand_route(simple_route(origins=("TPE", "KHH"), destinations=("NRT", "KIX", "FUK")))

    assert len(specs) == 6
    assert {(s.origin, s.destination) for s in specs} == {
        ("TPE", "NRT"), ("TPE", "KIX"), ("TPE", "FUK"),
        ("KHH", "NRT"), ("KHH", "KIX"), ("KHH", "FUK"),
    }


def test_exclude_removes_specific_pairs():
    specs = expand_route(
        simple_route(
            origins=("TPE", "KHH"),
            destinations=("NRT", "KIX"),
            exclude=(("KHH", "KIX"),),
        )
    )

    assert len(specs) == 3
    assert ("KHH", "KIX") not in {(s.origin, s.destination) for s in specs}


def test_same_origin_and_destination_is_skipped():
    specs = expand_route(simple_route(origins=("TPE", "NRT"), destinations=("NRT",)))

    assert len(specs) == 1
    assert specs[0].origin == "TPE"


def test_date_range_expands_one_search_per_day():
    window = Window(departs=tuple(date(2026, 11, d) for d in range(1, 31)), nights=5)
    specs = expand_route(simple_route(windows=(window,)))

    assert len(specs) == 30
    assert specs[0].depart == date(2026, 11, 1)
    assert specs[-1].depart == date(2026, 11, 30)
    assert specs[-1].ret == date(2026, 12, 5)


def test_overlapping_windows_do_not_produce_duplicate_searches():
    overlapping = (
        Window(departs=(date(2026, 12, 20), date(2026, 12, 21)), nights=7),
        Window(departs=(date(2026, 12, 21), date(2026, 12, 22)), nights=7),
    )
    specs = expand_route(simple_route(windows=overlapping))

    assert len(specs) == 3
    assert len({s.key for s in specs}) == 3


def test_compare_flag_tags_specs_with_the_group_name():
    tagged = expand_route(simple_route(name="日本隨便飛", compare=True))
    untagged = expand_route(simple_route(name="日本隨便飛", compare=False))

    assert tagged[0].group == "日本隨便飛"
    assert untagged[0].group == ""


def test_multi_city_keeps_legs_verbatim():
    route = RouteConfig(
        name="東京進大阪出",
        trip="multi",
        legs=(
            ("TPE", "NRT", date(2027, 1, 10)),
            ("KIX", "TPE", date(2027, 1, 17)),
        ),
        options=SearchOptions(),
    )

    specs = expand_route(route)

    assert len(specs) == 1
    assert specs[0].trip == "multi"
    assert specs[0].ret is None, "multi-city has no single return date"
    # Non-contiguous legs must stay visible rather than collapsing to TPE>NRT>TPE.
    assert specs[0].itinerary == "TPE>NRT / KIX>TPE"


def test_expand_all_raises_when_over_budget_and_names_the_worst_route():
    big = simple_route(
        name="爆量路線",
        origins=("TPE", "KHH"),
        destinations=("NRT", "KIX", "FUK"),
        windows=(Window(departs=tuple(date(2026, 11, d) for d in range(1, 31)), nights=5),),
    )
    config = make_config(routes=(simple_route(), big), max_queries=60)

    with pytest.raises(TooManyQueries) as excinfo:
        expand_all(config)

    assert excinfo.value.wanted == 181
    assert excinfo.value.limit == 60
    assert excinfo.value.worst_offender == "爆量路線"


def test_expand_all_allows_skipping_the_budget_check():
    big = simple_route(origins=("TPE", "KHH"), destinations=("NRT", "KIX", "FUK"))
    specs = expand_all(make_config(routes=(big,), max_queries=1), enforce_limit=False)

    assert len(specs) == 6


def test_drop_past_splits_on_departure_date():
    past = simple_route(windows=(Window(departs=(date(2020, 1, 1),), nights=7),))
    future = simple_route(windows=(Window(departs=(date(2030, 1, 1),), nights=7),))
    specs = expand_route(past) + expand_route(future)

    still_bookable, departed = drop_past(specs, today=date(2026, 9, 17))

    assert len(still_bookable) == 1
    assert len(departed) == 1
    assert still_bookable[0].depart == date(2030, 1, 1)


def test_summarise_reports_counts_per_route():
    specs = expand_route(simple_route(name="日本", origins=("TPE",), destinations=("NRT", "KIX")))

    text = summarise(specs)

    assert "共 2 次查詢" in text
    assert "日本: 2 次" in text


def test_summarise_handles_no_searches():
    assert "沒有任何查詢" in summarise([])
