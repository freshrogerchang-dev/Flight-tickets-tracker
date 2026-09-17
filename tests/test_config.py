"""Config parsing: forgiving about shape, strict about content."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tracker.config import ConfigError, load_config

MINIMAL = """
currency: TWD
routes:
  - name: 台北-東京
    from: TPE
    to: NRT
    windows:
      - depart: 2026-12-20
        nights: 7
"""


def write(tmp_path, text: str):
    path = tmp_path / "routes.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_config_loads(tmp_path):
    config = load_config(write(tmp_path, MINIMAL))

    assert config.currency == "TWD"
    assert len(config.routes) == 1
    route = config.routes[0]
    assert route.origins == ("TPE",)
    assert route.destinations == ("NRT",)
    assert route.windows[0].departs == (date(2026, 12, 20),)


def test_from_and_to_accept_both_a_string_and_a_list(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
routes:
  - name: multi
    from: [tpe, khh]
    to: nrt
    windows:
      - depart: 2026-12-20
        nights: 7
""",
        )
    )

    route = config.routes[0]
    assert route.origins == ("TPE", "KHH"), "codes are upper-cased"
    assert route.destinations == ("NRT",)


def test_depart_range_expands_inclusively(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
routes:
  - name: range
    from: TPE
    to: NRT
    windows:
      - depart_range: [2026-11-01, 2026-11-05]
        nights: 3
""",
        )
    )

    departs = config.routes[0].windows[0].departs
    assert len(departs) == 5
    assert departs[0] == date(2026, 11, 1)
    assert departs[-1] == date(2026, 11, 5)


def test_defaults_are_inherited_and_overridable(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
defaults:
  adults: 2
  seat: business
  max_stops: 0
routes:
  - name: inherits
    from: TPE
    to: NRT
    windows: [{depart: 2026-12-20, nights: 7}]
  - name: overrides
    from: TPE
    to: NRT
    seat: economy
    windows: [{depart: 2026-12-20, nights: 7}]
""",
        )
    )

    assert config.routes[0].options.seat == "business"
    assert config.routes[0].options.adults == 2
    assert config.routes[1].options.seat == "economy"
    assert config.routes[1].options.adults == 2, "unset fields still inherit"


def test_compare_puts_a_route_in_a_group_named_after_itself(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
routes:
  - name: 日本隨便飛
    from: TPE
    to: [NRT, KIX]
    compare: true
    windows: [{depart: 2026-12-20, nights: 7}]
""",
        )
    )

    assert config.routes[0].group == "日本隨便飛"


def test_routes_without_compare_are_ungrouped(tmp_path):
    assert load_config(write(tmp_path, MINIMAL)).routes[0].group == ""


def test_separate_routes_can_share_one_comparison_group(tmp_path):
    """Destinations needing different settings must still rank against each other."""
    config = load_config(
        write(
            tmp_path,
            """
routes:
  - name: 台北-雪梨
    group: 台北-澳洲東岸
    from: TPE
    to: SYD
    max_stops: 2
    windows: [{depart: 2026-12-20, nights: 12}]
  - name: 台北-黃金海岸
    group: 台北-澳洲東岸
    from: TPE
    to: OOL
    max_stops: 3
    windows: [{depart: 2026-12-20, nights: 12}]
""",
        )
    )

    assert [r.group for r in config.routes] == ["台北-澳洲東岸", "台北-澳洲東岸"]
    assert [r.options.max_stops for r in config.routes] == [2, 3], "each keeps its own limit"


def test_an_explicit_group_overrides_the_compare_default(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
routes:
  - name: 台北-雪梨
    group: 澳洲
    compare: true
    from: TPE
    to: SYD
    windows: [{depart: 2026-12-20, nights: 12}]
""",
        )
    )

    assert config.routes[0].group == "澳洲"


def test_serpapi_is_added_as_fallback_when_the_key_is_present(tmp_path, monkeypatch):
    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    config = load_config(write(tmp_path, MINIMAL))

    assert config.providers == ("fast_flights", "serpapi")


def test_serpapi_is_absent_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    config = load_config(write(tmp_path, MINIMAL))

    assert config.providers == ("fast_flights",)


@pytest.mark.parametrize(
    "body, expected",
    [
        ("routes: []", "non-empty 'routes'"),
        ("routes:\n  - {name: x, to: NRT, windows: [{depart: 2026-12-20, nights: 7}]}", "needs both 'from' and 'to'"),
        ("routes:\n  - {name: x, from: TPE, to: NRT}", "non-empty 'windows'"),
        ("routes:\n  - {name: x, from: TPE, to: NRT, windows: [{depart: 2026-12-20}]}", "round trips need 'nights'"),
        ("routes:\n  - {name: x, from: TPE, to: NRT, trip: teleport, windows: [{depart: 2026-12-20, nights: 1}]}", "trip must be one of"),
        ("routes:\n  - {name: x, from: TPE, to: NRT, seat: couch, windows: [{depart: 2026-12-20, nights: 1}]}", "seat must be one of"),
        ("routes:\n  - {name: x, from: TPE, to: NRT, windows: [{depart: nope, nights: 1}]}", "not a YYYY-MM-DD date"),
        ("routes:\n  - {name: x, from: TPE, to: NRT, windows: [{depart_range: [2026-12-20, 2026-12-01], nights: 1}]}", "is before start"),
        ("routes:\n  - {name: x, trip: multi}", "needs a non-empty 'legs'"),
        ("routes:\n  - {name: x, from: TPE, to: NRT, alert_drop_pct: 150, windows: [{depart: 2026-12-20, nights: 1}]}", "between 0 and 100"),
    ],
)
def test_bad_config_fails_loudly_with_a_useful_message(tmp_path, body, expected):
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, body))

    assert expected in str(excinfo.value)


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_multi_city_legs_parse(tmp_path):
    config = load_config(
        write(
            tmp_path,
            """
routes:
  - name: 東京進大阪出
    trip: multi
    legs:
      - {from: TPE, to: NRT, depart: 2027-01-10}
      - {from: KIX, to: TPE, depart: 2027-01-17}
""",
        )
    )

    assert config.routes[0].legs == (
        ("TPE", "NRT", date(2027, 1, 10)),
        ("KIX", "TPE", date(2027, 1, 17)),
    )


def test_shipped_config_is_valid_and_expands():
    """The committed routes.yaml must load and stay inside its own query budget.

    Deliberately asserts on properties rather than specific routes: this file is
    meant to be edited, and a test that pins the destinations would fail every
    time someone changes where they want to fly.
    """
    from tracker.expand import expand_all

    config = load_config(Path(__file__).parent.parent / "routes.yaml")

    assert config.routes, "routes.yaml ships with at least one route"
    specs = expand_all(config)  # raises TooManyQueries if it is over budget
    assert specs
    assert all(spec.legs for spec in specs)
