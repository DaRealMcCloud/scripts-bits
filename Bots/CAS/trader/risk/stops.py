"""Stop-loss logic — ATR-based stops, time exits, EOD flatten.

Broker-neutral: consumes :class:`trader.brokers.types.Bar` sequences and any
:class:`trader.brokers.base.BrokerClient`.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta

from trader.brokers.base import BrokerClient
from trader.brokers.types import Bar, TimeFrame
from trader.config import Config

logger = logging.getLogger(__name__)


def compute_atr(bars: Sequence[Bar], period: int = 14) -> float:
    """Compute Average True Range from daily bars for a single symbol."""
    if len(bars) < period + 1:
        return 0.0

    ordered = sorted(bars, key=lambda b: b.timestamp)
    trs: list[float] = []
    prev_close = ordered[0].close
    for b in ordered[1:]:
        tr = max(
            b.high - b.low,
            abs(b.high - prev_close),
            abs(b.low - prev_close),
        )
        trs.append(tr)
        prev_close = b.close

    if not trs:
        return 0.0
    window = trs[-period:] if len(trs) >= period else trs
    return float(sum(window) / len(window))


def get_stop_price(
    broker: BrokerClient,
    symbol: str,
    entry_price: float,
    side: str,
    cfg: Config,
) -> float:
    """Calculate initial stop price using ATR."""
    end = datetime.now()
    start = end - timedelta(days=30)

    atr = 0.0
    try:
        bars_by_symbol = broker.get_bars([symbol], TimeFrame.DAY, start=start, end=end)
        bars = bars_by_symbol.get(symbol, [])
        if bars:
            atr = compute_atr(bars)
    except Exception:
        atr = 0.0

    if atr == 0:
        return entry_price * (0.98 if side == "long" else 1.02)

    stop_distance = atr * cfg.risk.atr_stop_multiplier

    if side == "long":
        return entry_price - stop_distance
    return entry_price + stop_distance


def should_time_exit(entry_time: datetime, cfg: Config) -> bool:
    """Return True if position has been held longer than max_hold_days."""
    if cfg.risk.max_hold_days <= 0:
        return False
    elapsed = datetime.now() - entry_time
    return elapsed > timedelta(days=cfg.risk.max_hold_days)
