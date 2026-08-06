"""Cross-sectional momentum ranking with fundamental quality filter."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from trader.brokers.base import BrokerClient
from trader.brokers.types import Bar, TimeFrame
from trader.config import Config
from trader.data.fundamentals import filter_by_quality
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class MomentumStrategy(Strategy):
    name = "momentum"
    preferred_regimes = (MarketRegime.TRENDING,)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.momentum
        self.stock_symbols = stock_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []

        signals: list[Signal] = []
        end = datetime.now()
        start = end - timedelta(days=self.cfg.slow_lookback * 2)  # extra buffer

        # 1. Fetch historical daily bars for the stock universe
        bars_by_symbol = self._fetch_bars(start, end)
        if not bars_by_symbol:
            logger.warning("No bars returned for momentum scan")
            return []

        # 2. Compute momentum scores (fast + slow)
        scores = self._compute_momentum(bars_by_symbol)
        if not scores:
            return []

        # 3. Rank and take top N (fetch extra to survive quality filter)
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_symbols = [sym for sym, _ in ranked[: self.cfg.top_n * 2]]

        # 4. Apply fundamental quality filter
        qualified = filter_by_quality(top_symbols, self.cfg)

        # 5. Build signals from qualified symbols, limited to top_n
        for sym in qualified[: self.cfg.top_n]:
            score = float(scores[sym])
            strength = min(1.0, max(0.0, score))
            signals.append(
                Signal(
                    symbol=sym,
                    direction=SignalDirection.LONG,
                    strength=strength,
                    strategy=self.name,
                    reason=f"momentum_rank combined={score:.3f}",
                    confidence=min(1.0, 0.5 + strength / 2),
                    regime=MarketRegime.TRENDING,
                )
            )

        logger.info("Momentum scan produced %d signals", len(signals))
        return signals

    def _fetch_bars(self, start: datetime, end: datetime) -> dict[str, list[Bar]]:
        chunk_size = 200
        result: dict[str, list[Bar]] = {}
        for i in range(0, len(self.stock_symbols), chunk_size):
            chunk = self.stock_symbols[i : i + chunk_size]
            try:
                result.update(self.broker.get_bars(chunk, TimeFrame.DAY, start=start, end=end))
            except Exception:
                logger.warning("Bar fetch failed for chunk at %d", i)
        return result

    def _compute_momentum(self, bars_by_symbol: dict[str, list[Bar]]) -> dict[str, float]:
        """Compute fast + slow momentum returns per symbol; return combined score."""
        results: dict[str, float] = {}

        for symbol, bars in bars_by_symbol.items():
            try:
                closes = [b.close for b in sorted(bars, key=lambda b: b.timestamp)]
                if len(closes) < self.cfg.slow_lookback:
                    continue

                fast_ret = closes[-1] / closes[-self.cfg.fast_lookback] - 1
                slow_ret = closes[-1] / closes[-self.cfg.slow_lookback] - 1
                # Combined: weighted average (fast momentum more relevant short-term)
                results[symbol] = 0.6 * fast_ret + 0.4 * slow_ret
            except Exception:
                continue

        return results
