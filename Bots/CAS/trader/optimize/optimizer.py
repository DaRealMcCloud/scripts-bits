"""Coordinate / directional parameter optimizer.

The optimizer maintains a *current* Config and repeatedly:

1. Proposes candidates by perturbing each tunable parameter up and down by its
   step (a coordinate search), plus a "momentum" candidate that nudges every
   parameter in the direction that has *historically* helped.
2. Evaluates all candidates (optionally in parallel across CPU cores) with the
   robust, overfit-resistant :func:`trader.optimize.evaluator.evaluate_config`.
3. Adopts the best candidate if it beats the current score, and records, per
   parameter, whether moving up/down improved things — so future rounds can
   "adjust the parameters more in the same direction as the improving ones"
   (exactly the behaviour the user described).

Random restarts (jittering all params) periodically break out of local optima.
State is JSON-serialisable and saved after every round, so long runs resume.

Which searcher performs best is left to measurement: the coordinate+momentum
scheme here is the default, and :meth:`Optimizer.propose` is small enough to
swap for a pure-random / evolutionary strategy if experiments favour it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import random
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from trader.brokers.types import Bar
from trader.config import Config
from trader.optimize.evaluator import EvalRequest, EvalResult, evaluate_config
from trader.optimize.scoring import ScoreWeights

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParamSpec:
    """A single tunable parameter, addressed by a dotted path into Config.

    ``path`` is dotted (e.g. ``"strategies.momentum.fast_lookback"`` or
    ``"risk.atr_stop_multiplier"``). ``step`` is the perturbation applied per
    round; ``lo``/``hi`` clamp the value; ``is_int`` rounds to whole numbers.
    """

    path: str
    step: float
    lo: float
    hi: float
    is_int: bool = False


# Default tunable set — the parameters that "govern trading behaviour". Covers
# the momentum lookbacks + core risk knobs, plus the crypto-momentum analogues.
DEFAULT_PARAM_SPECS: dict[str, list[ParamSpec]] = {
    "momentum": [
        ParamSpec("strategies.momentum.fast_lookback", 5, 5, 60, is_int=True),
        ParamSpec("strategies.momentum.slow_lookback", 10, 20, 200, is_int=True),
        ParamSpec("strategies.momentum.top_n", 2, 3, 30, is_int=True),
        ParamSpec("risk.atr_stop_multiplier", 0.25, 0.5, 6.0),
        ParamSpec("risk.max_hold_days", 1, 1, 30, is_int=True),
        ParamSpec("risk.max_risk_per_trade", 0.005, 0.005, 0.05),
        ParamSpec("risk.max_positions", 1, 3, 30, is_int=True),
    ],
    "crypto_momentum": [
        ParamSpec("strategies.crypto.momentum.fast_ma", 5, 5, 60, is_int=True),
        ParamSpec("strategies.crypto.momentum.slow_ma", 10, 20, 200, is_int=True),
        ParamSpec("strategies.crypto.momentum.adx_min", 2.5, 5.0, 50.0),
        ParamSpec("strategies.crypto.momentum.top_n", 2, 3, 30, is_int=True),
        ParamSpec("risk.atr_stop_multiplier", 0.25, 0.5, 6.0),
        ParamSpec("risk.max_hold_days", 1, 1, 60, is_int=True),
        ParamSpec("risk.max_risk_per_trade", 0.005, 0.005, 0.05),
        ParamSpec("risk.max_positions", 1, 3, 30, is_int=True),
    ],
    "crypto_volatility": [
        ParamSpec("strategies.crypto.volatility.lookback", 5, 10, 60, is_int=True),
        ParamSpec("strategies.crypto.volatility.low_vol_pct", 0.05, 0.10, 1.50),
        ParamSpec("strategies.crypto.volatility.high_vol_pct", 0.10, 0.50, 3.00),
        ParamSpec("strategies.crypto.volatility.top_n", 2, 3, 30, is_int=True),
        ParamSpec("risk.atr_stop_multiplier", 0.25, 0.5, 6.0),
        ParamSpec("risk.max_hold_days", 1, 1, 60, is_int=True),
        ParamSpec("risk.max_risk_per_trade", 0.005, 0.005, 0.05),
        ParamSpec("risk.max_positions", 1, 3, 30, is_int=True),
    ],
}


# ── dotted-path get/set on a Config ─────────────────────────────────────────
def get_param(cfg: Config, path: str) -> float:
    obj = cfg
    for part in path.split("."):
        obj = getattr(obj, part)
    return float(obj)


def set_param(cfg: Config, path: str, value: float, is_int: bool = False) -> None:
    parts = path.split(".")
    obj = cfg
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], int(round(value)) if is_int else float(value))


def _clamp(value: float, spec: ParamSpec) -> float:
    value = max(spec.lo, min(spec.hi, value))
    return float(round(value)) if spec.is_int else value


@dataclass
class RoundRecord:
    """One optimizer round, for the history report."""

    round: int
    best_score: float
    accepted: bool
    changed: dict[str, float] = field(default_factory=dict)
    eval_summary: str = ""
    holdout_score: float | None = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class OptimizerState:
    """Resumable optimizer state (JSON-serialisable)."""

    strategy: str
    best_score: float = float("-inf")
    best_params: dict[str, float] = field(default_factory=dict)
    current_params: dict[str, float] = field(default_factory=dict)
    # Per-parameter running tally of net improvement contributed by +/- moves.
    # Positive => moving the param UP has tended to help; negative => DOWN helps.
    direction_bias: dict[str, float] = field(default_factory=dict)
    step_sizes: dict[str, float] = field(default_factory=dict)
    rounds: list[dict] = field(default_factory=list)
    completed_rounds: int = 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, default=str)

    @classmethod
    def from_json(cls, text: str) -> OptimizerState:
        data = json.loads(text)
        return cls(**data)


# ── module-level worker (picklable) ──────────────────────────────────────────
def _eval_worker(req: EvalRequest) -> EvalResult:
    return evaluate_config(req)


class Optimizer:
    """Directional coordinate search with per-parameter improvement memory."""

    def __init__(
        self,
        base_cfg: Config,
        series: dict[str, list[Bar]],
        *,
        strategy: str = "momentum",
        specs: list[ParamSpec] | None = None,
        weights: ScoreWeights | None = None,
        workers: int | None = None,
        starting_equity: float = 100_000.0,
        restart_every: int = 8,
        seed: int = 0,
        holdout_frac: float = 0.0,
        stagnation_patience: int = 0,
    ) -> None:
        if strategy not in DEFAULT_PARAM_SPECS and specs is None:
            raise ValueError(f"No default param specs for strategy '{strategy}'")
        self.base_cfg = base_cfg
        self.series = series
        self.strategy = strategy
        self.specs = specs or DEFAULT_PARAM_SPECS[strategy]
        self.weights = weights or ScoreWeights()
        self.starting_equity = starting_equity
        self.restart_every = restart_every
        self.rng = random.Random(seed)
        self.seed = seed
        self.holdout_frac = max(0.0, min(0.5, holdout_frac))
        self.stagnation_patience = max(0, stagnation_patience)
        cpu = os.cpu_count() or 1
        self.workers = max(1, workers if workers is not None else cpu)

        self.cfg = copy.deepcopy(base_cfg)
        self.state = OptimizerState(strategy=strategy)
        self.interrupted = False
        self.state.current_params = {s.path: get_param(self.cfg, s.path) for s in self.specs}
        self.state.direction_bias = {s.path: 0.0 for s in self.specs}
        self.state.step_sizes = {s.path: s.step for s in self.specs}

    # ── candidate generation ─────────────────────────────────
    def _apply(self, params: dict[str, float]) -> Config:
        cfg = copy.deepcopy(self.base_cfg)
        by_path = {s.path: s for s in self.specs}
        for path, value in params.items():
            set_param(cfg, path, value, by_path[path].is_int)
        return cfg

    def _is_feasible(self, params: dict[str, float]) -> bool:
        """Reject parameter combinations that the strategy cannot use safely."""
        if self.strategy == "momentum":
            return params["strategies.momentum.fast_lookback"] < params[
                "strategies.momentum.slow_lookback"
            ]
        if self.strategy == "crypto_momentum":
            return params["strategies.crypto.momentum.fast_ma"] < params[
                "strategies.crypto.momentum.slow_ma"
            ]
        if self.strategy == "crypto_volatility":
            return params["strategies.crypto.volatility.low_vol_pct"] < params[
                "strategies.crypto.volatility.high_vol_pct"
            ]
        return True

    def _step_for(self, spec: ParamSpec) -> float:
        return self.state.step_sizes.get(spec.path, spec.step)

    def propose(self, current: dict[str, float]) -> list[dict[str, float]]:
        """Return candidate parameter dicts to evaluate this round."""
        candidates: list[dict[str, float]] = []
        # 1. Coordinate moves: each param up and down by its step.
        for spec in self.specs:
            for direction in (+1, -1):
                cand = dict(current)
                cand[spec.path] = _clamp(
                    current[spec.path] + direction * self._step_for(spec), spec
                )
                if cand[spec.path] != current[spec.path] and self._is_feasible(cand):
                    candidates.append(cand)
        # 2. Momentum move: nudge every param toward its historically-helpful
        #    direction (the "adjust more in the improving direction" behaviour).
        momentum = dict(current)
        moved = False
        for spec in self.specs:
            bias = self.state.direction_bias.get(spec.path, 0.0)
            if bias != 0.0:
                step = self._step_for(spec) * (1 if bias > 0 else -1)
                nv = _clamp(current[spec.path] + step, spec)
                if nv != momentum[spec.path]:
                    momentum[spec.path] = nv
                    moved = True
        if moved and self._is_feasible(momentum):
            candidates.append(momentum)
        # De-dupe identical candidates.
        unique: list[dict[str, float]] = []
        seen: set[tuple] = set()
        for c in candidates:
            key = tuple(sorted(c.items()))
            if key not in seen:
                seen.add(key)
                unique.append(c)
        return unique

    def _local_restarts(
        self, current: dict[str, float], count: int = 4
    ) -> list[dict[str, float]]:
        """Generate feasible local perturbations around the incumbent."""
        candidates: list[dict[str, float]] = []
        for _ in range(count):
            candidate = dict(current)
            for spec in self.specs:
                radius = 2.0 * self._step_for(spec)
                candidate[spec.path] = _clamp(
                    current[spec.path] + self.rng.uniform(-radius, radius), spec
                )
            if candidate != current and self._is_feasible(candidate):
                candidates.append(candidate)
        logger.info("Local restart: generated %d feasible candidates", len(candidates))
        return candidates

    def _adapt_steps(
        self,
        current: dict[str, float],
        candidates: list[dict[str, float]],
        results: list[EvalResult],
        accepted: bool,
    ) -> None:
        """Shrink stagnant dimensions and gently expand useful dimensions."""
        for spec in self.specs:
            path = spec.path
            coordinate_scores = [
                result.score
                for candidate, result in zip(candidates, results)
                if candidate[path] != current[path]
                and all(
                    candidate[other.path] == current[other.path]
                    for other in self.specs
                    if other.path != path
                )
            ]
            if accepted and any(
                candidate[path] != current[path]
                and result.score == max(result.score for result in results)
                for candidate, result in zip(candidates, results)
            ):
                self.state.step_sizes[path] = min(
                    spec.hi - spec.lo,
                    self._step_for(spec) * 1.25,
                )
            elif not coordinate_scores or max(coordinate_scores) <= self.state.best_score:
                self.state.step_sizes[path] = max(
                    spec.step / 16.0,
                    self._step_for(spec) * 0.5,
                )

    def _random_restart(self) -> dict[str, float]:
        """Return a random feasible point for backwards-compatible callers."""
        params: dict[str, float] = {}
        for spec in self.specs:
            val = self.rng.uniform(spec.lo, spec.hi)
            params[spec.path] = _clamp(val, spec)
        if not self._is_feasible(params):
            return dict(self.state.current_params)
        logger.info("Random restart: jittered all parameters")
        return params

    # ── evaluation ───────────────────────────────────────────
    def _make_request(self, params: dict[str, float]) -> EvalRequest:
        return EvalRequest(
            cfg=self._apply(params),
            series=self.series,
            strategy=self.strategy,
            starting_equity=self.starting_equity,
            weights=self.weights,
            seed=self.seed,
        )

    def _evaluate_holdout(self, params: dict[str, float]) -> float | None:
        if self.holdout_frac <= 0:
            return None
        request = EvalRequest(
            cfg=self._apply(params),
            series=self.series,
            strategy=self.strategy,
            starting_equity=self.starting_equity,
            weights=self.weights,
            seed=self.seed,
            holdout_frac=self.holdout_frac,
        )
        return evaluate_config(request).holdout_score

    def _evaluate_many(self, param_sets: list[dict[str, float]]) -> list[EvalResult]:
        requests = [self._make_request(p) for p in param_sets]
        if self.workers <= 1 or len(requests) <= 1:
            return [evaluate_config(r) for r in requests]
        pool = ProcessPoolExecutor(max_workers=self.workers)
        try:
            results = list(pool.map(_eval_worker, requests))
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
            return results

    # ── main loop ─────────────────────────────────────────────
    def run(self, rounds: int, on_round=None) -> OptimizerState:
        """Run ``rounds`` optimisation rounds and return the final state."""
        self.interrupted = False
        stagnant_rounds = 0
        try:
            # Establish the baseline score for the starting parameters.
            if self.state.best_score == float("-inf"):
                base = evaluate_config(self._make_request(self.state.current_params))
                self.state.best_score = base.score
                self.state.best_params = dict(self.state.current_params)
                logger.info("Baseline %s", base.summary())

            for r in range(rounds):
                round_no = self.state.completed_rounds + 1
                current = dict(self.state.current_params)

                if self.restart_every and round_no % self.restart_every == 0:
                    candidates = self._local_restarts(current)
                else:
                    candidates = self.propose(current)
                if not candidates:
                    break

                results = self._evaluate_many(candidates)
                best_idx = max(range(len(results)), key=lambda i: results[i].score)
                best_cand = candidates[best_idx]
                best_res = results[best_idx]

                accepted = best_res.score > self.state.best_score
                changed: dict[str, float] = {}
                if accepted:
                    # Update direction bias for every param that moved.
                    for spec in self.specs:
                        delta = best_cand[spec.path] - current[spec.path]
                        if delta != 0:
                            gain = best_res.score - self.state.best_score
                            sign = 1.0 if delta > 0 else -1.0
                            self.state.direction_bias[spec.path] += sign * gain
                            changed[spec.path] = best_cand[spec.path]
                    self.state.current_params = dict(best_cand)
                    self.state.best_params = dict(best_cand)
                    self.state.best_score = best_res.score
                    logger.info("Round %d ACCEPTED %s | changed=%s", round_no, best_res.summary(), changed)
                else:
                    logger.info("Round %d no improvement (best cand %s)", round_no, best_res.summary())

                self._adapt_steps(current, candidates, results, accepted)
                holdout_score = (
                    self._evaluate_holdout(self.state.best_params) if accepted else None
                )

                record = RoundRecord(
                    round=round_no,
                    best_score=self.state.best_score,
                    accepted=accepted,
                    changed=changed,
                    eval_summary=best_res.summary(),
                    holdout_score=holdout_score,
                )
                self.state.rounds.append(asdict(record))
                self.state.completed_rounds = round_no
                if on_round is not None:
                    on_round(self.state, record)
                stagnant_rounds = 0 if accepted else stagnant_rounds + 1
                if self.stagnation_patience and stagnant_rounds >= self.stagnation_patience:
                    logger.info(
                        "Stopping after %d stagnant rounds at round %d",
                        stagnant_rounds,
                        round_no,
                    )
                    break
        except KeyboardInterrupt:
            self.interrupted = True
            logger.warning(
                "Optimisation interrupted after %d completed rounds; keeping the best result found so far.",
                self.state.completed_rounds,
            )

        return self.state

    def best_config(self) -> Config:
        """Return a Config with the best parameters found applied."""
        return self._apply(self.state.best_params or self.state.current_params)


# ── state persistence ─────────────────────────────────────────────────────────
def save_state(state: OptimizerState, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.to_json(), encoding="utf-8")
    return p


def load_state(path: str | Path) -> OptimizerState | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        return OptimizerState.from_json(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not load optimizer state %s: %s", p, exc)
        return None
