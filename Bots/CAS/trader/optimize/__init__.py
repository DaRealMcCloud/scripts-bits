"""Backtest-driven self-improvement (parameter optimizer).

This package implements an offline optimisation loop that repeatedly backtests
the trading strategies against a large, cached universe of historical bars and
walks the tunable parameters toward proven, *out-of-sample* improvements.

Design goals (see the README section "Self-improvement / parameter optimizer"):

- **Overfit resilience** — every candidate is scored on a portfolio of many
  symbols, split into walk-forward train/validation windows and k-fold symbol
  groups. The objective rewards return but penalises drawdown and
  cross-symbol/fold variance, so a parameter set that merely replicates one
  symbol's curve scores poorly.
- **Data caching** — the full universe of bars is downloaded once and cached to
  disk (pickle) so thousands of parameter evaluations reuse a single download.
- **Parallelism** — candidate/fold evaluation is spread across CPU cores with a
  process pool (graceful serial fallback on a single core).
- **Easy adoption** — the winning parameters are written to
  ``config.optimized.yaml`` and can be applied to ``config.yaml`` in one step.
"""

from trader.optimize.data_cache import BarCache, build_cache, load_cache
from trader.optimize.evaluator import EvalRequest, EvalResult, evaluate_config
from trader.optimize.optimizer import Optimizer, OptimizerState, ParamSpec
from trader.optimize.scoring import ScoreWeights, composite_score

__all__ = [
    "BarCache",
    "build_cache",
    "load_cache",
    "EvalRequest",
    "EvalResult",
    "evaluate_config",
    "Optimizer",
    "OptimizerState",
    "ParamSpec",
    "ScoreWeights",
    "composite_score",
]
