"""Parse and validate ``routes.yaml``.

The YAML is deliberately forgiving about shape -- ``from``/``to`` accept either
a single airport code or a list of them, and dates can be a single day or a
range -- but strict about content, so a typo fails loudly at load time rather
than silently producing zero searches.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import SearchOptions, SeatType, TripType

VALID_SEATS = ("economy", "premium-economy", "business", "first")
VALID_TRIPS = ("round", "oneway", "multi")
DEFAULT_MAX_QUERIES = 60


class ConfigError(ValueError):
    """Raised when routes.yaml is malformed. The message names the offending route."""


@dataclass
class Window:
    """A span of departure dates to scan, plus how long the trip lasts."""

    departs: tuple[date, ...]
    #: Nights until the return flight. ``None`` means one-way.
    nights: int | None = None


@dataclass
class RouteConfig:
    name: str
    origins: tuple[str, ...] = ()
    destinations: tuple[str, ...] = ()
    trip: TripType = "round"
    windows: tuple[Window, ...] = ()
    #: Explicit legs for ``trip: multi``. Each is ``(origin, destination, date)``.
    legs: tuple[tuple[str, str, date], ...] = ()
    exclude: tuple[tuple[str, str], ...] = ()
    compare: bool = False
    #: Comparison group these quotes are ranked in. ``compare: true`` puts a
    #: route in a group of its own; naming a ``group`` explicitly lets several
    #: routes share one, so destinations that need different settings (a looser
    #: max_stops, say) still get ranked against each other.
    group: str = ""
    alert_below: int | None = None
    alert_drop_pct: float | None = None
    options: SearchOptions = field(default_factory=SearchOptions)

    def __post_init__(self) -> None:
        # Resolved here rather than in the YAML parser so a RouteConfig built
        # directly -- in a test, or by the ad-hoc `query` command -- behaves the
        # same as one loaded from disk.
        if not self.group and self.compare:
            self.group = self.name


@dataclass
class Config:
    routes: tuple[RouteConfig, ...]
    currency: str = "TWD"
    max_queries: int = DEFAULT_MAX_QUERIES
    #: Provider names to try in order. The first one that returns quotes wins.
    providers: tuple[str, ...] = ("fast_flights",)
    #: Days of history used as the baseline for ``alert_drop_pct``.
    baseline_days: int = 30


def _as_list(value: Any, field_name: str, route_name: str) -> tuple[str, ...]:
    """Accept either ``TPE`` or ``[TPE, KHH]`` and always return a tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ConfigError(f"route {route_name!r}: {field_name} must be a code or a list of codes")

    codes = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"route {route_name!r}: {field_name} contains a non-string entry: {item!r}")
        codes.append(item.strip().upper())
    return tuple(codes)


def _as_date(value: Any, where: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ConfigError(f"{where}: {value!r} is not a YYYY-MM-DD date") from exc
    raise ConfigError(f"{where}: expected a YYYY-MM-DD date, got {value!r}")


def _parse_window(raw: Any, route_name: str) -> Window:
    if not isinstance(raw, dict):
        raise ConfigError(f"route {route_name!r}: each entry under 'windows' must be a mapping")

    where = f"route {route_name!r} window"
    if "depart_range" in raw:
        span = raw["depart_range"]
        if not isinstance(span, (list, tuple)) or len(span) != 2:
            raise ConfigError(f"{where}: depart_range must be [start, end]")
        start, end = _as_date(span[0], where), _as_date(span[1], where)
        if end < start:
            raise ConfigError(f"{where}: depart_range end {end} is before start {start}")
        departs = tuple(date.fromordinal(o) for o in range(start.toordinal(), end.toordinal() + 1))
    elif "depart" in raw:
        departs = (_as_date(raw["depart"], where),)
    else:
        raise ConfigError(f"{where}: needs either 'depart' or 'depart_range'")

    nights = raw.get("nights")
    if nights is not None:
        if not isinstance(nights, int) or nights < 0:
            raise ConfigError(f"{where}: nights must be a non-negative integer, got {nights!r}")
    return Window(departs=departs, nights=nights)


def _parse_options(raw: dict[str, Any], defaults: SearchOptions, route_name: str) -> SearchOptions:
    seat = raw.get("seat", defaults.seat)
    if seat not in VALID_SEATS:
        raise ConfigError(f"route {route_name!r}: seat must be one of {VALID_SEATS}, got {seat!r}")

    max_stops = raw.get("max_stops", defaults.max_stops)
    if max_stops is not None and (not isinstance(max_stops, int) or max_stops < 0):
        raise ConfigError(f"route {route_name!r}: max_stops must be a non-negative integer or omitted")

    counts = {}
    for key, fallback in (
        ("adults", defaults.adults),
        ("children", defaults.children),
        ("infants_in_seat", defaults.infants_in_seat),
        ("infants_on_lap", defaults.infants_on_lap),
    ):
        value = raw.get(key, fallback)
        if not isinstance(value, int) or value < 0:
            raise ConfigError(f"route {route_name!r}: {key} must be a non-negative integer, got {value!r}")
        counts[key] = value
    if counts["adults"] < 1:
        raise ConfigError(f"route {route_name!r}: adults must be at least 1")

    return SearchOptions(
        seat=seat,
        max_stops=max_stops,
        currency=raw.get("currency", defaults.currency),
        **counts,
    )


def _parse_legs(raw: Any, route_name: str) -> tuple[tuple[str, str, date], ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError(f"route {route_name!r}: trip 'multi' needs a non-empty 'legs' list")
    legs = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"route {route_name!r} leg {index}: must be a mapping with from/to/depart")
        missing = [k for k in ("from", "to", "depart") if k not in item]
        if missing:
            raise ConfigError(f"route {route_name!r} leg {index}: missing {', '.join(missing)}")
        origin = _as_list(item["from"], "from", route_name)
        destination = _as_list(item["to"], "to", route_name)
        if len(origin) != 1 or len(destination) != 1:
            raise ConfigError(
                f"route {route_name!r} leg {index}: multi-city legs take exactly one 'from' and one 'to'"
            )
        legs.append((origin[0], destination[0], _as_date(item["depart"], f"route {route_name!r} leg {index}")))
    return tuple(legs)


def _resolve_group(raw: dict, name: str) -> str:
    """An explicit ``group`` lets several routes be ranked together.

    Left empty, ``RouteConfig`` falls back to the route's own name when
    ``compare: true`` is set.
    """
    return str(raw.get("group") or "")


def _parse_route(raw: Any, index: int, defaults: SearchOptions, currency: str) -> RouteConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"routes[{index}]: each route must be a mapping")
    name = str(raw.get("name") or f"route-{index + 1}")

    trip = raw.get("trip", "round")
    if trip not in VALID_TRIPS:
        raise ConfigError(f"route {name!r}: trip must be one of {VALID_TRIPS}, got {trip!r}")

    options = _parse_options(raw, SearchOptions(**{**defaults.__dict__, "currency": currency}), name)

    alert_below = raw.get("alert_below")
    if alert_below is not None and (not isinstance(alert_below, (int, float)) or alert_below <= 0):
        raise ConfigError(f"route {name!r}: alert_below must be a positive number")
    alert_drop_pct = raw.get("alert_drop_pct")
    if alert_drop_pct is not None and (not isinstance(alert_drop_pct, (int, float)) or not 0 < alert_drop_pct < 100):
        raise ConfigError(f"route {name!r}: alert_drop_pct must be between 0 and 100")

    if trip == "multi":
        route = RouteConfig(
            name=name,
            trip=trip,
            legs=_parse_legs(raw.get("legs"), name),
            compare=bool(raw.get("compare", False)),
            group=_resolve_group(raw, name),
            alert_below=int(alert_below) if alert_below else None,
            alert_drop_pct=float(alert_drop_pct) if alert_drop_pct else None,
            options=options,
        )
        return route

    origins = _as_list(raw.get("from"), "from", name)
    destinations = _as_list(raw.get("to"), "to", name)
    if not origins or not destinations:
        raise ConfigError(f"route {name!r}: needs both 'from' and 'to'")

    windows_raw = raw.get("windows")
    if not isinstance(windows_raw, list) or not windows_raw:
        raise ConfigError(f"route {name!r}: needs a non-empty 'windows' list")
    windows = tuple(_parse_window(w, name) for w in windows_raw)

    if trip == "round":
        missing_nights = [w for w in windows if w.nights is None]
        if missing_nights:
            raise ConfigError(f"route {name!r}: round trips need 'nights' on every window")

    exclude_raw = raw.get("exclude") or []
    if not isinstance(exclude_raw, list):
        raise ConfigError(f"route {name!r}: exclude must be a list of [from, to] pairs")
    exclude = []
    for pair in exclude_raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ConfigError(f"route {name!r}: each exclude entry must be a [from, to] pair, got {pair!r}")
        exclude.append((str(pair[0]).strip().upper(), str(pair[1]).strip().upper()))

    return RouteConfig(
        name=name,
        origins=origins,
        destinations=destinations,
        trip=trip,
        windows=windows,
        exclude=tuple(exclude),
        compare=bool(raw.get("compare", False)),
        group=_resolve_group(raw, name),
        alert_below=int(alert_below) if alert_below else None,
        alert_drop_pct=float(alert_drop_pct) if alert_drop_pct else None,
        options=options,
    )


def load_config(path: str | Path) -> Config:
    """Read ``routes.yaml`` and return a validated :class:`Config`."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    currency = str(raw.get("currency", "TWD")).upper()
    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise ConfigError(f"{path}: 'defaults' must be a mapping")
    defaults = _parse_options(defaults_raw, SearchOptions(currency=currency), "defaults")

    routes_raw = raw.get("routes")
    if not isinstance(routes_raw, list) or not routes_raw:
        raise ConfigError(f"{path}: needs a non-empty 'routes' list")

    routes = tuple(_parse_route(r, i, defaults, currency) for i, r in enumerate(routes_raw))

    max_queries = raw.get("max_queries", DEFAULT_MAX_QUERIES)
    if not isinstance(max_queries, int) or max_queries < 1:
        raise ConfigError(f"{path}: max_queries must be a positive integer")

    providers = _as_list(raw.get("providers", ["fast_flights"]), "providers", "<top level>")
    providers = tuple(p.lower() for p in providers)
    # SerpApi is a paid fallback: opt in simply by setting the key.
    if os.environ.get("SERPAPI_KEY") and "serpapi" not in providers:
        providers += ("serpapi",)

    baseline_days = raw.get("baseline_days", 30)
    if not isinstance(baseline_days, int) or baseline_days < 1:
        raise ConfigError(f"{path}: baseline_days must be a positive integer")

    return Config(
        routes=routes,
        currency=currency,
        max_queries=max_queries,
        providers=providers,
        baseline_days=baseline_days,
    )
