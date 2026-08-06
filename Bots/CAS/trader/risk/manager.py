"""Risk manager — position sizing, drawdown breaker, concentration limits."""

from __future__ import annotations

import logging

from trader.brokers.base import BrokerClient
from trader.config import Config
from trader.strategies.base import Signal

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, broker: BrokerClient, cfg: Config) -> None:
        self.broker = broker
        self.cfg = cfg.risk
        self._start_of_day_equity: float | None = None
        self._halted = False

    def record_start_of_day(self) -> None:
        self._start_of_day_equity = self.broker.get_equity()
        self._halted = False
        logger.info("SOD equity recorded: $%.2f", self._start_of_day_equity)

    @property
    def is_halted(self) -> bool:
        return self._halted

    def check_drawdown(self) -> bool:
        """Return True if trading should continue, False if breaker has tripped."""
        if self._halted:
            return False
        if self._start_of_day_equity is None:
            return True

        current = self.broker.get_equity()
        drawdown = (self._start_of_day_equity - current) / self._start_of_day_equity

        if drawdown >= self.cfg.max_daily_drawdown:
            logger.error(
                "DAILY DRAWDOWN BREAKER: %.2f%% loss (limit %.2f%%). Halting trading.",
                drawdown * 100,
                self.cfg.max_daily_drawdown * 100,
            )
            self._halted = True
            return False
        return True

    def can_open_position(self, asset_class: str = "us_equity") -> bool:
        """Check if we can open another position (max position limit).

        Equity and crypto are counted separately. Crypto uses
        ``max_crypto_positions`` where 0 means unlimited.
        """
        if self._halted:
            return False
        positions = self.broker.get_positions()
        if asset_class == "crypto":
            limit = self.cfg.max_crypto_positions
            if limit <= 0:  # 0 = unlimited
                return True
            crypto_open = sum(1 for p in positions if "/" in p.symbol)
            if crypto_open >= limit:
                logger.info("Max crypto positions (%d) reached", limit)
                return False
            return True
        # Equity: count only non-crypto positions against max_positions.
        equity_open = sum(1 for p in positions if "/" not in p.symbol)
        if equity_open >= self.cfg.max_positions:
            logger.info("Max positions (%d) reached", self.cfg.max_positions)
            return False
        return True

    def compute_position_size(
        self,
        signal: Signal,
        entry_price: float,
        stop_price: float,
        allow_fractional: bool = False,
    ) -> float:
        """Compute position size based on fixed-fractional risk sizing.

        Risk per trade = max_risk_per_trade * equity.
        Units = risk_amount / abs(entry_price - stop_price).

        When ``allow_fractional`` is True (crypto), the size is NOT floored to a
        whole unit — otherwise a single BTC unit would exceed the risk budget and
        round down to zero.
        """
        if entry_price <= 0 or stop_price <= 0:
            return 0.0

        equity = self.broker.get_equity()
        risk_amount = equity * self.cfg.max_risk_per_trade
        risk_per_share = abs(entry_price - stop_price)

        if risk_per_share == 0:
            return 0.0

        shares = risk_amount / risk_per_share

        # Cap at 5% of equity in a single position value
        max_position_value = equity * 0.05
        max_shares_by_value = max_position_value / entry_price
        shares = min(shares, max_shares_by_value)

        if allow_fractional:
            # Keep fractional units (round to 8 dp, typical crypto precision).
            return max(0.0, round(float(shares), 8))

        # Equities: round down to avoid over-sizing.
        shares = max(0.0, float(int(shares)))
        return shares
