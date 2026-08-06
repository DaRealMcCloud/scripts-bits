"""Market regime classification.

Classifies a symbol's recent price action into a :class:`MarketRegime`
(trending / mean-reverting / high-vol / range-bound) using ADX, realized
volatility and a Bollinger-band width heuristic.  The aggregator uses this to
up-weight strategies suited to the current regime.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from trader.analysis import indicators as ind
from trader.brokers.types import Bar
from trader.strategies.base import MarketRegime

logger = logging.getLogger(__name__)


class RegimeClassifier:
    def __init__(
        self,
        adx_trend_threshold: float = 25.0,
        high_vol_threshold: float = 0.40,
        range_band_pct: float = 0.05,
    ) -> None:
        self.adx_trend_threshold = adx_trend_threshold
        self.high_vol_threshold = high_vol_threshold
        self.range_band_pct = range_band_pct

    def classify(self, bars: Sequence[Bar]) -> MarketRegime:
        if len(bars) < 30:
            return MarketRegime.UNKNOWN

        vol = ind.realized_vol(bars) or 0.0
        if vol >= self.high_vol_threshold:
            return MarketRegime.HIGH_VOL

        adx_val = ind.adx(bars)
        if adx_val is not None and adx_val >= self.adx_trend_threshold:
            return MarketRegime.TRENDING

        band = ind.bollinger(bars)
        if band and band["middle"] > 0:
            width = (band["upper"] - band["lower"]) / band["middle"]
            if width <= self.range_band_pct:
                return MarketRegime.RANGE_BOUND

        # Low trend strength but not tight range → mean-reverting bias.
        return MarketRegime.MEAN_REVERTING

    def classify_market(self, benchmark_bars: Sequence[Bar]) -> MarketRegime:
        """Classify the overall market from a benchmark (e.g. SPY) series."""
        return self.classify(benchmark_bars)
