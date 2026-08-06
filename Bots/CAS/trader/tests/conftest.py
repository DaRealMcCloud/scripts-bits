"""Shared pytest fixtures + a broker-neutral ``FakeBroker`` for tests.

The FakeBroker implements the full :class:`~trader.brokers.base.BrokerClient`
interface in memory so strategies, risk, execution, and the backtest engine can
be exercised without any network or vendor SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest

from trader.brokers.base import BrokerCapabilities, BrokerClient, InstrumentIdKind
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
    Side,
    Snapshot,
    TimeFrame,
    TimeInForce,
)


def make_bars(
    symbol: str,
    closes: Sequence[float],
    start: datetime | None = None,
    volume: float = 1_000_000,
) -> list[Bar]:
    """Build a daily bar series from a list of closes (open=prev close)."""
    start = start or datetime(2024, 1, 1)
    bars: list[Bar] = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * 1.01
        lo = min(prev, c) * 0.99
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=start + timedelta(days=i),
                open=prev,
                high=hi,
                low=lo,
                close=c,
                volume=volume,
            )
        )
        prev = c
    return bars


class FakeBroker(BrokerClient):
    """In-memory broker for unit tests."""

    def __init__(
        self,
        equity: float = 100_000.0,
        bars: dict[str, list[Bar]] | None = None,
        snapshots: dict[str, Snapshot] | None = None,
        quotes: dict[str, Quote] | None = None,
        positions: list[Position] | None = None,
        caps: BrokerCapabilities | None = None,
        crypto_assets: list[Instrument] | None = None,
    ) -> None:
        self._equity = equity
        self._bars = bars or {}
        self._snapshots = snapshots or {}
        self._quotes = quotes or {}
        self._positions = positions or []
        self._crypto_assets = crypto_assets or []
        self._caps = caps or BrokerCapabilities(
            name="fake",
            id_kind=InstrumentIdKind.SYMBOL,
            supports_streaming=True,
            supports_native_stops=True,
            supports_bracket_orders=True,
            supports_short=True,
            supports_fractional=True,
            supports_crypto=True,
        )
        self.submitted: list[OrderRequest] = []
        self.stop_orders: list[tuple] = []
        self.closed_positions: list[str] = []
        self.cancelled_orders: list[str] = []
        self._open_orders: list[Order] = []
        # Optional map of order_id -> OrderStatus for reconciliation tests.
        self.order_statuses: dict[str, OrderStatus] = {}
        # Optional list of Orders returned by get_recent_orders (backfill tests).
        self.recent_orders: list[Order] = []
        self.connected = False
        self._order_seq = 0

    # ── Metadata / lifecycle ─────────────────────────────────
    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    # ── Account & positions ──────────────────────────────────
    def get_account(self) -> Account:
        return Account(
            account_id="FAKE1",
            equity=self._equity,
            cash=self._equity,
            buying_power=self._equity * 2,
        )

    def get_equity(self) -> float:
        return self._equity

    def get_positions(self) -> list[Position]:
        return list(self._positions)

    # ── Instruments / universe ───────────────────────────────
    def resolve_instrument(self, symbol: str) -> Instrument | None:
        return Instrument(symbol=symbol, broker_id=symbol)

    def list_tradable_equities(self) -> list[Instrument]:
        return [Instrument(symbol=s, broker_id=s) for s in self._bars]

    def list_tradable_crypto(self) -> list[Instrument]:
        return list(self._crypto_assets)

    # ── Market data ──────────────────────────────────────────
    def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        return {s: list(self._bars.get(s, [])) for s in symbols}

    def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        return {s: self._snapshots[s] for s in symbols if s in self._snapshots}

    def get_latest_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    # ── Orders ───────────────────────────────────────────────
    def submit_order(self, request: OrderRequest) -> OrderResult:
        self.submitted.append(request)
        self._order_seq += 1
        oid = f"ord-{self._order_seq}"
        children: tuple[str, ...] = ()
        if request.attached_stop_price is not None:
            children = (f"{oid}-stop",)
        return OrderResult(
            order_id=oid,
            status=OrderStatus.NEW,
            symbol=request.symbol,
            submitted_qty=request.qty,
            child_order_ids=children,
        )

    def submit_stop_order(
        self, symbol: str, side, qty: float, stop_price: float, time_in_force
    ) -> OrderResult:
        self.stop_orders.append((symbol, side, qty, stop_price, time_in_force))
        self._order_seq += 1
        return OrderResult(
            order_id=f"stop-{self._order_seq}",
            status=OrderStatus.NEW,
            symbol=symbol,
            submitted_qty=qty,
        )

    def cancel_all_orders(self) -> None:
        self._open_orders = []

    def cancel_order(self, order_id: str) -> None:
        self.cancelled_orders.append(order_id)
        self._open_orders = [o for o in self._open_orders if o.order_id != order_id]

    def close_all_positions(self) -> None:
        self._positions = []

    def close_position(self, symbol: str) -> bool:
        self.closed_positions.append(symbol)
        self._positions = [p for p in self._positions if p.symbol != symbol]
        return True

    def get_open_orders(self) -> list[Order]:
        return list(self._open_orders)

    def get_order_status(self, order_id: str) -> OrderStatus | None:
        return self.order_statuses.get(order_id)

    def get_recent_orders(self, lookback_days: int = 7) -> list[Order]:
        return list(self.recent_orders)


@pytest.fixture
def fake_broker() -> FakeBroker:
    return FakeBroker()
