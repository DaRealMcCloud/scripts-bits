"""Crypto volatility — buy low-volatility consolidations (long-only).

Enters names whose realised volatility sits in a favourable band: after a
low-volatility coil (below ``low_vol_pct``) that often precedes expansion, while
avoiding extreme-volatility blow-off names (above ``high_vol_pct``). Long-only.
"""

from __future__ import annotations

import logging

from trader.analysis.indicators import realized_vol, sma
from trader.brokers.base import BrokerClient
from trader.brokers.types import AssetClass
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class CryptoVolatilityStrategy(Strategy):
    name = "crypto_volatility"
    preferred_regimes = (MarketRegime.RANGE_BOUND, MarketRegime.HIGH_VOL)

    def __init__(self, broker: BrokerClient, cfg: Config, crypto_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.crypto.volatility
        self.crypto_symbols = crypto_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled or not self.crypto_symbols:
            return []

        days = max(self.cfg.lookback * 4, 90)
        bars_by_symbol = fetch_daily_bars(self.broker, self.crypto_symbols, days)
        if not bars_by_symbol:
            logger.warning("No bars returned for crypto volatility scan")
            return []

        candidates: list[tuple[str, float]] = []
        for symbol, bars in bars_by_symbol.items():
            vol = realized_vol(bars, self.cfg.lookback)
            if vol is None:
                continue
            # Skip extreme / blow-off volatility outright.
            if vol > self.cfg.high_vol_pct:
                continue
            # Only buy a genuine low-volatility coil (vol at/below low_vol_pct)
            # in an intact uptrend (price >= short MA) → expansion setup.
            if vol > self.cfg.low_vol_pct:
                continue
            fast = sma(bars, 10)
            if fast is None or bars[-1].close < fast:
                continue
            # Lower vol → tighter coil → rank higher.
            candidates.append((symbol, vol))

        candidates.sort(key=lambda kv: kv[1])  # lowest vol first
        signals: list[Signal] = []
        for symbol, vol in candidates[: self.cfg.top_n]:
            strength = min(1.0, max(0.1, self.cfg.low_vol_pct / (vol + 1e-9) / 5.0))
            signals.append(
                Signal(
                    symbol=symbol,
                    direction=SignalDirection.LONG,
                    strength=strength,
                    strategy=self.name,
                    reason=f"crypto_volatility coil vol={vol:.2f}",
                    confidence=min(1.0, 0.5 + strength / 2),
                    regime=MarketRegime.RANGE_BOUND,
                    asset_class=AssetClass.CRYPTO,
                )
            )

        logger.info("Crypto volatility scan produced %d signals", len(signals))
        return signals
