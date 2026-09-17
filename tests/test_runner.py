"""End-to-end run with a stubbed provider: no network, real store and alert logic."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from tracker.alerts import AlertState
from tracker.config import RouteConfig, Window
from tracker.models import SearchOptions
from tracker.expand import expand_route
from tracker.providers import ProviderError
from tracker.runner import run as run_searches
from tracker.store import PriceStore

from conftest import make_config, make_quote


class StubProvider:
    """Returns a scripted price per search, or raises."""

    def __init__(self, name: str, prices: list[int] | None = None, error: str | None = None):
        self.name = name
        self.prices = prices or []
        self.error = error
        self.calls = 0

    def search(self, spec):
        self.calls += 1
        if self.error:
            raise ProviderError(self.error)
        return [
            make_quote(
                price,
                route_name=spec.route_name,
                group=spec.group,
                itinerary=spec.itinerary,
                origin=spec.origin,
                destination=spec.destination,
                depart=spec.depart,
                ret=spec.ret,
                source=self.name,
            )
            for price in sorted(self.prices)
        ]


@pytest.fixture
def route() -> RouteConfig:
    return RouteConfig(
        name="台北-東京",
        origins=("TPE",),
        destinations=("NRT",),
        trip="round",
        windows=(Window(departs=(date(2030, 12, 20),), nights=7),),
        alert_below=10000,
        options=SearchOptions(),
    )


@pytest.fixture
def pieces(tmp_path):
    return PriceStore(tmp_path / "prices.csv"), AlertState(tmp_path / "alerts.json")


def install(monkeypatch, **providers):
    monkeypatch.setattr("tracker.runner.get_provider", lambda name: providers[name])


def test_a_cheap_fare_is_stored_and_alerted(monkeypatch, route, pieces, now):
    store, state = pieces
    install(monkeypatch, fast_flights=StubProvider("fast_flights", [9000, 9500, 11000]))
    config = make_config(routes=(route,))

    result = run_searches(expand_route(route), config, store, state, pause=False, now=now, verbose=False)

    assert len(result.alerts) == 1
    assert result.alerts[0].quote.price == 9000, "the alert is about the cheapest quote"
    assert len(store.rows()) == 3, "top 3 quotes are kept as history"
    assert result.ok


def test_an_expensive_fare_is_stored_without_alerting(monkeypatch, route, pieces, now):
    store, state = pieces
    install(monkeypatch, fast_flights=StubProvider("fast_flights", [20000]))

    result = run_searches(expand_route(route), make_config(routes=(route,)), store, state, pause=False, now=now, verbose=False)

    assert result.alerts == []
    assert len(store.rows()) == 1


def test_keep_per_search_limits_what_is_written(monkeypatch, route, pieces, now):
    store, state = pieces
    install(monkeypatch, fast_flights=StubProvider("fast_flights", list(range(9000, 9020))))

    run_searches(
        expand_route(route), make_config(routes=(route,)), store, state,
        pause=False, now=now, verbose=False, keep_per_search=2,
    )

    assert len(store.rows()) == 2


def test_serpapi_takes_over_when_fast_flights_fails(monkeypatch, route, pieces, now):
    store, state = pieces
    primary = StubProvider("fast_flights", error="Google 擋掉了")
    fallback = StubProvider("serpapi", [9000])
    install(monkeypatch, fast_flights=primary, serpapi=fallback)
    config = make_config(routes=(route,), providers=("fast_flights", "serpapi"))

    result = run_searches(expand_route(route), config, store, state, pause=False, now=now, verbose=False)

    assert primary.calls == 1 and fallback.calls == 1
    assert result.ok
    assert store.rows()[0]["source"] == "serpapi"


def test_the_fallback_is_not_called_when_the_primary_works(monkeypatch, route, pieces, now):
    store, state = pieces
    fallback = StubProvider("serpapi", [1])
    install(monkeypatch, fast_flights=StubProvider("fast_flights", [9000]), serpapi=fallback)
    config = make_config(routes=(route,), providers=("fast_flights", "serpapi"))

    run_searches(expand_route(route), config, store, state, pause=False, now=now, verbose=False)

    assert fallback.calls == 0, "SerpApi costs money per search"


def test_every_provider_failing_is_recorded_not_raised(monkeypatch, route, pieces, now):
    store, state = pieces
    install(monkeypatch, fast_flights=StubProvider("fast_flights", error="壞掉了"))

    result = run_searches(expand_route(route), make_config(routes=(route,)), store, state, pause=False, now=now, verbose=False)

    assert not result.ok
    assert len(result.failures) == 1
    assert "壞掉了" in result.failures[0][1]
    assert store.rows() == []


def test_one_failing_search_does_not_abort_the_rest(monkeypatch, pieces, now):
    store, state = pieces
    multi = RouteConfig(
        name="多點",
        origins=("TPE",),
        destinations=("NRT", "KIX", "FUK"),
        trip="round",
        windows=(Window(departs=(date(2030, 12, 20),), nights=7),),
        alert_below=10000,
        options=SearchOptions(),
    )

    class Flaky(StubProvider):
        def search(self, spec):
            if spec.destination == "KIX":
                raise ProviderError("查無航班")
            return super().search(spec)

    install(monkeypatch, fast_flights=Flaky("fast_flights", [9000]))

    result = run_searches(expand_route(multi), make_config(routes=(multi,)), store, state, pause=False, now=now, verbose=False)

    assert len(result.failures) == 1
    assert len(result.alerts) == 2, "NRT and FUK still alerted"


def test_a_repeat_alert_is_counted_but_not_resent(monkeypatch, route, pieces, now):
    store, state = pieces
    install(monkeypatch, fast_flights=StubProvider("fast_flights", [9000]))
    config = make_config(routes=(route,))
    specs = expand_route(route)

    first = run_searches(specs, config, store, state, pause=False, now=now, verbose=False)
    for alert in first.alerts:
        state.record(alert.quote, now=now)

    second = run_searches(specs, config, store, state, pause=False, now=now + timedelta(hours=2), verbose=False)

    assert second.alerts == []
    assert second.skipped_repeats == 1
    assert len(store.rows()) == 2, "prices are still recorded even when the alert is suppressed"


def test_the_baseline_excludes_this_run_s_own_price(monkeypatch, pieces, now):
    """Appending before evaluating would let a quote drag its own baseline down."""
    store, state = pieces
    drop_route = RouteConfig(
        name="跌價",
        origins=("TPE",),
        destinations=("NRT",),
        trip="round",
        windows=(Window(departs=(date(2030, 12, 20),), nights=7),),
        alert_drop_pct=20,
        options=SearchOptions(),
    )
    specs = expand_route(drop_route)
    store.append([make_quote(10000, itinerary=specs[0].itinerary, depart=specs[0].depart, ret=specs[0].ret,
                             fetched_at=now - timedelta(days=d)) for d in (1, 2, 3)])

    install(monkeypatch, fast_flights=StubProvider("fast_flights", [7000]))
    result = run_searches(specs, make_config(routes=(drop_route,)), store, state, pause=False, now=now, verbose=False)

    assert len(result.alerts) == 1
    assert result.alerts[0].baseline == 10000


def test_a_total_wipeout_is_pushed_to_the_notification_channels(monkeypatch, tmp_path):
    """A tracker that silently stops working looks just like one finding nothing cheap."""
    from tracker import cli

    sent = []
    monkeypatch.setattr(cli, "available_notifiers", lambda: [type("N", (), {"name": "ntfy"})()])
    monkeypatch.setattr(
        cli,
        "deliver",
        lambda notifiers, subject, body: sent.append((subject, body))
        or [type("D", (), {"channel": "ntfy", "ok": True, "detail": ""})()],
    )

    from tracker.runner import RunResult

    result = RunResult()
    result.failures = [(object(), "fast-flights 查詢失敗: KeyError: 'price'")] * 5

    cli._warn_tracker_is_broken(result)

    assert len(sent) == 1
    subject, body = sent[0]
    assert "查不到任何票價" in subject
    assert "5 組查詢全部失敗" in body
    assert "另外還有 2 筆" in body, "long failure lists are truncated, not dumped whole"


def test_no_configured_channel_means_no_failure_push(monkeypatch):
    from tracker import cli
    from tracker.runner import RunResult

    monkeypatch.setattr(cli, "available_notifiers", lambda: [])
    monkeypatch.setattr(cli, "deliver", lambda *a: pytest.fail("must not try to deliver"))

    result = RunResult()
    result.failures = [(object(), "boom")]

    cli._warn_tracker_is_broken(result)  # must not raise


def test_two_routes_sharing_a_group_are_ranked_together(monkeypatch, pieces, now):
    """The point of `group:` -- SYD at 2 stops and OOL at 3 still compare directly."""
    store, state = pieces
    common = dict(
        trip="round",
        group="台北-澳洲東岸",
        windows=(Window(departs=(date(2030, 12, 20),), nights=12),),
    )
    syd = RouteConfig(name="台北-雪梨", origins=("TPE",), destinations=("SYD",),
                      options=SearchOptions(max_stops=2), **common)
    ool = RouteConfig(name="台北-黃金海岸", origins=("TPE",), destinations=("OOL",),
                      options=SearchOptions(max_stops=3), **common)

    class PerDestination(StubProvider):
        def search(self, spec):
            self.prices = {"SYD": [26000], "OOL": [21000]}[spec.destination]
            return super().search(spec)

    install(monkeypatch, fast_flights=PerDestination("fast_flights"))
    config = make_config(routes=(syd, ool))
    specs = expand_route(syd) + expand_route(ool)
    run_searches(specs, config, store, state, pause=False, now=now, verbose=False)

    ranked = store.cheapest_per_pair(group="台北-澳洲東岸")

    assert [r["destination"] for r in ranked] == ["OOL", "SYD"]
    assert len(ranked) == 2, "both routes land in the one group"


def test_report_ranks_a_comparison_group(monkeypatch, pieces, now):
    store, state = pieces
    group_route = RouteConfig(
        name="日本隨便飛",
        origins=("TPE",),
        destinations=("NRT", "KIX", "FUK"),
        trip="round",
        compare=True,
        windows=(Window(departs=(date(2030, 12, 20),), nights=4),),
        options=SearchOptions(),
    )

    class PerDestination(StubProvider):
        def search(self, spec):
            self.prices = {"NRT": [12000], "KIX": [9000], "FUK": [15000]}[spec.destination]
            return super().search(spec)

    install(monkeypatch, fast_flights=PerDestination("fast_flights"))
    run_searches(expand_route(group_route), make_config(routes=(group_route,)), store, state,
                 pause=False, now=now, verbose=False)

    ranked = store.cheapest_per_key(group="日本隨便飛")

    assert [r["destination"] for r in ranked] == ["KIX", "NRT", "FUK"]
    assert int(ranked[0]["price"]) == 9000
