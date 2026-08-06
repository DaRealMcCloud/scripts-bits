"""Trend-following strategy — MA crossover confirmed by ADX trend strength."""

from __future__ import annotations

import logging

from trader.analysis import indicators as ind
from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class TrendFollowingStrategy(Strategy):
    name = "trend_following"
    preferred_regimes = (MarketRegime.TRENDING,)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.trend_following
        self.stock_symbols = stock_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []
        bars_by_symbol = fetch_daily_bars(self.broker, self.stock_symbols, self.cfg.slow_ma * 3)
        candidates: list[tuple[str, float, SignalDirection]] = []

        for sym, bars in bars_by_symbol.items():
            fast = ind.sma(bars, self.cfg.fast_ma)
            slow = ind.sma(bars, self.cfg.slow_ma)
            adx_val = ind.adx(bars)
            if fast is None or slow is None or adx_val is None:
                continue
            if adx_val < self.cfg.adx_min:
                continue
            spread = (fast - slow) / slow if slow else 0.0
            if fast > slow:
                candidates.append((sym, abs(spread) * adx_val / 100, SignalDirection.LONG))
            elif fast < slow:
                candidates.append((sym, abs(spread) * adx_val / 100, SignalDirection.SHORT))

        candidates.sort(key=lambda c: c[1], reverse=True)
        signals = [
            Signal(
                symbol=sym,
                direction=direction,
                strength=min(1.0, score),
                strategy=self.name,
                reason=f"MA{self.cfg.fast_ma}/{self.cfg.slow_ma} cross, trend={score:.2f}",
                confidence=min(1.0, 0.5 + score),
                regime=MarketRegime.TRENDING,
            )
            for sym, score, direction in candidates[: self.cfg.top_n]
        ]
        logger.info("Trend-following produced %d signals", len(signals))
        return signals
