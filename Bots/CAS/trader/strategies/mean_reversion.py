"""Mean-reversion strategy — buy oversold (low z-score + low RSI) names."""

from __future__ import annotations

import logging

from trader.analysis import indicators as ind
from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"
    preferred_regimes = (MarketRegime.MEAN_REVERTING, MarketRegime.RANGE_BOUND)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.mean_reversion
        self.stock_symbols = stock_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []
        bars_by_symbol = fetch_daily_bars(self.broker, self.stock_symbols, self.cfg.lookback * 3)
        candidates: list[tuple[str, float]] = []

        for sym, bars in bars_by_symbol.items():
            z = ind.zscore(bars, self.cfg.lookback)
            rsi_val = ind.rsi(bars)
            if z is None or rsi_val is None:
                continue
            # Oversold: deeply negative z-score AND low RSI → mean-revert long.
            if z <= self.cfg.zscore_entry and rsi_val <= self.cfg.rsi_oversold:
                score = min(1.0, abs(z) / 3.0)
                candidates.append((sym, score))

        candidates.sort(key=lambda c: c[1], reverse=True)
        signals = [
            Signal(
                symbol=sym,
                direction=SignalDirection.LONG,
                strength=score,
                strategy=self.name,
                reason=f"z<= {self.cfg.zscore_entry} & RSI<= {self.cfg.rsi_oversold}",
                confidence=min(1.0, 0.4 + score / 2),
                regime=MarketRegime.MEAN_REVERTING,
            )
            for sym, score in candidates[: self.cfg.top_n]
        ]
        logger.info("Mean-reversion produced %d signals", len(signals))
        return signals
