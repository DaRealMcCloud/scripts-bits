"""Breakout strategy — Donchian channel breakouts."""

from __future__ import annotations

import logging

from trader.analysis import indicators as ind
from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class BreakoutStrategy(Strategy):
    name = "breakout"
    preferred_regimes = (MarketRegime.TRENDING, MarketRegime.HIGH_VOL)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.breakout
        self.stock_symbols = stock_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []
        bars_by_symbol = fetch_daily_bars(self.broker, self.stock_symbols, self.cfg.channel * 3)
        candidates: list[tuple[str, float, SignalDirection]] = []

        for sym, bars in bars_by_symbol.items():
            # Exclude the most recent bar from the channel so a fresh high counts.
            channel = ind.donchian(bars[:-1], self.cfg.channel)
            if channel is None or not bars:
                continue
            last_close = bars[-1].close
            width = channel["upper"] - channel["lower"]
            if width <= 0:
                continue
            if last_close > channel["upper"]:
                strength = min(1.0, (last_close - channel["upper"]) / width)
                candidates.append((sym, strength, SignalDirection.LONG))
            elif last_close < channel["lower"]:
                strength = min(1.0, (channel["lower"] - last_close) / width)
                candidates.append((sym, strength, SignalDirection.SHORT))

        candidates.sort(key=lambda c: c[1], reverse=True)
        signals = [
            Signal(
                symbol=sym,
                direction=direction,
                strength=max(0.2, strength),
                strategy=self.name,
                reason=f"{self.cfg.channel}d Donchian breakout",
                confidence=min(1.0, 0.5 + strength),
                regime=MarketRegime.TRENDING,
            )
            for sym, strength, direction in candidates[: self.cfg.top_n]
        ]
        logger.info("Breakout produced %d signals", len(signals))
        return signals
