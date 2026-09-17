"""Price sources.

Every provider implements :meth:`Provider.search` and returns cheapest-first
:class:`~tracker.models.Quote` objects. Providers are looked up lazily so that
an unconfigured or uninstalled one never breaks a run that does not use it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Quote, SearchSpec


class ProviderError(RuntimeError):
    """A provider failed to answer. Never fatal: the runner falls through to the next one."""


@runtime_checkable
class Provider(Protocol):
    name: str

    def search(self, spec: SearchSpec) -> list[Quote]:
        """Return quotes for ``spec``, cheapest first. Raise ProviderError on failure."""
        ...


def get_provider(name: str) -> Provider:
    """Instantiate a provider by name. Raises ProviderError for unknown names."""
    key = name.lower().replace("-", "_")
    if key == "fast_flights":
        from .fast_flights_provider import FastFlightsProvider

        return FastFlightsProvider()
    if key == "serpapi":
        from .serpapi_provider import SerpApiProvider

        return SerpApiProvider()
    raise ProviderError(f"未知的 provider: {name!r}（可用：fast_flights, serpapi）")
