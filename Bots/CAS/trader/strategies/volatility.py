"""Volatility strategy — favours low-realized-vol names, avoids extreme vol."""

from __future__ import annotations

import logging

from trader.analysis import indicators as ind
from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class VolatilityStrategy(Strategy):
    name = "volatility"
    preferred_regimes = (MarketRegime.RANGE_BOUND, MarketRegime.MEAN_REVERTING)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.volatility
        self.stock_symbols = stock_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []
        bars_by_symbol = fetch_daily_bars(self.broker, self.stock_symbols, self.cfg.lookback * 3)
        candidates: list[tuple[str, float]] = []

        for sym, bars in bars_by_symbol.items():
            vol = ind.realized_vol(bars, self.cfg.lookback)
            if vol is None or vol > self.cfg.high_vol_pct:
                continue
            if vol <= self.cfg.low_vol_pct:
                # Lower vol → higher score (favour stable uptrends).
                sma_fast = ind.sma(bars, 10)
                sma_slow = ind.sma(bars, self.cfg.lookback)
                if sma_fast and sma_slow and sma_fast > sma_slow:
                    score = min(1.0, (self.cfg.low_vol_pct - vol) / self.cfg.low_vol_pct + 0.3)
                    candidates.append((sym, score))

        candidates.sort(key=lambda c: c[1], reverse=True)
        signals = [
            Signal(
                symbol=sym,
                direction=SignalDirection.LONG,
                strength=score,
                strategy=self.name,
                reason="low-vol uptrend",
                confidence=min(1.0, 0.4 + score / 2),
                regime=MarketRegime.RANGE_BOUND,
            )
            for sym, score in candidates[: self.cfg.top_n]
        ]
        logger.info("Volatility strategy produced %d signals", len(signals))
        return signals
