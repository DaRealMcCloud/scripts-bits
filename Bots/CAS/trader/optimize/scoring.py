"""Composite, overfit-resistant scoring for the optimizer.

The objective rewards return but *penalises* three things that betray an
over-fit or fragile parameter set:

1. **Drawdown** — a high return bought with a deep equity dip is not robust.
2. **Cross-symbol variance** — if profit is concentrated in one or two symbols,
   the parameters have likely learned that symbol's idiosyncratic curve rather
   than a general edge. Penalising the spread of per-symbol returns is the core
   guard the user asked for ("must not fit a single symbol").
3. **Cross-fold variance** — inconsistent scores across walk-forward windows /
   symbol folds indicate luck rather than a stable edge.

All weights are configurable so the trade-off can be tuned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreWeights:
    """Penalty weights for the composite objective.

    ``score = mean_return_pct
              - dd * max_drawdown_pct
              - symbol_var * stdev(per_symbol_returns)
              - fold_var * stdev(per_fold_scores)
              - trade_penalty (if too few trades)``
    """

    dd: float = 0.5
    symbol_var: float = 0.5
    fold_var: float = 0.5
    # Minimum number of *closed* trades before a result is trusted; sparse
    # results get a large penalty so the optimizer doesn't chase a lucky 1-trade
    # win. 0 disables the guard.
    min_trades: int = 20
    sparse_penalty: float = 50.0


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def composite_score(
    *,
    mean_return_pct: float,
    max_drawdown_pct: float,
    per_symbol_returns: list[float],
    num_trades: int,
    weights: ScoreWeights = ScoreWeights(),
) -> float:
    """Score a single backtest fold. Higher is better.

    ``per_symbol_returns`` is the list of average per-symbol return percentages
    (one entry per symbol that traded). Its spread is the cross-symbol penalty.
    """
    symbol_spread = _stdev(per_symbol_returns)
    score = (
        mean_return_pct
        - weights.dd * max_drawdown_pct
        - weights.symbol_var * symbol_spread
    )
    if weights.min_trades > 0 and num_trades < weights.min_trades:
        # Scale the penalty by how far short we fell (fully applied at 0 trades).
        shortfall = (weights.min_trades - num_trades) / weights.min_trades
        score -= weights.sparse_penalty * shortfall
    return score


def aggregate_fold_scores(
    fold_scores: list[float], weights: ScoreWeights = ScoreWeights()
) -> float:
    """Combine per-fold scores into one number, penalising their spread.

    A parameter set that scores well on average *and* consistently across folds
    beats one that is great on one fold and poor on another (a robustness /
    anti-overfit signal).
    """
    if not fold_scores:
        return float("-inf")
    mean = sum(fold_scores) / len(fold_scores)
    return mean - weights.fold_var * _stdev(fold_scores)
