"""Overnight gap fade strategy — fades large opening gaps."""

from __future__ import annotations

import logging

from trader.brokers.base import BrokerClient
from trader.brokers.types import Snapshot
from trader.config import Config
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class GapFadeStrategy(Strategy):
    name = "gap_fade"
    preferred_regimes = (MarketRegime.MEAN_REVERTING, MarketRegime.RANGE_BOUND)

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.gap_fade
        self.stock_symbols = stock_symbols

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []

        signals: list[Signal] = []

        snapshots = self._get_snapshots()
        if not snapshots:
            logger.warning("No snapshots available for gap fade scan")
            return []

        count = 0
        for symbol, snap in snapshots.items():
            if count >= self.cfg.max_daily_trades:
                break

            try:
                prev_close = snap.prev_daily_close
                current_open = snap.daily_open

                if prev_close is None or current_open is None or prev_close == 0:
                    continue

                gap_pct = ((current_open - prev_close) / prev_close) * 100

                if abs(gap_pct) < self.cfg.min_gap_pct:
                    continue

                # Gap up → short (fade), gap down → long (fade)
                if gap_pct > 0:
                    direction = SignalDirection.SHORT
                    target = current_open - (current_open - prev_close) * self.cfg.target_retracement
                else:
                    direction = SignalDirection.LONG
                    target = current_open + (prev_close - current_open) * self.cfg.target_retracement

                strength = min(1.0, abs(gap_pct) / 10.0)  # 2% gap = 0.2, 10% = 1.0

                signals.append(
                    Signal(
                        symbol=symbol,
                        direction=direction,
                        strength=strength,
                        strategy=self.name,
                        reason=f"gap={gap_pct:+.1f}% target={target:.2f}",
                        confidence=min(1.0, 0.4 + strength / 2),
                        regime=MarketRegime.MEAN_REVERTING,
                        target_price=target,
                    )
                )
                count += 1

            except Exception:
                continue

        logger.info("Gap fade scan produced %d signals", len(signals))
        return signals

    def _get_snapshots(self) -> dict[str, Snapshot]:
        chunk_size = 200
        all_snaps: dict[str, Snapshot] = {}
        for i in range(0, len(self.stock_symbols), chunk_size):
            chunk = self.stock_symbols[i : i + chunk_size]
            try:
                all_snaps.update(self.broker.get_snapshots(chunk))
            except Exception:
                logger.warning("Snapshot fetch failed for chunk at %d", i)
        return all_snaps
