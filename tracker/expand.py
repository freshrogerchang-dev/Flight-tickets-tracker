"""Turn route configuration into a flat list of concrete searches.

This is where "multiple points" becomes real work: a route with 2 origins,
3 destinations and a 30-day departure range is 180 separate searches. Keeping
the expansion a pure function makes that arithmetic testable without touching
the network, and lets ``--dry-run`` show exactly what a run would do.
"""

from __future__ import annotations

from datetime import date, timedelta

from .config import Config, RouteConfig
from .models import Leg, SearchSpec


class TooManyQueries(RuntimeError):
    """Raised when expansion exceeds ``max_queries``.

    Carries the numbers so the CLI can tell the user what to shrink instead of
    just refusing.
    """

    def __init__(self, wanted: int, limit: int, worst_offender: str | None = None):
        self.wanted = wanted
        self.limit = limit
        self.worst_offender = worst_offender
        message = (
            f"這次會產生 {wanted} 次查詢，超過上限 {limit}。"
            " 請縮短日期區間、減少目的地，或調高 routes.yaml 的 max_queries。"
        )
        if worst_offender:
            message += f" 最大宗是路線 {worst_offender!r}。"
        super().__init__(message)


def expand_route(route: RouteConfig) -> list[SearchSpec]:
    """Expand one route into every (origin, destination, departure date) search.

    Ordering is deterministic -- origins outermost, then destinations, then
    departure dates -- so a dry run and the real run visit searches in the same
    order, and tests can assert on the list directly.
    """
    group = route.name if route.compare else ""

    if route.trip == "multi":
        legs = tuple(Leg(origin=o, destination=d, date=dt) for o, d, dt in route.legs)
        return [
            SearchSpec(
                legs=legs,
                trip="multi",
                options=route.options,
                route_name=route.name,
                group=group,
            )
        ]

    excluded = set(route.exclude)
    specs: list[SearchSpec] = []
    seen: set[str] = set()

    for origin in route.origins:
        for destination in route.destinations:
            if origin == destination or (origin, destination) in excluded:
                continue
            for window in route.windows:
                for depart in window.departs:
                    legs = [Leg(origin=origin, destination=destination, date=depart)]
                    if route.trip == "round":
                        # ``nights`` is guaranteed non-None for round trips by config validation.
                        return_date = depart + timedelta(days=window.nights or 0)
                        legs.append(Leg(origin=destination, destination=origin, date=return_date))

                    spec = SearchSpec(
                        legs=tuple(legs),
                        trip=route.trip,
                        options=route.options,
                        route_name=route.name,
                        group=group,
                    )
                    # Overlapping windows can produce the same search twice;
                    # querying it twice would just burn rate limit.
                    if spec.key in seen:
                        continue
                    seen.add(spec.key)
                    specs.append(spec)

    return specs


def expand_all(config: Config, *, enforce_limit: bool = True) -> list[SearchSpec]:
    """Expand every route in the config, enforcing the query budget."""
    per_route = {route.name: expand_route(route) for route in config.routes}
    specs = [spec for route in config.routes for spec in per_route[route.name]]

    if enforce_limit and len(specs) > config.max_queries:
        worst = max(per_route, key=lambda name: len(per_route[name])) if per_route else None
        raise TooManyQueries(len(specs), config.max_queries, worst)

    return specs


def drop_past(specs: list[SearchSpec], *, today: date | None = None) -> tuple[list[SearchSpec], list[SearchSpec]]:
    """Split specs into (still bookable, already departed).

    A config left untouched for a few months would otherwise keep querying dates
    in the past every single day, wasting the whole query budget on itineraries
    nobody can buy.
    """
    today = today or date.today()
    future, past = [], []
    for spec in specs:
        (past if spec.depart < today else future).append(spec)
    return future, past


def summarise(specs: list[SearchSpec]) -> str:
    """One-line-per-route summary used by ``--dry-run`` and the Actions log."""
    if not specs:
        return "沒有任何查詢（檢查 routes.yaml 的日期是不是都過期了）"

    by_route: dict[str, list[SearchSpec]] = {}
    for spec in specs:
        by_route.setdefault(spec.route_name, []).append(spec)

    lines = [f"共 {len(specs)} 次查詢："]
    for name, group in by_route.items():
        pairs = sorted({f"{s.origin}>{s.destination}" for s in group})
        dates = sorted({s.depart for s in group})
        span = dates[0].isoformat() if len(dates) == 1 else f"{dates[0].isoformat()}~{dates[-1].isoformat()}"
        lines.append(f"  {name}: {len(group)} 次  [{', '.join(pairs)}]  出發 {span}")
    return "\n".join(lines)
