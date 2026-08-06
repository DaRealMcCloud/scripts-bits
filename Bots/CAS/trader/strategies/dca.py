"""Dollar-cost-averaging strategy — periodic fixed-cadence buys.

Emits LONG signals for the configured symbols on the scheduled cadence.  Useful
for passive accumulation of core holdings alongside active strategies.  The
cadence is enforced by tracking the last-buy date per symbol.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class DcaStrategy(Strategy):
    name = "dca"
    preferred_regimes = ()  # regime-agnostic

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.dca
        self._last_buy: dict[str, datetime] = {}

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.cfg.symbols:
            return []
        now = datetime.now()
        signals: list[Signal] = []
        for sym in self.cfg.symbols:
            last = self._last_buy.get(sym)
            if last is not None and now - last < timedelta(days=self.cfg.cadence_days):
                continue
            self._last_buy[sym] = now
            signals.append(
                Signal(
                    symbol=sym,
                    direction=SignalDirection.LONG,
                    strength=0.5,
                    strategy=self.name,
                    reason=f"DCA every {self.cfg.cadence_days}d",
                    confidence=1.0,
                    regime=MarketRegime.UNKNOWN,
                )
            )
        logger.info("DCA produced %d signals", len(signals))
        return signals
