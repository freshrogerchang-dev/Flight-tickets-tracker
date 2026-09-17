"""Execute a batch of searches: query providers, store results, collect alerts."""

from __future__ import annotations

import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .alerts import AlertState, evaluate
from .config import Config, RouteConfig
from .models import Alert, Quote, SearchSpec
from .providers import ProviderError, get_provider
from .store import PriceStore

#: Seconds to wait between queries. Randomised so a long run does not look like
#: a metronome to Google's rate limiter.
PAUSE_RANGE = (2.0, 4.0)


@dataclass
class RunResult:
    quotes: list[Quote] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    failures: list[tuple[SearchSpec, str]] = field(default_factory=list)
    skipped_repeats: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        searched = len({(q.itinerary, q.date_label) for q in self.quotes})
        parts = [f"查了 {searched} 組行程"]
        parts.append(f"取得 {len(self.quotes)} 筆報價")
        parts.append(f"{len(self.alerts)} 筆達到通知門檻")
        if self.skipped_repeats:
            parts.append(f"{self.skipped_repeats} 筆因近期已通知而略過")
        if self.failures:
            parts.append(f"{len(self.failures)} 組查詢失敗")
        return "，".join(parts)


def _search_with_fallback(spec: SearchSpec, provider_names: tuple[str, ...]) -> tuple[list[Quote], list[str]]:
    """Try each provider in order; first one with quotes wins."""
    errors = []
    for name in provider_names:
        try:
            provider = get_provider(name)
            quotes = provider.search(spec)
            if quotes:
                return quotes, errors
            errors.append(f"{name}: 沒有回傳報價")
        except ProviderError as exc:
            errors.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - an unexpected bug in one provider is not fatal
            errors.append(f"{name}: 未預期的錯誤 {type(exc).__name__}: {exc}")
    return [], errors


def run(
    specs: list[SearchSpec],
    config: Config,
    store: PriceStore,
    state: AlertState,
    *,
    keep_per_search: int = 3,
    pause: bool = True,
    now: datetime | None = None,
    verbose: bool = True,
) -> RunResult:
    """Query every spec, persist the cheapest quotes, and collect alerts.

    Only the cheapest ``keep_per_search`` quotes are stored: Google returns
    dozens of near-identical itineraries per search, and keeping them all would
    bloat the CSV without telling you anything new about the price.
    """
    now = now or datetime.now(timezone.utc)
    routes_by_name: dict[str, RouteConfig] = {r.name: r for r in config.routes}
    result = RunResult()

    for index, spec in enumerate(specs, start=1):
        if verbose:
            print(f"[{index}/{len(specs)}] {spec.route_name}: {spec}", file=sys.stderr)

        quotes, errors = _search_with_fallback(spec, config.providers)
        if not quotes:
            reason = "；".join(errors) or "沒有可用的 provider"
            result.failures.append((spec, reason))
            if verbose:
                print(f"    ✗ {reason}", file=sys.stderr)
        else:
            kept = quotes[:keep_per_search]
            result.quotes.extend(kept)
            cheapest = kept[0]
            if verbose:
                print(
                    f"    ✓ 最低 {cheapest.price:,} {cheapest.currency}"
                    f" · {cheapest.airline_label} · 轉機 {cheapest.stops}",
                    file=sys.stderr,
                )

            route = routes_by_name.get(spec.route_name)
            if route:
                alert = evaluate(cheapest, route, store, baseline_days=config.baseline_days, now=now)
                if alert:
                    if state.should_send(cheapest, now=now):
                        result.alerts.append(alert)
                    else:
                        result.skipped_repeats += 1

        # Pause between queries, but not after the last one.
        if pause and index < len(specs):
            time.sleep(random.uniform(*PAUSE_RANGE))

    # Persist prices after the loop so a crash mid-run still leaves the CSV
    # consistent, and the baseline used above reflects only prior runs.
    store.append(result.quotes)
    return result
