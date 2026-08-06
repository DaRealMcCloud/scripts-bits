"""Broker abstraction: the ``BrokerClient`` interface + capability model.

Any concrete broker (Alpaca, IBKR, a backtest sim) implements ``BrokerClient``.
Trading logic depends only on this interface and the neutral DTOs in
``trader.brokers.types`` — never on a vendor SDK.

The :class:`BrokerCapabilities` object lets callers degrade gracefully when a
broker lacks a feature (e.g. streaming quotes or native stop orders) instead of
crashing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from trader.brokers.types import (
    Account,
    Bar,
    Instrument,
    Order,
    OrderRequest,
    OrderResult,
    OrderStatus,
    Position,
    Quote,
    Snapshot,
    TimeFrame,
)

# Callback signature for streaming quotes: (symbol, bid_size, ask_size, quote)
QuoteCallback = Callable[[str, float, float, Quote], None]


class InstrumentIdKind:
    """How the broker identifies instruments."""

    SYMBOL = "symbol"  # Alpaca — ticker is the id
    CONID = "conid"  # IBKR — numeric contract id


@dataclass(frozen=True)
class BrokerCapabilities:
    """Feature matrix for a broker, so callers can adapt behaviour."""

    name: str
    id_kind: str = InstrumentIdKind.SYMBOL
    supports_streaming: bool = False
    supports_bar_history: bool = True
    supports_snapshots: bool = True
    supports_native_stops: bool = False
    supports_bracket_orders: bool = False
    supports_short: bool = True
    supports_fractional: bool = False
    supports_crypto: bool = False
    # Some brokers (IBKR) require an explicit reply-confirmation dance / keepalive.
    requires_session_keepalive: bool = False


class BrokerError(RuntimeError):
    """Base class for broker adapter errors."""


class BrokerAuthError(BrokerError):
    """Raised when the broker session is not authenticated/ready."""


class BrokerClient(ABC):
    """Broker-neutral trading + market-data interface.

    Concrete adapters translate their native SDK/HTTP objects into the neutral
    DTOs from :mod:`trader.brokers.types`.
    """

    # ── Metadata ─────────────────────────────────────────────
    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        ...

    # ── Session / lifecycle ──────────────────────────────────
    @abstractmethod
    def connect(self) -> None:
        """Establish/validate the session. Raise BrokerAuthError if not ready."""

    def close(self) -> None:  # optional override
        """Release resources (stop streams, background threads)."""

    # ── Account & positions ──────────────────────────────────
    @abstractmethod
    def get_account(self) -> Account:
        ...

    def get_equity(self) -> float:
        return self.get_account().equity

    def get_cash(self) -> float:
        """Free cash available to fund new orders (spot/cash buying power).

        Crypto on Alpaca is cash-only (no margin), so this is the hard ceiling
        for new crypto buys. Falls back to ``cash`` if reported.
        """
        return self.get_account().cash

    @abstractmethod
    def get_positions(self) -> list[Position]:
        ...

    # ── Instruments / universe ───────────────────────────────
    @abstractmethod
    def resolve_instrument(self, symbol: str) -> Instrument | None:
        """Resolve a ticker into a broker-native Instrument (with broker_id)."""

    @abstractmethod
    def list_tradable_equities(self) -> list[Instrument]:
        ...

    def list_tradable_crypto(self) -> list[Instrument]:
        """Return all tradable crypto instruments the broker offers.

        Optional: brokers without a bulk crypto listing (or without crypto
        support at all) return an empty list. Only brokers whose
        ``capabilities.supports_crypto`` is true are expected to override this.
        """
        return []

    # ── Market data ──────────────────────────────────────────
    @abstractmethod
    def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        """Return historical bars keyed by symbol."""

    @abstractmethod
    def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        ...

    @abstractmethod
    def get_latest_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        ...

    # ── Streaming (optional; gated by capabilities) ──────────
    def subscribe_quotes(self, symbols: Sequence[str], callback: QuoteCallback) -> None:
        raise NotImplementedError(f"{self.capabilities.name} does not support streaming quotes")

    def start_stream(self) -> None:
        raise NotImplementedError(f"{self.capabilities.name} does not support streaming")

    def stop_stream(self) -> None:
        return None

    # ── Orders ───────────────────────────────────────────────
    @abstractmethod
    def submit_order(self, request: OrderRequest) -> OrderResult:
        """Submit a single order. If ``request.attached_stop_price`` is set and
        the broker supports it, a native bracket/stop leg should be attached."""

    @abstractmethod
    def submit_stop_order(
        self, symbol: str, side, qty: float, stop_price: float, time_in_force
    ) -> OrderResult:
        """Submit a standalone stop order (fallback when brackets unsupported)."""

    @abstractmethod
    def cancel_all_orders(self) -> None:
        ...

    def cancel_order(self, order_id: str) -> None:
        """Cancel a single order by id. Optional; default is a no-op."""
        return None

    @abstractmethod
    def close_all_positions(self) -> None:
        ...

    def close_position(self, symbol: str) -> bool:
        """Close a single position by symbol (market order).

        Optional override; brokers that only support bulk close can leave the
        default no-op, but crypto-capable brokers should implement it so the
        EOD flatten can close equities selectively while leaving crypto open.

        Returns True on success, False if the close failed.
        """
        return False

    @abstractmethod
    def get_open_orders(self) -> list[Order]:
        ...

    def get_order_status(self, order_id: str) -> OrderStatus | None:
        """Return the current status of a single order by id, or None.

        Used to reconcile persisted order records (which capture only the
        submit-time status) with the order's true terminal state (filled,
        canceled, rejected…). Optional: brokers that can't look up a single
        order return None and the stored status is left unchanged.
        """
        return None

    def get_recent_orders(self, lookback_days: int = 7) -> list[Order]:
        """Return recent orders (open + closed) within the lookback window.

        Includes ``filled_avg_price`` where available. Used to backfill the
        local store with fills the app didn't submit itself (e.g. software
        stop / take-profit closes) and to correct approximate fill prices.
        Optional: brokers without history return an empty list.
        """
        return []