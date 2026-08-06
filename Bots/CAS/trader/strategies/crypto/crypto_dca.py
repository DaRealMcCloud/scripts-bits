"""Crypto DCA — periodic accumulation of chosen symbols (long-only)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from trader.brokers.base import BrokerClient
from trader.brokers.types import AssetClass
from trader.config import Config
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class CryptoDcaStrategy(Strategy):
    """Dollar-cost-average accumulation.

    Emits a LONG signal for each configured symbol once per ``cadence_hours``,
    regardless of market regime. Cadence is tracked in-memory per symbol; the
    first scan after startup always accumulates.
    """

    name = "crypto_dca"
    # DCA is regime-agnostic; fits any state.
    preferred_regimes = (
        MarketRegime.TRENDING,
        MarketRegime.MEAN_REVERTING,
        MarketRegime.HIGH_VOL,
        MarketRegime.RANGE_BOUND,
        MarketRegime.UNKNOWN,
    )

    def __init__(self, broker: BrokerClient, cfg: Config, crypto_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.crypto.dca
        self.crypto_symbols = crypto_symbols
        self._last_buy: dict[str, datetime] = {}

    def _due(self, symbol: str, now: datetime) -> bool:
        last = self._last_buy.get(symbol)
        if last is None:
            return True
        return now - last >= timedelta(hours=self.cfg.cadence_hours)

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []

        # DCA trades a curated list; fall back to the discovered universe only
        # if none configured would be too broad, so require explicit symbols.
        targets = [s for s in self.cfg.symbols if s in self.crypto_symbols] or list(
            self.cfg.symbols
        )
        if not targets:
            return []

        now = datetime.now(timezone.utc)
        signals: list[Signal] = []
        for symbol in targets:
            if not self._due(symbol, now):
                continue
            self._last_buy[symbol] = now
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=0.5,
                    strategy=self.name,
                    reason=f"crypto_dca accumulate every {self.cfg.cadence_hours}h",
                    confidence=0.6,
                    regime=MarketRegime.UNKNOWN,
                    asset_class=AssetClass.CRYPTO,
                )
            )

        logger.info("Crypto DCA scan produced %d signals", len(signals))
        return signals
