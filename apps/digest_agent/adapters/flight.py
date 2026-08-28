"""Deterministic fake flight-price observation adapter for P4.3."""

import copy

from ..domain import DomainError, FlightObservationQuery, FlightPriceQuote, utc_now


class FakeFlightPriceProvider:
    """Return one typed, read-only quote; no network or notification effect."""

    provider_identity = "fake_flight_price"

    def __init__(self, price=920, *, observed_at=None, source_signal_id=None,
                 overrides=None, clock=None):
        if type(price) is not int or isinstance(price, bool):
            raise ValueError("fake flight price must be an integer")
        self.price = price
        self.observed_at = observed_at
        self.source_signal_id = source_signal_id
        self.overrides = copy.deepcopy(overrides or {})
        self.clock = clock or utc_now
        self.calls = []

    def observe(self, query):
        if not isinstance(query, FlightObservationQuery):
            raise DomainError("invalid flight observation query")
        self.calls.append(query)
        observed_at = self.observed_at or self.clock()
        raw = {
            "source_signal_id": self.source_signal_id or (
                "fake:SZX:WUH:"
                f"{query.travel_month}:{self.price}:{observed_at}"
            ),
            "origin": query.origin,
            "destination": query.destination,
            "trip_type": query.trip_type,
            "travel_month": query.travel_month,
            "metric": "round_trip_price",
            "price": self.price,
            "currency": "CNY",
            "observed_at": observed_at,
        }
        raw.update(copy.deepcopy(self.overrides))
        if set(raw) != {
                "source_signal_id", "origin", "destination", "trip_type",
                "travel_month", "metric", "price", "currency",
                "observed_at"}:
            raise DomainError("fake flight observation schema 无效")
        return FlightPriceQuote(**raw)
