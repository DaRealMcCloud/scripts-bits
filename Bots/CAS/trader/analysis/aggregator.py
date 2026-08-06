"""Signal aggregation — the "which trades" decision brain.

Multiple strategies produce :class:`Signal` objects that may overlap or
conflict on the same symbol.  The :class:`SignalAggregator` combines them into a
single ranked list of the best candidate trades using:

    score = strength * confidence * strategy_weight * regime_fit

Signals on the same symbol are netted (long vs short) and the strongest side
wins.  The aggregator returns at most ``top_n`` candidates above ``min_score``.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from trader.strategies.base import MarketRegime, Signal, SignalDirection

logger = logging.getLogger(__name__)


@dataclass
class ScoredSignal:
    signal: Signal
    score: float


class SignalAggregator:
    def __init__(
        self,
        strategy_weights: dict[str, float] | None = None,
        regime_fit_bonus: float = 1.5,
        regime_fit_penalty: float = 0.5,
        top_n: int = 10,
        min_score: float = 0.05,
        use_regime_routing: bool = True,
    ) -> None:
        self.strategy_weights = strategy_weights or {}
        self.regime_fit_bonus = regime_fit_bonus
        self.regime_fit_penalty = regime_fit_penalty
        self.top_n = top_n
        self.min_score = min_score
        self.use_regime_routing = use_regime_routing
        # Which regimes each strategy prefers (populated from Strategy classes).
        self._preferred: dict[str, tuple[MarketRegime, ...]] = {}

    def register_strategy_regimes(self, name: str, regimes: tuple[MarketRegime, ...]) -> None:
        self._preferred[name] = regimes

    def _regime_fit(self, strategy: str, current: MarketRegime) -> float:
        if not self.use_regime_routing or current == MarketRegime.UNKNOWN:
            return 1.0
        preferred = self._preferred.get(strategy, ())
        if not preferred:
            return 1.0
        return self.regime_fit_bonus if current in preferred else self.regime_fit_penalty

    def score_signal(self, signal: Signal, current_regime: MarketRegime) -> float:
        weight = self.strategy_weights.get(signal.strategy, 1.0)
        fit = self._regime_fit(signal.strategy, current_regime)
        return max(0.0, signal.strength) * max(0.0, signal.confidence) * weight * fit

    def aggregate(
        self, signals: list[Signal], current_regime: MarketRegime = MarketRegime.UNKNOWN
    ) -> list[ScoredSignal]:
        """Net conflicting signals per symbol and return ranked top-N."""
        # 1. Score everything.
        scored = [
            ScoredSignal(s, self.score_signal(s, current_regime)) for s in signals
        ]

        # 2. Net long/short per symbol.
        by_symbol: dict[str, list[ScoredSignal]] = defaultdict(list)
        for ss in scored:
            by_symbol[ss.signal.symbol].append(ss)

        winners: list[ScoredSignal] = []
        for symbol, group in by_symbol.items():
            long_score = sum(g.score for g in group if g.signal.direction == SignalDirection.LONG)
            short_score = sum(
                g.score for g in group if g.signal.direction == SignalDirection.SHORT
            )
            if long_score == 0 and short_score == 0:
                continue
            if long_score >= short_score:
                net = long_score - short_score
                best = max(
                    (g for g in group if g.signal.direction == SignalDirection.LONG),
                    key=lambda g: g.score,
                )
            else:
                net = short_score - long_score
                best = max(
                    (g for g in group if g.signal.direction == SignalDirection.SHORT),
                    key=lambda g: g.score,
                )
            winners.append(ScoredSignal(best.signal, net))

        # 3. Filter + rank.
        winners = [w for w in winners if w.score >= self.min_score]
        winners.sort(key=lambda w: w.score, reverse=True)
        top = winners[: self.top_n]
        logger.info(
            "Aggregated %d signals → %d candidates (regime=%s)",
            len(signals),
            len(top),
            current_regime.value,
        )
        return top
