"""Alert thresholds, repeat suppression, and the CSV history they read from."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tracker.alerts import AlertState, evaluate
from tracker.store import PriceStore

from conftest import make_quote, make_route


@pytest.fixture
def store(tmp_path) -> PriceStore:
    return PriceStore(tmp_path / "prices.csv")


# ---------------------------------------------------------------- store


def test_append_writes_a_header_once_then_appends(store):
    store.append([make_quote(10000)])
    store.append([make_quote(9000)])

    text = store.path.read_text(encoding="utf-8")
    assert text.count("fetched_at,route_name") == 1
    assert len(store.rows()) == 2


def test_append_of_nothing_does_not_create_a_file(store):
    assert store.append([]) == 0
    assert not store.path.exists()


def test_rows_on_a_missing_file_is_empty_not_an_error(store):
    assert store.rows() == []


def test_baseline_needs_enough_history(store, now):
    quote = make_quote(10000)
    store.append([make_quote(12000, fetched_at=now - timedelta(days=d)) for d in (1, 2)])

    assert store.baseline(quote, days=30, now=now) is None, "two points is noise, not a baseline"


def test_baseline_is_the_median_of_the_same_itinerary(store, now):
    prices = [12000, 14000, 13000, 11000, 15000]
    store.append([make_quote(p, fetched_at=now - timedelta(days=i + 1)) for i, p in enumerate(prices)])

    assert store.baseline(make_quote(9000), days=30, now=now) == 13000


def test_baseline_is_not_polluted_by_the_other_quotes_from_the_same_run(store, now):
    """The exact false alert seen in production.

    A single run stored 26793 / 32536 / 34423 for one itinerary. Taking the
    median of all three made the cheapest look 18% below "the 30-day median",
    when in fact nothing had moved -- that is just the spread between options
    on one day. Only one run of history exists here, so there is no baseline.
    """
    one_run = now - timedelta(days=1)
    store.append([make_quote(p, fetched_at=one_run) for p in (26793, 32536, 34423)])

    assert store.baseline(make_quote(26793), days=30, now=now) is None


def test_baseline_uses_the_cheapest_of_each_run(store, now):
    """Three runs, each with a spread. Only the cheapest of each should count."""
    for day, prices in enumerate([(30000, 36000, 40000), (28000, 35000, 39000), (32000, 37000, 41000)], start=1):
        stamp = now - timedelta(days=day)
        store.append([make_quote(p, fetched_at=stamp) for p in prices])

    # median(30000, 28000, 32000) -- not median of all nine rows (36000).
    assert store.baseline(make_quote(20000), days=30, now=now) == 30000


def test_a_stable_price_does_not_look_like_a_drop(store, now):
    """Same fare every day: no alert, however wide the daily spread is."""
    for day in range(1, 8):
        stamp = now - timedelta(days=day)
        store.append([make_quote(p, fetched_at=stamp) for p in (26793, 32536, 34423)])
    route = make_route(alert_drop_pct=12)

    assert evaluate(make_quote(26793), route, store, now=now) is None


def test_a_real_drop_still_alerts(store, now):
    """Guard against over-correcting: a genuine fall must still fire."""
    for day in range(1, 8):
        stamp = now - timedelta(days=day)
        store.append([make_quote(p, fetched_at=stamp) for p in (30000, 35000, 38000)])
    route = make_route(alert_drop_pct=12)

    alert = evaluate(make_quote(25000), route, store, now=now)

    assert alert is not None
    assert alert.baseline == 30000


def test_baseline_ignores_rows_outside_the_window(store, now):
    recent = [make_quote(10000, fetched_at=now - timedelta(days=d)) for d in (1, 2, 3)]
    ancient = [make_quote(50000, fetched_at=now - timedelta(days=d)) for d in (100, 200, 300)]
    store.append(recent + ancient)

    assert store.baseline(make_quote(9000), days=30, now=now) == 10000


def test_baseline_ignores_other_itineraries(store, now):
    mine = [make_quote(10000, fetched_at=now - timedelta(days=d)) for d in (1, 2, 3)]
    theirs = [
        make_quote(99000, itinerary="TPE>KIX>TPE", destination="KIX", fetched_at=now - timedelta(days=d))
        for d in (1, 2, 3)
    ]
    store.append(mine + theirs)

    assert store.baseline(make_quote(9000), days=30, now=now) == 10000


def test_baseline_ignores_the_same_route_on_different_dates(store, now):
    """A December fare says nothing about an April fare on the same route."""
    december = [make_quote(10000, fetched_at=now - timedelta(days=d)) for d in (1, 2, 3)]
    store.append(december)

    april = make_quote(9000, depart=date(2027, 4, 1), ret=date(2027, 4, 8))
    assert store.baseline(april, days=30, now=now) is None


def test_cheapest_per_key_keeps_the_lowest_per_itinerary(store, now):
    store.append(
        [
            make_quote(12000, fetched_at=now - timedelta(days=2)),
            make_quote(9000, fetched_at=now - timedelta(days=1)),
            make_quote(11000, itinerary="TPE>KIX>TPE", destination="KIX"),
        ]
    )

    rows = store.cheapest_per_key()

    assert [int(r["price"]) for r in rows] == [9000, 11000]


def test_cheapest_per_pair_collapses_the_date_sweep(store):
    """A compare group answers 'where is cheapest', so each destination appears once."""
    store.append(
        [
            make_quote(12000, group="日本", depart=date(2026, 12, 5), ret=date(2026, 12, 9)),
            make_quote(9000, group="日本", depart=date(2026, 12, 6), ret=date(2026, 12, 10)),
            make_quote(11000, group="日本", itinerary="TPE>KIX>TPE", destination="KIX"),
        ]
    )

    rows = store.cheapest_per_pair(group="日本")

    assert [(r["destination"], int(r["price"])) for r in rows] == [("NRT", 9000), ("KIX", 11000)]


def test_cheapest_per_pair_separates_origins(store):
    store.append(
        [
            make_quote(9000, group="日本"),
            make_quote(8000, group="日本", itinerary="KHH>NRT>KHH", origin="KHH"),
        ]
    )

    rows = store.cheapest_per_pair(group="日本")

    assert {(r["origin"], r["destination"]) for r in rows} == {("TPE", "NRT"), ("KHH", "NRT")}


def test_cheapest_per_key_filters_by_group(store):
    store.append([make_quote(9000, group="日本"), make_quote(8000, itinerary="TPE>ICN>TPE", group="韓國")])

    rows = store.cheapest_per_key(group="日本")

    assert len(rows) == 1
    assert rows[0]["itinerary"] == "TPE>NRT>TPE"


def test_top_n_per_route_keeps_every_route_separate(store):
    """Unlike cheapest_per_pair, a much cheaper route must not crowd out the others."""
    store.append(
        [
            make_quote(9000, route_name="日本"),
            make_quote(30000, route_name="澳洲", itinerary="TPE>SYD>TPE", destination="SYD"),
        ]
    )

    grouped = store.top_n_per_route()

    assert [name for name, _ in grouped] == ["日本", "澳洲"], "cheapest route leads"
    assert {name: [int(r["price"]) for r in rows] for name, rows in grouped} == {
        "日本": [9000],
        "澳洲": [30000],
    }


def test_top_n_per_route_caps_each_route_at_n(store):
    store.append(
        [
            make_quote(9000, route_name="日本", depart=date(2026, 12, 1)),
            make_quote(8000, route_name="日本", depart=date(2026, 12, 2)),
            make_quote(7000, route_name="日本", depart=date(2026, 12, 3)),
            make_quote(6000, route_name="日本", depart=date(2026, 12, 4)),
        ]
    )

    grouped = store.top_n_per_route(n=3)

    assert len(grouped) == 1
    name, rows = grouped[0]
    assert [int(r["price"]) for r in rows] == [6000, 7000, 8000], "the 3 cheapest, cheapest first"


def test_top_n_per_route_respects_since(store, now):
    store.append(
        [
            make_quote(9000, route_name="日本", fetched_at=now - timedelta(days=40)),
            make_quote(8000, route_name="日本", fetched_at=now - timedelta(days=1)),
        ]
    )

    grouped = store.top_n_per_route(since=(now - timedelta(days=30)).date())

    assert len(grouped) == 1
    assert [int(r["price"]) for r in grouped[0][1]] == [8000]


# ---------------------------------------------------------------- evaluate


def test_alert_below_fires_at_or_under_the_threshold(store):
    route = make_route(alert_below=10000)

    assert evaluate(make_quote(9999), route, store) is not None
    assert evaluate(make_quote(10000), route, store) is not None, "threshold is inclusive"
    assert evaluate(make_quote(10001), route, store) is None


def test_drop_pct_fires_against_the_median(store, now):
    store.append([make_quote(10000, fetched_at=now - timedelta(days=d)) for d in (1, 2, 3)])
    route = make_route(alert_drop_pct=15)

    assert evaluate(make_quote(8500), route, store, now=now) is not None, "exactly 15% off"
    assert evaluate(make_quote(8600), route, store, now=now) is None


def test_drop_pct_stays_quiet_without_a_baseline(store, now):
    """The very first run must not alert on every route just because history is empty."""
    route = make_route(alert_drop_pct=15)

    assert evaluate(make_quote(1), route, store, now=now) is None


def test_a_route_with_no_thresholds_never_alerts(store):
    assert evaluate(make_quote(1), make_route(), store) is None


def test_alert_reason_names_the_threshold(store):
    alert = evaluate(make_quote(9000), make_route(alert_below=10000), store)

    assert "10,000" in alert.reason


# ---------------------------------------------------------------- state


def test_first_sighting_is_always_sent(tmp_path, now):
    state = AlertState(tmp_path / "alerts.json")

    assert state.should_send(make_quote(9000), now=now)


def test_repeat_within_the_quiet_window_is_suppressed(tmp_path, now):
    state = AlertState(tmp_path / "alerts.json")
    state.record(make_quote(9000), now=now)

    assert not state.should_send(make_quote(9000), now=now + timedelta(hours=6))


def test_a_further_real_drop_breaks_through_the_quiet_window(tmp_path, now):
    state = AlertState(tmp_path / "alerts.json")
    state.record(make_quote(10000), now=now)

    assert not state.should_send(make_quote(9600), now=now + timedelta(hours=1)), "4% is noise"
    assert state.should_send(make_quote(9500), now=now + timedelta(hours=1)), "5% is news"


def test_the_quiet_window_expires(tmp_path, now):
    state = AlertState(tmp_path / "alerts.json")
    state.record(make_quote(9000), now=now)

    assert state.should_send(make_quote(9000), now=now + timedelta(hours=25))


def test_state_round_trips_through_disk(tmp_path, now):
    path = tmp_path / "alerts.json"
    state = AlertState(path)
    state.record(make_quote(9000), now=now)
    state.save()

    assert not AlertState(path).should_send(make_quote(9000), now=now + timedelta(hours=1))


def test_corrupt_state_file_does_not_stop_the_run(tmp_path, now):
    path = tmp_path / "alerts.json"
    path.write_text("{ this is not json", encoding="utf-8")

    assert AlertState(path).should_send(make_quote(9000), now=now)


def test_prune_drops_stale_entries(tmp_path, now):
    state = AlertState(tmp_path / "alerts.json")
    state.record(make_quote(9000), now=now - timedelta(days=400))
    state.prune(now=now, keep_days=180)

    assert state.should_send(make_quote(9000), now=now)
