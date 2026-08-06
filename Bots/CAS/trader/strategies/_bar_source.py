"""Shared helpers for bar-based strategies."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from trader.brokers.base import BrokerClient
from trader.brokers.types import Bar, TimeFrame

logger = logging.getLogger(__name__)


def fetch_daily_bars(
    broker: BrokerClient, symbols: list[str], days: int, chunk_size: int = 200
) -> dict[str, list[Bar]]:
    """Fetch daily bars for symbols over the trailing ``days`` window."""
    end = datetime.now()
    start = end - timedelta(days=days)
    result: dict[str, list[Bar]] = {}
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i : i + chunk_size]
        try:
            result.update(broker.get_bars(chunk, TimeFrame.DAY, start=start, end=end))
        except Exception:
            logger.warning("Bar fetch failed for chunk at %d", i)
    return {s: sorted(b, key=lambda x: x.timestamp) for s, b in result.items() if b}
