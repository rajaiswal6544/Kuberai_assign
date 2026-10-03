"""Gold price source with a last-good-price cache. We never quote or sell on a stale price."""

import threading
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.clock import Clock
from app.config import RUPEE
from app.logging import get_logger

log = get_logger()


class PriceProviderError(Exception):
    pass


class PriceUnavailable(Exception):
    """No price fresh enough to sell on."""


@dataclass(frozen=True)
class PricePoint:
    price: Decimal  # INR per gram, ex-GST, 2 dp
    as_of: datetime


class PriceProvider(Protocol):
    name: str

    def fetch(self) -> PricePoint: ...


class SimulatedPriceProvider:
    """Stands in for a real feed. Can be switched "down" to simulate an outage."""

    name = "simulated"

    def __init__(self, base_price: Decimal, clock: Clock, mode: str = "up") -> None:
        self.base_price = base_price
        self.mode = mode
        self._clock = clock

    def fetch(self) -> PricePoint:
        if self.mode != "up":
            raise PriceProviderError("simulated price source is down")
        return PricePoint(price=self.base_price, as_of=self._clock.now())


class PriceService:
    """Fetches from the provider on every call. If the provider fails, falls back to the
    last good price only while it is younger than max_age; otherwise refuses."""

    def __init__(self, provider: PriceProvider, clock: Clock, max_age_seconds: int) -> None:
        self.provider = provider
        self._clock = clock
        self._max_age = max_age_seconds
        self._last_good: PricePoint | None = None
        self._lock = threading.Lock()

    def _age(self, point: PricePoint) -> float:
        return (self._clock.now() - point.as_of).total_seconds()

    def current(self) -> PricePoint:
        try:
            point = self.provider.fetch()
            if (
                not isinstance(point.price, Decimal)
                or not point.price.is_finite()
                or point.price <= 0
                or point.price != point.price.quantize(RUPEE)
            ):
                raise PriceProviderError(f"invalid price from provider: {point.price!r}")
            if self._age(point) > self._max_age:
                log.warning("price_stale", source="provider", age_seconds=self._age(point))
            else:
                with self._lock:
                    self._last_good = point
                return point
        except PriceProviderError as exc:
            log.warning("price_fetch_failed", provider=self.provider.name, error=str(exc))

        with self._lock:
            cached = self._last_good
        if cached is not None:
            age = self._age(cached)
            if age <= self._max_age:
                log.info("price_cache_used", age_seconds=round(age, 1), price=cached.price)
                return cached
            log.warning("price_stale", source="cache", age_seconds=round(age, 1))
        raise PriceUnavailable()

    def expire_cache(self) -> None:
        with self._lock:
            self._last_good = None

    def status(self) -> dict:
        try:
            point = self.current()
        except PriceUnavailable:
            return {"status": "unavailable", "provider_mode": getattr(self.provider, "mode", None)}
        return {
            "status": "up",
            "provider_mode": getattr(self.provider, "mode", None),
            "price_per_gram": str(point.price),
            "age_seconds": round(self._age(point), 1),
        }
