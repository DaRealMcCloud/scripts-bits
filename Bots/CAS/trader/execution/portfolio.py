"""Portfolio tracker — reads positions from the broker and logs P&L.

Broker-neutral: the broker already returns :class:`trader.brokers.types.Position`
DTOs, so this module is mostly a logging/summary helper.
"""

from __future__ import annotations

import logging

from trader.brokers.base import BrokerClient
from trader.brokers.types import Position

logger = logging.getLogger(__name__)

# Kept as an alias for backward compatibility with earlier imports.
PositionSummary = Position


def get_portfolio(broker: BrokerClient) -> list[Position]:
    """Fetch current positions as neutral Position DTOs."""
    return broker.get_positions()


def log_portfolio(broker: BrokerClient) -> None:
    """Log the current portfolio state."""
    positions = get_portfolio(broker)
    if not positions:
        logger.info("Portfolio: no open positions")
        return

    total_value = sum(p.market_value for p in positions)
    total_pl = sum(p.unrealized_pl for p in positions)

    logger.info(
        "Portfolio: %d positions, value=$%.2f, unrealized P&L=$%.2f",
        len(positions),
        total_value,
        total_pl,
    )

    for p in positions:
        logger.info(
            "  %s %s %.0f @ $%.2f → $%.2f  P&L=$%.2f (%.1f%%)",
            p.side.value.upper(),
            p.symbol,
            p.abs_qty,
            p.avg_entry_price,
            p.current_price,
            p.unrealized_pl,
            p.unrealized_pl_pct,
        )
