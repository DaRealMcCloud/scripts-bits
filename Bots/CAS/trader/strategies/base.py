"""Base strategy interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from trader.brokers.types import AssetClass


def is_crypto_symbol(symbol: str) -> bool:
    """Return True if a symbol is a crypto pair (e.g. ``BTC/USD``).

    Crypto pairs use a ``BASE/QUOTE`` slash form; equities/ETFs do not contain
    a slash. This mirrors :func:`trader.data.universe.split_universe`.
    """
    return "/" in symbol


class SignalDirection(Enum):
    LONG = "long"
    SHORT = "short"


class MarketRegime(str, Enum):
    """Coarse market state used by the aggregator to weight strategies."""

    TRENDING = "trending"
    MEAN_REVERTING = "mean_reverting"
    HIGH_VOL = "high_vol"
    RANGE_BOUND = "range_bound"
    UNKNOWN = "unknown"


@dataclass
class Signal:
    symbol: str
    direction: SignalDirection
    strength: float  # 0.0 – 1.0
    strategy: str
    reason: str
    # ── Optional decision-support fields (back-compat: all defaulted) ──
    confidence: float = 1.0  # 0.0 – 1.0, model/strategy self-assessed
    regime: MarketRegime = MarketRegime.UNKNOWN
    target_price: float | None = None
    stop_hint: float | None = None
    timeframe: str = "1day"
    asset_class: AssetClass = AssetClass.US_EQUITY
    metadata: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"Signal({self.symbol} {self.direction.value} "
            f"str={self.strength:.2f} conf={self.confidence:.2f} "
            f"via {self.strategy}: {self.reason})"
        )


class Strategy(ABC):
    """All strategies must implement scan()."""

    name: str = "base"
    # Regimes this strategy performs well in; used for regime-based routing.
    preferred_regimes: tuple[MarketRegime, ...] = ()

    @abstractmethod
    def scan(self) -> list[Signal]:
        """Run the strategy and return a list of trade signals."""
        ...
