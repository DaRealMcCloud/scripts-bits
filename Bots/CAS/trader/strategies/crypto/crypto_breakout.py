"""Crypto breakout — Donchian channel upper-band breakout (long-only)."""

from __future__ import annotations

import logging

from trader.analysis.indicators import donchian
from trader.brokers.base import BrokerClient
from trader.brokers.types import AssetClass
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class CryptoBreakoutStrategy(Strategy):
    """Buy when price breaks above the Donchian channel high.

    Long-only: only upper-band breakouts generate signals. Lower-band
    breakdowns are ignored (no shorting on crypto).
    """

    name = "crypto_breakout"
    preferred_regimes = (MarketRegime.TRENDING, MarketRegime.HIGH_VOL)

    def __init__(self, broker: BrokerClient, cfg: Config, crypto_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.crypto.breakout
        self.crypto_symbols = crypto_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.crypto_symbols:
            return []

        days = max(self.cfg.channel * 3, 90)
        bars_by_symbol = fetch_daily_bars(self.broker, self.crypto_symbols, days)
        if not bars_by_symbol:
            logger.warning("No bars returned for crypto breakout scan")
            return []

        scored: list[tuple[str, float]] = []
        for symbol, bars in bars_by_symbol.items():
            if len(bars) < self.cfg.channel + 1:
                continue
            # Channel from bars EXCLUDING the latest, then test the latest close.
            ch = donchian(bars[:-1], self.cfg.channel)
            if ch is None:
                continue
            last_close = bars[-1].close
            upper = ch["upper"]
            if upper <= 0 or last_close <= upper:
                continue  # long-only: ignore breakdowns / no-breakout
            breakout_pct = (last_close - upper) / upper
            scored.append((symbol, breakout_pct))

        scored.sort(key=lambda kv: kv[1], reverse=True)
        signals: list[Signal] = []
        for symbol, breakout_pct in scored[: self.cfg.top_n]:
            strength = min(1.0, breakout_pct * 20.0)
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=max(0.1, strength),
                    strategy=self.name,
                    reason=f"crypto_breakout +{breakout_pct * 100:.2f}% over {self.cfg.channel}d high",
                    confidence=min(1.0, 0.5 + strength / 2),
                    regime=MarketRegime.TRENDING,
                    asset_class=AssetClass.CRYPTO,
                )
            )

        logger.info("Crypto breakout scan produced %d signals", len(signals))
        return signals
