"""Optional paid fallback: SerpApi's Google Flights endpoint.

Enabled automatically when ``SERPAPI_KEY`` is set. It costs money per search,
so it is only reached after fast-flights has already failed -- which is exactly
when it earns its keep, since it is officially supported and will not break
when Google reshuffles its markup.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests

from ..models import Quote, SearchSpec
from . import ProviderError

ENDPOINT = "https://serpapi.com/search"
TIMEOUT_SECONDS = 45

#: SerpApi's `type` parameter: 1 = round trip, 2 = one way, 3 = multi-city.
TRIP_CODES = {"round": 1, "oneway": 2, "multi": 3}

TRAVEL_CLASS = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}


class SerpApiProvider:
    name = "serpapi"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SERPAPI_KEY", "")

    def _params(self, spec: SearchSpec) -> dict:
        options = spec.options
        params = {
            "engine": "google_flights",
            "api_key": self.api_key,
            "type": TRIP_CODES[spec.trip],
            "travel_class": TRAVEL_CLASS[options.seat],
            "adults": options.adults,
            "children": options.children,
            "infants_in_seat": options.infants_in_seat,
            "infants_on_lap": options.infants_on_lap,
            "currency": options.currency,
            "hl": "en",
        }
        if options.max_stops is not None:
            # SerpApi: 0 = any, 1 = nonstop, 2 = <=1 stop, 3 = <=2 stops.
            params["stops"] = min(options.max_stops + 1, 3)

        if spec.trip == "multi":
            params["multi_city_json"] = json_dumps_legs(spec)
        else:
            params["departure_id"] = spec.origin
            params["arrival_id"] = spec.destination
            params["outbound_date"] = spec.depart.isoformat()
            if spec.ret:
                params["return_date"] = spec.ret.isoformat()
        return params

    def search(self, spec: SearchSpec) -> list[Quote]:
        if not self.api_key:
            raise ProviderError("SERPAPI_KEY 未設定，略過 serpapi provider")

        try:
            response = requests.get(ENDPOINT, params=self._params(spec), timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise ProviderError(f"SerpApi 請求失敗: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"SerpApi 回傳的不是 JSON: {exc}") from exc

        if payload.get("error"):
            raise ProviderError(f"SerpApi 錯誤: {payload['error']}")

        fetched_at = datetime.now(timezone.utc)
        url = payload.get("search_metadata", {}).get("google_flights_url", "")

        raw = list(payload.get("best_flights") or []) + list(payload.get("other_flights") or [])
        quotes = [q for q in (self._to_quote(item, spec, url, fetched_at) for item in raw) if q]
        if not quotes:
            raise ProviderError(f"SerpApi 沒有回傳票價: {spec}")

        quotes.sort(key=lambda q: q.price)
        return quotes

    def _to_quote(self, item: dict, spec: SearchSpec, url: str, fetched_at: datetime) -> Quote | None:
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return None

        segments = item.get("flights") or []
        airlines = tuple(dict.fromkeys(s.get("airline", "") for s in segments if s.get("airline")))

        return Quote(
            route_name=spec.route_name,
            group=spec.group,
            itinerary=spec.itinerary,
            origin=spec.origin,
            destination=spec.destination,
            depart=spec.depart,
            ret=spec.ret,
            price=int(price),
            currency=spec.options.currency,
            airlines=airlines,
            # SerpApi reports layovers for the whole priced itinerary; on a
            # round trip that includes the return, unlike the outbound-only
            # count fast-flights gives us. Recorded as-is rather than guessed at.
            stops=len(item.get("layovers") or []),
            duration_minutes=int(item.get("total_duration") or 0),
            url=url,
            source=self.name,
            fetched_at=fetched_at,
        )


def json_dumps_legs(spec: SearchSpec) -> str:
    """Serialise multi-city legs the way SerpApi's ``multi_city_json`` expects."""
    return json.dumps(
        [
            {
                "departure_id": leg.origin,
                "arrival_id": leg.destination,
                "date": leg.date.isoformat(),
            }
            for leg in spec.legs
        ]
    )
