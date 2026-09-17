"""Kiwi.com as a price source, reached through its public MCP endpoint.

Added because fast-flights returns nothing at all for some routes -- Taipei to
the Gold Coast being the case that prompted it, where every query died inside
fast-flights while Kiwi found fifteen itineraries for the same dates. Kiwi also
covers budget carriers and self-transfer combinations that Google does not sell.

No key needed: ``https://mcp.kiwi.com`` is authless. Because it sits after
fast-flights in the provider list, it is only reached for searches that already
failed, so routes that work today keep using Google's prices.

By default this provider asks Kiwi to **exclude self-transfer** itineraries --
separate tickets stitched together, where a missed connection is your own
problem to solve and pay for. That risk does not shrink with the fare, so it
stays off unless a caller explicitly accepts it.

Connections that change airports within the same city (the TPE>OOL result that
landed at MEL but continued from AVV, an hour's drive away) are **allowed** by
default: it costs an hour and some planning, not a second ticket, and for
routes with few single-ticket options at all -- Gold Coast among them -- ruling
it out can mean no usable fare ever clears an alert threshold. Turn it off per
call if that trade is not acceptable for a given route.

Baggage is not assumed either way: the cheapest fares here routinely include a
cabin bag only, so the price is not directly comparable to a fare with checked
baggage.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from ..mcp_client import McpError, call_tool
from ..models import Quote, SearchSpec
from . import ProviderError

DEFAULT_ENDPOINT = "https://mcp.kiwi.com"
TOOL_NAME = "search-flight"

#: Kiwi's cabin codes.
CABIN_CLASS = {"economy": "M", "premium-economy": "W", "business": "C", "first": "F"}


def _as_kiwi_date(value: date) -> str:
    """Kiwi takes dd/mm/yyyy, not ISO."""
    return value.strftime("%d/%m/%Y")


class KiwiProvider:
    name = "kiwi"

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        token: str | None = None,
        allow_self_transfer: bool = False,
        allow_diff_airport_connection: bool = True,
    ):
        self.endpoint = endpoint
        self.token = token
        self.allow_self_transfer = allow_self_transfer
        self.allow_diff_airport_connection = allow_diff_airport_connection

    def _arguments(self, spec: SearchSpec) -> dict:
        options = spec.options
        arguments = {
            "flyFrom": spec.origin,
            "flyTo": spec.destination,
            "departureDate": _as_kiwi_date(spec.depart),
            "currency": options.currency,
            "adults": options.adults,
            "children": options.children,
            "infants": options.infants_in_seat + options.infants_on_lap,
            "cabinClass": CABIN_CLASS.get(options.seat, "M"),
            "sort": "price",
            "allow_self_transfer": self.allow_self_transfer,
            "allow_diff_airport_connection": self.allow_diff_airport_connection,
        }
        if spec.ret:
            arguments["returnDate"] = _as_kiwi_date(spec.ret)
        if options.max_stops is not None:
            arguments["max_sector_stopovers"] = options.max_stops
        return arguments

    def search(self, spec: SearchSpec) -> list[Quote]:
        if spec.trip == "multi":
            # The tool takes one origin/destination pair, so an open-jaw
            # itinerary cannot be expressed. Say so rather than quietly
            # searching something else.
            raise ProviderError("kiwi: 不支援多段行程（multi），請用 fast_flights")

        try:
            payload = call_tool(self.endpoint, TOOL_NAME, self._arguments(spec), token=self.token)
        except McpError as exc:
            raise ProviderError(f"kiwi 查詢失敗: {exc}") from exc

        if isinstance(payload, str):
            raise ProviderError(f"kiwi 回傳的不是結構化結果: {payload[:200]}")
        if not isinstance(payload, dict):
            raise ProviderError(f"kiwi 回傳了非預期的型別: {type(payload).__name__}")
        if payload.get("error"):
            raise ProviderError(f"kiwi 回報錯誤: {payload['error']}")

        itineraries = payload.get("itineraries") or []
        if not itineraries:
            raise ProviderError(f"kiwi 查無航班: {spec}")

        currency = str(payload.get("currency") or spec.options.currency)
        fetched_at = datetime.now(timezone.utc)

        quotes = [
            quote
            for quote in (self._to_quote(item, spec, currency, fetched_at) for item in itineraries)
            if quote
        ]
        if not quotes:
            raise ProviderError(
                f"kiwi 回傳 {len(itineraries)} 筆結果，但沒有一筆的出發日是 {spec.depart}"
            )

        quotes.sort(key=lambda q: q.price)
        return quotes

    def _to_quote(self, item: dict, spec: SearchSpec, currency: str, fetched_at: datetime) -> Quote | None:
        price = item.get("price")
        if not isinstance(price, (int, float)) or price <= 0:
            return None

        outbound = item.get("outbound") or {}

        # Kiwi may answer with nearby dates. Storing one of those under this
        # search's key would corrupt the price history for the date we asked
        # about, so drop anything that does not depart on the requested day.
        if not self._departs_on(outbound, spec.depart):
            return None

        segments = outbound.get("segments") or []
        airlines = tuple(
            dict.fromkeys(
                str(segment.get("carrierName") or segment.get("carrier") or "").strip()
                for segment in segments
                if segment.get("carrierName") or segment.get("carrier")
            )
        )

        stops = outbound.get("stops")
        if not isinstance(stops, int):
            stops = max(len(segments) - 1, 0)

        # Outbound door-to-door, layovers included. fast-flights reports the
        # sum of segment flight times instead, so the two are not identical --
        # each provider's number is the honest one for its own data.
        duration_seconds = outbound.get("durationSeconds") or 0

        return Quote(
            route_name=spec.route_name,
            group=spec.group,
            itinerary=spec.itinerary,
            origin=spec.origin,
            destination=spec.destination,
            depart=spec.depart,
            ret=spec.ret,
            price=int(price),
            currency=currency,
            airlines=airlines,
            stops=stops,
            duration_minutes=int(duration_seconds) // 60,
            url=str(item.get("bookingUrl") or ""),
            source=self.name,
            fetched_at=fetched_at,
        )

    @staticmethod
    def _departs_on(outbound: dict, wanted: date) -> bool:
        raw = outbound.get("departureTime")
        if not isinstance(raw, str) or not raw:
            # Unknown shape: keep it rather than silently discarding a fare.
            return True
        try:
            return datetime.fromisoformat(raw).date() == wanted
        except ValueError:
            return True
