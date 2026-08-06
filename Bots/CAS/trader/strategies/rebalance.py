"""Rebalancing strategy — steer the portfolio toward target weights.

Compares current position weights against configured target weights and emits
signals to buy underweight targets (drift beyond ``drift_pct``).  Trimming
overweight names is left to the risk/execution layer's position management;
this strategy focuses on topping up underweights.
"""

from __future__ import annotations

import logging

from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class RebalanceStrategy(Strategy):
    name = "rebalance"
    preferred_regimes = ()

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.rebalance

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.cfg.targets:
            return []
        try:
            account = self.broker.get_account()
            positions = {p.symbol: p for p in self.broker.get_positions()}
        except Exception:
            logger.warning("Rebalance could not read account/positions", exc_info=True)
            return []

        equity = account.equity or 0.0
        if equity <= 0:
            return []

        signals: list[Signal] = []
        for sym, target_w in self.cfg.targets.items():
            current_val = abs(positions[sym].market_value) if sym in positions else 0.0
            current_w = current_val / equity
            drift = target_w - current_w
            if drift > self.cfg.drift_pct:
                strength = min(1.0, drift / max(target_w, 1e-6))
                signals.append(
                    Signal(
                        symbol=sym,
                        direction=SignalDirection.LONG,
                        strength=strength,
                        strategy=self.name,
                        reason=f"underweight {current_w:.0%} vs target {target_w:.0%}",
                        confidence=1.0,
                        regime=MarketRegime.UNKNOWN,
                    )
                )
        logger.info("Rebalance produced %d signals", len(signals))
        return signals
