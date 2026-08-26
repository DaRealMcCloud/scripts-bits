"""Evaluate a Config against cached bars using walk-forward + k-fold splits.

This is the workhorse the optimizer calls (in parallel) for every candidate
parameter set. It is deliberately a **module-level, picklable** function taking
plain data so it can be dispatched to a ``ProcessPoolExecutor`` worker.

Overfit resilience is built in via two orthogonal splits:

- **Walk-forward windows** — the history is chopped into consecutive time
  windows so a parameter set must work across different market regimes, not just
  the most recent stretch.
- **K-fold symbol groups** — symbols are partitioned into folds; each fold is
  backtested separately so profit concentrated in one symbol shows up as high
  cross-fold variance and is penalised.

The per-symbol return spread *within* each fold is also penalised (see
:mod:`trader.optimize.scoring`), directly discouraging single-symbol fitting.
"""

from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass, field
from datetime import datetime

from trader.backtest import Backtester
from trader.brokers.types import Bar
from trader.config import Config
from trader.optimize.scoring import (
    ScoreWeights,
    aggregate_fold_scores,
    composite_score,
)

logger = logging.getLogger(__name__)


@dataclass
class EvalRequest:
    """Everything a worker needs to score one Config (all picklable)."""

    cfg: Config
    series: dict[str, list[Bar]]
    strategy: str = "momentum"
    starting_equity: float = 100_000.0
    n_symbol_folds: int = 4
    n_time_windows: int = 3
    # Fraction of each time window reserved for out-of-sample validation.
    validation_frac: float = 0.3
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    seed: int = 0
    holdout_frac: float = 0.0


@dataclass
class EvalResult:
    """Aggregated score plus the diagnostics that explain it."""

    score: float
    train_score: float
    validation_score: float
    total_return_pct: float
    max_drawdown_pct: float
    num_trades: int
    n_folds_evaluated: int
    per_fold_scores: list[float] = field(default_factory=list)
    holdout_score: float | None = None

    def summary(self) -> str:
        return (
            f"score={self.score:+.3f} (train={self.train_score:+.3f} "
            f"val={self.validation_score:+.3f}) ret={self.total_return_pct:+.2f}% "
            f"dd={self.max_drawdown_pct:.1f}% trades={self.num_trades} "
            f"folds={self.n_folds_evaluated}"
            + (f" holdout={self.holdout_score:+.3f}" if self.holdout_score is not None else "")
        )


def _symbol_folds(symbols: list[str], n_folds: int, seed: int) -> list[list[str]]:
    """Deterministically partition symbols into ``n_folds`` groups.

    A simple stride assignment on a stably-shuffled list keeps folds balanced
    and reproducible without importing numpy.
    """
    ordered = sorted(symbols)
    # Stable pseudo-shuffle: Python's built-in hash is intentionally randomized
    # between interpreter processes, so it cannot be used for reproducible folds.
    ordered.sort(
        key=lambda s: hashlib.sha256(f"{seed}:{s}".encode("utf-8")).digest()
    )
    n_folds = max(1, min(n_folds, len(ordered) or 1))
    folds: list[list[str]] = [[] for _ in range(n_folds)]
    for i, s in enumerate(ordered):
        folds[i % n_folds].append(s)
    return [f for f in folds if f]


def _time_windows(
    series: dict[str, list[Bar]], n_windows: int
) -> list[tuple[datetime, datetime]]:
    """Split the overall date span into ``n_windows`` consecutive windows."""
    all_dates = sorted({b.timestamp for bars in series.values() for b in bars})
    if len(all_dates) < 2:
        return []
    n_windows = max(1, n_windows)
    size = len(all_dates) // n_windows
    if size < 2:
        return [(all_dates[0], all_dates[-1])]
    windows: list[tuple[datetime, datetime]] = []
    for w in range(n_windows):
        lo = w * size
        hi = (w + 1) * size - 1 if w < n_windows - 1 else len(all_dates) - 1
        windows.append((all_dates[lo], all_dates[hi]))
    return windows


def _slice_series(
    series: dict[str, list[Bar]], start: datetime, end: datetime
) -> dict[str, list[Bar]]:
    out: dict[str, list[Bar]] = {}
    for sym, bars in series.items():
        kept = [b for b in bars if start <= b.timestamp <= end]
        if kept:
            out[sym] = kept
    return out


def _split_train_val(
    bars: list[Bar], validation_frac: float
) -> tuple[list[Bar], list[Bar]]:
    """Split one window's bars into a leading train part and trailing val part."""
    if len(bars) < 4 or validation_frac <= 0:
        return bars, []
    cut = int(len(bars) * (1 - validation_frac))
    cut = max(2, min(cut, len(bars) - 2))
    return bars[:cut], bars[cut:]


def _score_series(
    cfg: Config,
    series: dict[str, list[Bar]],
    strategy: str,
    starting_equity: float,
    weights: ScoreWeights,
) -> tuple[float, float, float, int] | None:
    """Backtest one series slice; return (score, return%, dd%, trades) or None."""
    if not series:
        return None
    bt = Backtester(cfg, starting_equity=starting_equity, strategy=strategy)
    result = bt.run_on_series(series)
    per_symbol = list(result.per_symbol_return_pct.values())
    if not per_symbol:
        return None
    mean_ret = sum(per_symbol) / len(per_symbol)
    score = composite_score(
        mean_return_pct=mean_ret,
        max_drawdown_pct=result.max_drawdown_pct,
        per_symbol_returns=per_symbol,
        num_trades=result.num_trades,
        weights=weights,
    )
    return score, result.total_return_pct, result.max_drawdown_pct, result.num_trades


def evaluate_config(req: EvalRequest) -> EvalResult:
    """Score a Config across walk-forward windows × symbol folds on cached bars.

    Returns an :class:`EvalResult` whose ``score`` is the robust aggregate the
    optimizer maximises. ``validation_score`` (out-of-sample) is reported
    separately so the optimizer can prefer params that generalise.
    """
    if req.holdout_frac > 0:
        dates = sorted({bar.timestamp for bars in req.series.values() for bar in bars})
        if len(dates) >= 2:
            split_at = max(1, min(len(dates) - 1, int(len(dates) * (1 - req.holdout_frac))))
            selection_end = dates[split_at - 1]
            selection = {
                symbol: [bar for bar in bars if bar.timestamp <= selection_end]
                for symbol, bars in req.series.items()
            }
            holdout = {
                symbol: [bar for bar in bars if bar.timestamp > selection_end]
                for symbol, bars in req.series.items()
            }
            selection_result = evaluate_config(
                EvalRequest(
                    cfg=req.cfg,
                    series=selection,
                    strategy=req.strategy,
                    starting_equity=req.starting_equity,
                    n_symbol_folds=req.n_symbol_folds,
                    n_time_windows=req.n_time_windows,
                    validation_frac=req.validation_frac,
                    weights=req.weights,
                    seed=req.seed,
                )
            )
            holdout_result = evaluate_config(
                EvalRequest(
                    cfg=req.cfg,
                    series=holdout,
                    strategy=req.strategy,
                    starting_equity=req.starting_equity,
                    n_symbol_folds=req.n_symbol_folds,
                    n_time_windows=1,
                    validation_frac=0.0,
                    weights=req.weights,
                    seed=req.seed,
                )
            )
            selection_result.holdout_score = holdout_result.score
            return selection_result

    symbols = list(req.series.keys())
    folds = _symbol_folds(symbols, req.n_symbol_folds, req.seed)
    windows = _time_windows(req.series, req.n_time_windows)
    if not windows:
        return EvalResult(
            score=float("-inf"),
            train_score=float("-inf"),
            validation_score=float("-inf"),
            total_return_pct=0.0,
            max_drawdown_pct=0.0,
            num_trades=0,
            n_folds_evaluated=0,
        )

    fold_scores: list[float] = []
    train_scores: list[float] = []
    val_scores: list[float] = []
    ret_acc: list[float] = []
    dd_acc: list[float] = []
    trades_total = 0

    for fold_syms in folds:
        fold_series_full = {s: req.series[s] for s in fold_syms if s in req.series}
        for win_start, win_end in windows:
            win_series = _slice_series(fold_series_full, win_start, win_end)
            if not win_series:
                continue
            # Train / validation split within the window.
            train_series: dict[str, list[Bar]] = {}
            val_series: dict[str, list[Bar]] = {}
            for sym, bars in win_series.items():
                tr, va = _split_train_val(bars, req.validation_frac)
                if tr:
                    train_series[sym] = tr
                if va:
                    val_series[sym] = va

            train = _score_series(
                req.cfg, train_series, req.strategy, req.starting_equity, req.weights
            )
            if train is not None:
                train_scores.append(train[0])
                fold_scores.append(train[0])
                ret_acc.append(train[1])
                dd_acc.append(train[2])
                trades_total += train[3]

            val = _score_series(
                req.cfg, val_series, req.strategy, req.starting_equity, req.weights
            )
            if val is not None:
                val_scores.append(val[0])

    if not fold_scores:
        return EvalResult(
            score=float("-inf"),
            train_score=float("-inf"),
            validation_score=float("-inf"),
            total_return_pct=0.0,
            max_drawdown_pct=0.0,
            num_trades=trades_total,
            n_folds_evaluated=0,
        )

    train_agg = aggregate_fold_scores(train_scores, req.weights)
    val_agg = aggregate_fold_scores(val_scores, req.weights) if val_scores else train_agg
    # Final objective favours out-of-sample: average the (robust) train and
    # validation aggregates, so a param set must generalise to score well.
    final = 0.5 * train_agg + 0.5 * val_agg

    return EvalResult(
        score=final,
        train_score=train_agg,
        validation_score=val_agg,
        total_return_pct=sum(ret_acc) / len(ret_acc) if ret_acc else 0.0,
        max_drawdown_pct=max(dd_acc) if dd_acc else 0.0,
        num_trades=trades_total,
        n_folds_evaluated=len(fold_scores),
        per_fold_scores=fold_scores,
    )
