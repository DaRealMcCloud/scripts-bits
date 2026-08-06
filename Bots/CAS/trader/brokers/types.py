"""Broker-neutral data types.

These enums and dataclasses form the *boundary contract* between the trading
logic and any concrete broker implementation.  Nothing above the broker layer
(strategies, risk, execution, analysis) should ever import a vendor SDK type
(``alpaca.*`` etc.).  Adapters are responsible for translating their native
objects into these neutral types and back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────
class Side(str, Enum):
    """Order side."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"
    OPG = "opg"  # market-on-open


class TimeFrame(str, Enum):
    """Bar granularity, broker-neutral."""

    MIN_1 = "1min"
    MIN_5 = "5min"
    MIN_15 = "15min"
    MIN_30 = "30min"
    HOUR_1 = "1hour"
    DAY = "1day"
    WEEK = "1week"


class AssetClass(str, Enum):
    US_EQUITY = "us_equity"
    CRYPTO = "crypto"
    FOREX = "forex"
    OPTION = "option"
    FUTURE = "future"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    PENDING = "pending"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


# ─────────────────────────────────────────────────────────────────────────────
# Data transfer objects (DTOs)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Instrument:
    """A tradeable instrument, resolved across brokers.

    ``symbol`` is the human/ticker identifier used by strategies.
    ``broker_id`` is the broker-native identifier (Alpaca uses the symbol
    itself; IBKR uses an integer ``conid`` stored as a string).
    """

    symbol: str
    broker_id: str = ""
    asset_class: AssetClass = AssetClass.US_EQUITY
    exchange: str = ""
    currency: str = "USD"
    tradable: bool = True
    fractionable: bool = False


@dataclass(frozen=True)
class Bar:
    """A single OHLCV bar."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    trade_count: int | None = None


@dataclass(frozen=True)
class Quote:
    """Top-of-book quote."""

    symbol: str
    bid_price: float
    ask_price: float
    bid_size: float = 0.0
    ask_size: float = 0.0
    timestamp: datetime | None = None

    @property
    def mid(self) -> float:
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2.0
        return self.ask_price or self.bid_price


@dataclass(frozen=True)
class Snapshot:
    """A point-in-time market snapshot for a symbol.

    Field names are deliberately neutral; adapters populate what they can.
    """

    symbol: str
    latest_price: float | None = None
    daily_open: float | None = None
    daily_high: float | None = None
    daily_low: float | None = None
    daily_close: float | None = None
    daily_volume: float | None = None
    prev_daily_close: float | None = None
    quote: Quote | None = None


@dataclass(frozen=True)
class Position:
    """An open position."""

    symbol: str
    qty: float  # signed: positive = long, negative = short
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_pl_pct: float
    asset_class: AssetClass = AssetClass.US_EQUITY

    @property
    def side(self) -> PositionSide:
        return PositionSide.LONG if self.qty >= 0 else PositionSide.SHORT

    @property
    def abs_qty(self) -> float:
        return abs(self.qty)


@dataclass(frozen=True)
class Account:
    """Account-level financial snapshot."""

    account_id: str
    equity: float
    cash: float
    buying_power: float
    currency: str = "USD"
    unrealized_pl: float = 0.0


@dataclass
class Order:
    """A submitted or historical order (neutral form)."""

    order_id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType
    status: OrderStatus = OrderStatus.UNKNOWN
    limit_price: float | None = None
    stop_price: float | None = None
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    submitted_at: datetime | None = None
    strategy: str = ""
    parent_order_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class OrderRequest:
    """A request to place an order, broker-neutral."""

    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.LIMIT
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    # Optional attached protective stop, used to build native bracket orders.
    attached_stop_price: float | None = None
    strategy: str = ""
    client_order_id: str | None = None


@dataclass(frozen=True)
class OrderResult:
    """The outcome of an order submission."""

    order_id: str
    status: OrderStatus
    symbol: str
    submitted_qty: float
    child_order_ids: tuple[str, ...] = ()  # e.g. attached stop leg
    raw: dict = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.status not in (OrderStatus.REJECTED, OrderStatus.UNKNOWN)
