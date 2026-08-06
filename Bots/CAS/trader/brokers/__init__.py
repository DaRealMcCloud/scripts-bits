"""Broker abstraction package.

Public surface:
- :mod:`trader.brokers.types` — neutral enums + DTOs.
- :mod:`trader.brokers.base` — ``BrokerClient`` interface + capabilities.
- :func:`trader.brokers.factory.make_broker` — build a broker from config.
"""

from __future__ import annotations

from trader.brokers.base import (
    BrokerAuthError,
    BrokerCapabilities,
    BrokerClient,
    BrokerError,
    InstrumentIdKind,
)
from trader.brokers.types import (
    Account,
    AssetClass,
    Bar,
    Instrument,
    Order,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    PositionSide,
    Quote,
    Side,
    Snapshot,
    TimeFrame,
    TimeInForce,
)

__all__ = [
    "Account",
    "AssetClass",
    "Bar",
    "BrokerAuthError",
    "BrokerCapabilities",
    "BrokerClient",
    "BrokerError",
    "Instrument",
    "InstrumentIdKind",
    "Order",
    "OrderRequest",
    "OrderResult",
    "OrderStatus",
    "OrderType",
    "Position",
    "PositionSide",
    "Quote",
    "Side",
    "Snapshot",
    "TimeFrame",
    "TimeInForce",
]
