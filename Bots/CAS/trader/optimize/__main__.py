"""CLI entry point for the backtest-driven parameter optimizer.

Examples::

    # 1. Build (or refresh) the on-disk bar cache for the top-500 liquid names.
    python -m trader.optimize --refresh-cache --max-symbols 500 --cache-only

    # 2. Optimise equity momentum for 40 rounds across all CPU cores.
    python -m trader.optimize --strategy momentum --rounds 40

    # 3. Optimise crypto momentum, then apply the winner to config.yaml.
    python -m trader.optimize --strategy crypto_momentum --rounds 40 --apply

Outputs (under ``--out-dir``, default ``data/optimize``):
    optimize_state.json     resumable optimizer state (also the run history)
    optimize_report.csv     one row per round (score, accepted, changed params)
    config.optimized.yaml   the base config with the winning parameters merged

With ``--apply`` the winner is also written to ``config.yaml`` after backing the
existing file up to ``config.yaml.bak``.
"""

from __future__ import annotations

import argparse
import csv
import logging
import shutil
from pathlib import Path

from trader.brokers.factory import make_broker
from trader.config import Config, dump_config, load_config
from trader.logger import setup_logging
from trader.optimize.data_cache import (
    DEFAULT_CACHE_DIR,
    BarCache,
    build_cache,
    load_cache,
    save_cache,
)
from trader.optimize.optimizer import (
    DEFAULT_PARAM_SPECS,
    Optimizer,
    OptimizerState,
    load_state,
    save_state,
)
from trader.optimize.scoring import ScoreWeights

logger = logging.getLogger(__name__)

DEFAULT_OUT_DIR = Path("data") / "optimize"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backtest-driven parameter optimizer")
    p.add_argument(
        "--strategy",
        default="momentum",
        choices=sorted(DEFAULT_PARAM_SPECS),
        help="Which strategy's parameters to optimise",
    )
    p.add_argument("--rounds", type=int, default=20, help="Optimisation rounds to run")
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel worker processes (0 = all CPU cores, 1 = serial)",
    )
    p.add_argument(
        "--max-symbols",
        type=int,
        default=500,
        help="Cap symbols per asset class; equities are volume-ranked first",
    )
    p.add_argument("--days", type=int, default=1095, help="History window to cache (days)")
    p.add_argument("--config", default="config.yaml", help="Base config to start from")
    p.add_argument("--cache", default=str(DEFAULT_CACHE_DIR / "bars.pkl"), help="Bar cache path")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="Output directory")
    p.add_argument("--refresh-cache", action="store_true", help="Rebuild the bar cache")
    p.add_argument("--cache-only", action="store_true", help="Only build the cache, then exit")
    p.add_argument("--resume", action="store_true", help="Resume from saved optimizer state")
    p.add_argument("--apply", action="store_true", help="Write the winner to config.yaml (with backup)")
    p.add_argument("--restart-every", type=int, default=8, help="Random-restart cadence (rounds)")
    p.add_argument("--seed", type=int, default=0, help="Deterministic seed")
    # Composite-objective penalty weights.
    p.add_argument("--w-drawdown", type=float, default=0.5)
    p.add_argument("--w-symbol-var", type=float, default=0.5)
    p.add_argument("--w-fold-var", type=float, default=0.5)
    p.add_argument("--min-trades", type=int, default=20)
    return p.parse_args()


def _get_or_build_cache(args: argparse.Namespace, cfg: Config) -> BarCache | None:
    cache_path = Path(args.cache)
    cache = None if args.refresh_cache else load_cache(cache_path)
    if cache is None:
        include_equities = not args.strategy.startswith("crypto")
        include_crypto = args.strategy.startswith("crypto")
        logger.info("Building bar cache (this can take a while)…")
        broker = make_broker(cfg)
        broker.connect()
        cache = build_cache(
            broker,
            cfg,
            days=args.days,
            max_symbols=args.max_symbols,
            include_equities=include_equities,
            include_crypto=include_crypto,
        )
        save_cache(cache, cache_path)
    return cache


def _series_for_strategy(cache: BarCache, strategy: str) -> dict[str, list]:
    if strategy.startswith("crypto"):
        return cache.subset(cache.crypto_symbols)
    return cache.subset(cache.stock_symbols)


def _write_report(state: OptimizerState, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["round", "best_score", "accepted", "changed", "eval_summary", "timestamp"])
        for r in state.rounds:
            writer.writerow(
                [
                    r.get("round"),
                    f"{r.get('best_score', float('nan')):.4f}",
                    r.get("accepted"),
                    ";".join(f"{k}={v}" for k, v in (r.get("changed") or {}).items()),
                    r.get("eval_summary"),
                    r.get("timestamp"),
                ]
            )
    logger.info("Wrote optimisation report to %s", path)


def main() -> None:
    args = _parse_args()
    cfg = load_config(args.config)
    setup_logging(cfg)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache = _get_or_build_cache(args, cfg)
    if cache is None or not cache.bars:
        logger.error("No bar data available; cannot optimise.")
        raise SystemExit(1)

    if args.cache_only:
        logger.info("Cache-only run complete. %s", cache.summary())
        return

    series = _series_for_strategy(cache, args.strategy)
    if not series:
        logger.error("No %s symbols in the cache to optimise.", args.strategy)
        raise SystemExit(1)
    logger.info("Optimising %s over %d symbols", args.strategy, len(series))

    weights = ScoreWeights(
        dd=args.w_drawdown,
        symbol_var=args.w_symbol_var,
        fold_var=args.w_fold_var,
        min_trades=args.min_trades,
    )
    workers = None if args.workers == 0 else args.workers

    opt = Optimizer(
        cfg,
        series,
        strategy=args.strategy,
        weights=weights,
        workers=workers,
        restart_every=args.restart_every,
        seed=args.seed,
    )

    state_path = out_dir / f"optimize_state_{args.strategy}.json"
    if args.resume:
        prior = load_state(state_path)
        if prior is not None and prior.strategy == args.strategy:
            opt.state = prior
            if prior.current_params:
                opt.state.current_params = dict(prior.current_params)
            logger.info("Resumed from %s (round %d, best=%.4f)", state_path, prior.completed_rounds, prior.best_score)

    def _on_round(state: OptimizerState, _record) -> None:
        save_state(state, state_path)  # save after every round → resumable

    state = opt.run(args.rounds, on_round=_on_round)
    save_state(state, state_path)
    _write_report(state, out_dir / f"optimize_report_{args.strategy}.csv")

    best_cfg = opt.best_config()
    optimized_path = out_dir / "config.optimized.yaml"
    dump_config(best_cfg, optimized_path)

    logger.info("Best score %.4f with params: %s", state.best_score, state.best_params)
    print("\n=== Optimisation complete ===")
    print(f"Strategy      : {args.strategy}")
    print(f"Rounds run    : {state.completed_rounds}")
    print(f"Best score    : {state.best_score:.4f}")
    print("Best params   :")
    for k, v in state.best_params.items():
        print(f"  {k} = {v}")
    print(f"\nSuggested config written to: {optimized_path}")

    if args.apply:
        target = Path(args.config)
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            shutil.copy2(target, backup)
            logger.info("Backed up %s -> %s", target, backup)
        dump_config(best_cfg, target)
        print(f"Applied winning parameters to {target} (backup at {target}.bak)")
    else:
        print(
            "Not applied. To adopt, copy config.optimized.yaml over config.yaml, "
            "or re-run with --apply."
        )


if __name__ == "__main__":
    main()
