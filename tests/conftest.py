from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from tracker.config import Config, RouteConfig
from tracker.models import Quote, SearchOptions


def make_config(**overrides) -> Config:
    defaults = dict(routes=(), currency="TWD", max_queries=60, providers=("fast_flights",), baseline_days=30)
    defaults.update(overrides)
    return Config(**defaults)


def make_quote(price: int = 10000, **overrides) -> Quote:
    defaults = dict(
        route_name="test",
        group="",
        itinerary="TPE>NRT>TPE",
        origin="TPE",
        destination="NRT",
        depart=date(2026, 12, 20),
        ret=date(2026, 12, 27),
        price=price,
        currency="TWD",
        airlines=("China Airlines",),
        stops=0,
        duration_minutes=185,
        url="https://example.invalid/flight",
        source="test",
        fetched_at=datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Quote(**defaults)


def make_route(**overrides) -> RouteConfig:
    defaults = dict(name="test", origins=("TPE",), destinations=("NRT",), options=SearchOptions())
    defaults.update(overrides)
    return RouteConfig(**defaults)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 17, 1, 0, tzinfo=timezone.utc)


@pytest.fixture
def days_ago(now):
    def _days_ago(count: int) -> datetime:
        return now - timedelta(days=count)

    return _days_ago
