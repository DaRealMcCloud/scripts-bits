"""Crypto momentum — dual-MA trend with ADX filter (long-only)."""

from __future__ import annotations

import logging

from trader.analysis.indicators import adx, sma
from trader.brokers.base import BrokerClient
from trader.brokers.types import AssetClass
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class CryptoMomentumStrategy(Strategy):
    """Buy crypto in a confirmed uptrend (fast MA > slow MA, ADX above min).

    Unlike the equity momentum strategy there is NO fundamental quality filter
    (crypto has no fundamentals). Long-only.
    """

    name = "crypto_momentum"
    preferred_regimes = (MarketRegime.TRENDING,)

    def __init__(self, broker: BrokerClient, cfg: Config, crypto_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.crypto.momentum
        self.crypto_symbols = crypto_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.crypto_symbols:
            return []

        days = max(self.cfg.slow_ma * 3, 120)
        bars_by_symbol = fetch_daily_bars(self.broker, self.crypto_symbols, days)
        if not bars_by_symbol:
            logger.warning("No bars returned for crypto momentum scan")
            return []

        scored: list[tuple[str, float]] = []
        for symbol, bars in bars_by_symbol.items():
            fast = sma(bars, self.cfg.fast_ma)
            slow = sma(bars, self.cfg.slow_ma)
            trend = adx(bars, 14)
            if fast is None or slow is None or trend is None:
                continue
            if fast <= slow or trend < self.cfg.adx_min:
                continue
            # Score: MA separation scaled by trend strength.
            sep = (fast - slow) / slow if slow else 0.0
            score = max(0.0, sep) * (trend / 100.0)
            if score > 0:
                scored.append((symbol, score))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        signals: list[Signal] = []
        for symbol, score in scored[: self.cfg.top_n]:
            strength = min(1.0, score * 5.0)
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=strength,
                    strategy=self.name,
                    reason=f"crypto_momentum sep*adx={score:.3f}",
                    confidence=min(1.0, 0.5 + strength / 2),
                    regime=MarketRegime.TRENDING,
                    asset_class=AssetClass.CRYPTO,
                )
            )

        logger.info("Crypto momentum scan produced %d signals", len(signals))
        return signals
