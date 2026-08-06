"""Portfolio exposure controls — concentration and correlation caps.

Feeds into the :class:`trader.risk.manager.RiskManager` to prevent piling into
correlated names or a single sector.  Kept intentionally simple and
dependency-light; sector data is optional (falls back to per-name caps only).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from trader.brokers.types import Position

logger = logging.getLogger(__name__)


class ExposureManager:
    def __init__(
        self,
        max_sector_pct: float = 0.30,
        max_name_pct: float = 0.10,
        sector_map: dict[str, str] | None = None,
    ) -> None:
        self.max_sector_pct = max_sector_pct
        self.max_name_pct = max_name_pct
        self.sector_map = sector_map or {}

    def name_exposure(self, positions: Sequence[Position], equity: float) -> dict[str, float]:
        if equity <= 0:
            return {}
        return {p.symbol: abs(p.market_value) / equity for p in positions}

    def sector_exposure(self, positions: Sequence[Position], equity: float) -> dict[str, float]:
        if equity <= 0:
            return {}
        out: dict[str, float] = {}
        for p in positions:
            sector = self.sector_map.get(p.symbol, "unknown")
            out[sector] = out.get(sector, 0.0) + abs(p.market_value) / equity
        return out

    def can_add(
        self,
        symbol: str,
        add_value: float,
        positions: Sequence[Position],
        equity: float,
    ) -> tuple[bool, str]:
        """Return (allowed, reason) for adding ``add_value`` of ``symbol``."""
        if equity <= 0:
            return False, "no equity"

        name_pct = (
            sum(abs(p.market_value) for p in positions if p.symbol == symbol) + add_value
        ) / equity
        if name_pct > self.max_name_pct:
            return False, f"name cap {name_pct:.0%} > {self.max_name_pct:.0%}"

        sector = self.sector_map.get(symbol, "unknown")
        if sector != "unknown":
            sector_val = sum(
                abs(p.market_value)
                for p in positions
                if self.sector_map.get(p.symbol) == sector
            )
            sector_pct = (sector_val + add_value) / equity
            if sector_pct > self.max_sector_pct:
                return False, f"sector {sector} cap {sector_pct:.0%} > {self.max_sector_pct:.0%}"

        return True, "ok"
