"""Crypto mean reversion — z-score + RSI oversold bounce (long-only)."""

from __future__ import annotations

import logging

from trader.analysis.indicators import rsi, zscore
from trader.brokers.base import BrokerClient
from trader.brokers.types import AssetClass
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class CryptoMeanReversionStrategy(Strategy):
    """Buy oversold crypto expecting reversion to the mean.

    Long-only: enters when the close is stretched below its mean (negative
    z-score) AND RSI is oversold. Never shorts overbought conditions.
    """

    name = "crypto_mean_reversion"
    preferred_regimes = (MarketRegime.MEAN_REVERTING, MarketRegime.RANGE_BOUND)

    def __init__(self, broker: BrokerClient, cfg: Config, crypto_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.crypto.mean_reversion
        self.crypto_symbols = crypto_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.crypto_symbols:
            return []

        days = max(self.cfg.lookback * 3, 90)
        bars_by_symbol = fetch_daily_bars(self.broker, self.crypto_symbols, days)
        if not bars_by_symbol:
            logger.warning("No bars returned for crypto mean-reversion scan")
            return []

        scored: list[tuple[str, float]] = []
        for symbol, bars in bars_by_symbol.items():
            z = zscore(bars, self.cfg.lookback)
            r = rsi(bars, 14)
            if z is None or r is None:
                continue
            # Long-only: require stretched-below AND oversold.
            if z <= self.cfg.zscore_entry and r <= self.cfg.rsi_oversold:
                # Deeper stretch → stronger signal.
                scored.append((symbol, abs(z)))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        signals: list[Signal] = []
        for symbol, mag in scored[: self.cfg.top_n]:
            strength = min(1.0, mag / 3.0)
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=strength,
                    strategy=self.name,
                    reason=f"crypto_mean_reversion z={-mag:.2f}",
                    confidence=min(1.0, 0.5 + strength / 2),
                    regime=MarketRegime.MEAN_REVERTING,
                    asset_class=AssetClass.CRYPTO,
                )
            )

        logger.info("Crypto mean-reversion scan produced %d signals", len(signals))
        return signals
