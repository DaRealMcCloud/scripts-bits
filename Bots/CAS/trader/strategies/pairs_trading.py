"""Pairs trading — statistical arbitrage on a spread's z-score.

For each configured pair ``A/B`` we compute the log-price spread and its
z-score.  When the spread stretches beyond ``zscore_entry`` we go long the
cheap leg and short the rich leg (both signals emitted).
"""

from __future__ import annotations

import logging
import math

from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class PairsTradingStrategy(Strategy):
    name = "pairs_trading"
    preferred_regimes = (MarketRegime.MEAN_REVERTING, MarketRegime.RANGE_BOUND)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.pairs_trading

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.cfg.pairs:
            return []

        legs = {s for pair in self.cfg.pairs for s in pair.split("/")}
        bars_by_symbol = fetch_daily_bars(self.broker, sorted(legs), self.cfg.lookback * 3)
        signals: list[Signal] = []

        for pair in self.cfg.pairs:
            try:
                a, b = pair.split("/")
            except ValueError:
                continue
            ba, bb = bars_by_symbol.get(a), bars_by_symbol.get(b)
            if not ba or not bb or len(ba) < self.cfg.lookback or len(bb) < self.cfg.lookback:
                continue
            spread = [
                math.log(x.close) - math.log(y.close)
                for x, y in zip(ba[-self.cfg.lookback :], bb[-self.cfg.lookback :], strict=False)
                if x.close > 0 and y.close > 0
            ]
            if len(spread) < 2:
                continue
            mean = sum(spread) / len(spread)
            var = sum((s - mean) ** 2 for s in spread) / len(spread)
            std = math.sqrt(var)
            if std == 0:
                continue
            z = (spread[-1] - mean) / std
            strength = min(1.0, abs(z) / (self.cfg.zscore_entry * 2))

            if z >= self.cfg.zscore_entry:
                # Spread rich → short A, long B.
                signals.append(self._sig(a, SignalDirection.SHORT, strength, z, pair))
                signals.append(self._sig(b, SignalDirection.LONG, strength, z, pair))
            elif z <= -self.cfg.zscore_entry:
                signals.append(self._sig(a, SignalDirection.LONG, strength, z, pair))
                signals.append(self._sig(b, SignalDirection.SHORT, strength, z, pair))

        logger.info("Pairs trading produced %d signals", len(signals))
        return signals

    def _sig(self, sym, direction, strength, z, pair) -> Signal:
        return Signal(
            symbol=sym,
            direction=direction,
            strength=strength,
            strategy=self.name,
            reason=f"pair {pair} spread z={z:+.2f}",
            confidence=min(1.0, 0.4 + strength / 2),
            regime=MarketRegime.MEAN_REVERTING,
        )
