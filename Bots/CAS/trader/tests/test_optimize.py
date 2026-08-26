"""Tests for the backtest-driven self-improvement optimizer.

Covers:
- the volume-ranked universe pre-filter (regression for the "take the most
  liquid, not the first N" fix);
- the composite scoring (drawdown + single-symbol variance penalties);
- the config YAML serializer (with secret redaction);
- the evaluator (walk-forward + k-fold, determinism);
- the optimizer loop (improves a synthetic signal, direction memory);
- the bar cache round-trip.

Everything uses the in-memory FakeBroker / synthetic bars — no network.
"""

from __future__ import annotations

import math
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta

from trader.backtest import Backtester
from trader.config import Config, config_to_dict, dump_config, load_config
from trader.data.universe import _avg_daily_volumes, _rank_by_volume
from trader.optimize.data_cache import BarCache, load_cache, save_cache
from trader.optimize.__main__ import _cache_is_current
from trader.optimize.evaluator import EvalRequest, evaluate_config, evaluate_configs_parallel
from trader.optimize.optimizer import (
    Optimizer,
    ParamSpec,
    get_param,
    load_state,
    save_state,
    set_param,
)
from trader.optimize.scoring import ScoreWeights, aggregate_fold_scores, composite_score

from .conftest import FakeBroker, make_bars


# ── universe: volume-ranked pre-filter ───────────────────────────────────────
def _bars_with_volume(symbol: str, n: int, volume: float) -> list:
    return make_bars(symbol, [100 + i * 0.1 for i in range(n)], volume=volume)


def test_rank_by_volume_keeps_most_liquid_not_first():
    # LOWVOL is listed first but is illiquid; HIGH1/HIGH2 are the liquid names.
    bars = {
        "LOWVOL": _bars_with_volume("LOWVOL", 40, volume=1_000),
        "HIGH1": _bars_with_volume("HIGH1", 40, volume=9_000_000),
        "MIDVOL": _bars_with_volume("MIDVOL", 40, volume=500_000),
        "HIGH2": _bars_with_volume("HIGH2", 40, volume=8_000_000),
    }
    broker = FakeBroker(bars=bars)
    cfg = Config()
    cfg.universe.min_avg_volume = 0  # don't let the floor mask the ranking
    top = _rank_by_volume(broker, list(bars.keys()), cfg, top_n=2)
    assert top == ["HIGH1", "HIGH2"]  # highest volume first, NOT ["LOWVOL", "HIGH1"]


def test_rank_by_volume_applies_min_volume_floor():
    bars = {
        "A": _bars_with_volume("A", 40, volume=100),
        "B": _bars_with_volume("B", 40, volume=2_000_000),
    }
    broker = FakeBroker(bars=bars)
    cfg = Config()
    cfg.universe.min_avg_volume = 500_000
    top = _rank_by_volume(broker, ["A", "B"], cfg, top_n=5)
    assert top == ["B"]  # A dropped by the floor even though top_n allows both


def test_avg_daily_volumes_reports_per_symbol():
    bars = {"X": _bars_with_volume("X", 10, volume=1_234_567)}
    broker = FakeBroker(bars=bars)
    vols = _avg_daily_volumes(broker, ["X"])
    assert math.isclose(vols["X"], 1_234_567, rel_tol=1e-6)


# ── config serializer ────────────────────────────────────────────────────────
def test_config_to_dict_redacts_secrets():
    cfg = Config()
    cfg.alpaca.api_key = "PKSECRET"
    cfg.alpaca.secret_key = "SHHH"
    data = config_to_dict(cfg)
    assert "api_key" not in data["alpaca"]
    assert "secret_key" not in data["alpaca"]
    # Non-secret fields survive.
    assert data["risk"]["max_positions"] == cfg.risk.max_positions


def test_dump_config_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setenv("BROKER_PROVIDER", "ibkr")  # avoids Alpaca cred check on load
    cfg = Config()
    cfg.strategies.momentum.fast_lookback = 33
    cfg.risk.atr_stop_multiplier = 3.5
    path = tmp_path / "out.yaml"
    dump_config(cfg, path)
    reloaded = load_config(path)
    assert reloaded.strategies.momentum.fast_lookback == 33
    assert reloaded.risk.atr_stop_multiplier == 3.5


# ── scoring ──────────────────────────────────────────────────────────────────
def test_composite_penalises_single_symbol_concentration():
    # Same mean return, but one is spread evenly and one is all in one symbol.
    even = composite_score(
        mean_return_pct=10.0,
        max_drawdown_pct=0.0,
        per_symbol_returns=[10.0, 10.0, 10.0, 10.0],
        num_trades=100,
    )
    concentrated = composite_score(
        mean_return_pct=10.0,
        max_drawdown_pct=0.0,
        per_symbol_returns=[40.0, 0.0, 0.0, 0.0],
        num_trades=100,
    )
    assert even > concentrated  # variance penalty punishes concentration


def test_composite_penalises_drawdown():
    low_dd = composite_score(
        mean_return_pct=10.0, max_drawdown_pct=2.0,
        per_symbol_returns=[10.0, 10.0], num_trades=100,
    )
    high_dd = composite_score(
        mean_return_pct=10.0, max_drawdown_pct=30.0,
        per_symbol_returns=[10.0, 10.0], num_trades=100,
    )
    assert low_dd > high_dd


def test_composite_sparse_trades_penalty():
    plenty = composite_score(
        mean_return_pct=10.0, max_drawdown_pct=0.0,
        per_symbol_returns=[10.0], num_trades=100,
    )
    sparse = composite_score(
        mean_return_pct=10.0, max_drawdown_pct=0.0,
        per_symbol_returns=[10.0], num_trades=1,
    )
    assert plenty > sparse


def test_aggregate_fold_scores_penalises_inconsistency():
    steady = aggregate_fold_scores([5.0, 5.0, 5.0])
    swingy = aggregate_fold_scores([15.0, -5.0, 5.0])  # same mean, higher spread
    assert steady > swingy


# ── evaluator ────────────────────────────────────────────────────────────────
def _uptrend_universe(n_symbols: int = 6, length: int = 160) -> dict[str, list]:
    series = {}
    for k in range(n_symbols):
        base = 50 + k * 5
        closes = [base + i * (0.5 + 0.1 * k) for i in range(length)]
        series[f"SYM{k}"] = make_bars(f"SYM{k}", closes)
    return series


def test_evaluate_config_is_deterministic():
    series = _uptrend_universe()
    cfg = Config()
    cfg.strategies.momentum.slow_lookback = 30
    req = EvalRequest(cfg=cfg, series=series, strategy="momentum", n_symbol_folds=3, n_time_windows=2)
    r1 = evaluate_config(req)
    r2 = evaluate_config(req)
    assert r1.score == r2.score
    assert r1.n_folds_evaluated == r2.n_folds_evaluated


def test_parallel_evaluator_matches_serial_reference():
    series = _uptrend_universe(n_symbols=8, length=180)
    cfg = Config()
    cfg.strategies.momentum.slow_lookback = 30
    requests = [
        EvalRequest(
            cfg=cfg,
            series=series,
            strategy="momentum",
            n_symbol_folds=3,
            n_time_windows=2,
            weights=ScoreWeights(min_trades=0),
        ),
        EvalRequest(
            cfg=cfg,
            series=series,
            strategy="momentum",
            n_symbol_folds=2,
            n_time_windows=3,
            weights=ScoreWeights(min_trades=0),
        ),
    ]

    serial = [evaluate_config(request) for request in requests]
    with ProcessPoolExecutor(max_workers=2) as executor:
        parallel = evaluate_configs_parallel(requests, executor)

    for expected, actual in zip(serial, parallel):
        assert actual.score == expected.score
        assert actual.train_score == expected.train_score
        assert actual.validation_score == expected.validation_score
        assert actual.total_return_pct == expected.total_return_pct
        assert actual.max_drawdown_pct == expected.max_drawdown_pct
        assert actual.num_trades == expected.num_trades
        assert actual.n_folds_evaluated == expected.n_folds_evaluated
        assert actual.per_fold_scores == expected.per_fold_scores


def test_symbol_folds_are_stable():
    from trader.optimize.evaluator import _symbol_folds

    symbols = [f"SYM{i}" for i in range(17)]
    first = _symbol_folds(symbols, n_folds=4, seed=42)
    second = _symbol_folds(list(reversed(symbols)), n_folds=4, seed=42)

    assert first == second


def test_evaluate_config_reports_validation_separately():
    series = _uptrend_universe()
    cfg = Config()
    cfg.strategies.momentum.slow_lookback = 30
    req = EvalRequest(cfg=cfg, series=series, strategy="momentum", n_symbol_folds=2, n_time_windows=2)
    res = evaluate_config(req)
    assert res.n_folds_evaluated > 0
    assert isinstance(res.validation_score, float)


def test_evaluate_config_reports_holdout_without_changing_selection_score():
    series = _uptrend_universe(length=180)
    cfg = Config()
    request = EvalRequest(cfg=cfg, series=series, strategy="momentum", holdout_frac=0.2)
    selection = evaluate_config(
        EvalRequest(cfg=cfg, series=series, strategy="momentum")
    )
    result = evaluate_config(request)

    assert result.score == selection.score
    assert result.holdout_score is not None


def test_evaluate_empty_series_is_worst_score():
    res = evaluate_config(EvalRequest(cfg=Config(), series={}, strategy="momentum"))
    assert res.score == float("-inf")


# ── optimizer ────────────────────────────────────────────────────────────────
def test_optimizer_improves_or_holds_baseline():
    series = _uptrend_universe(n_symbols=8, length=200)
    cfg = Config()
    # Start from a deliberately poor lookback so there's room to improve.
    cfg.strategies.momentum.slow_lookback = 120
    cfg.strategies.momentum.fast_lookback = 60
    opt = Optimizer(
        cfg, series, strategy="momentum",
        weights=ScoreWeights(min_trades=0),  # small synthetic data
        workers=1, restart_every=0, seed=1,
    )
    state = opt.run(rounds=6)
    # The optimizer must never end below its own baseline (first recorded round).
    assert state.best_score >= state.rounds[0]["best_score"] - 1e-9
    assert state.completed_rounds == 6


def test_optimizer_records_direction_bias_on_acceptance():
    series = _uptrend_universe(n_symbols=8, length=200)
    cfg = Config()
    cfg.strategies.momentum.slow_lookback = 120
    opt = Optimizer(
        cfg, series, strategy="momentum",
        weights=ScoreWeights(min_trades=0), workers=1, restart_every=0, seed=2,
    )
    opt.run(rounds=5)
    # At least one parameter should have accumulated a non-zero direction bias
    # if any round was accepted.
    accepted_any = any(r["accepted"] for r in opt.state.rounds)
    if accepted_any:
        assert any(v != 0.0 for v in opt.state.direction_bias.values())


def test_optimizer_best_config_applies_params():
    series = _uptrend_universe()
    cfg = Config()
    opt = Optimizer(cfg, series, strategy="momentum", workers=1, restart_every=0)
    opt.run(rounds=3)
    best = opt.best_config()
    # best_config reflects state.best_params for the momentum lookbacks.
    if opt.state.best_params:
        assert get_param(best, "strategies.momentum.fast_lookback") == \
            opt.state.best_params["strategies.momentum.fast_lookback"]


def test_optimizer_state_roundtrip(tmp_path):
    series = _uptrend_universe()
    opt = Optimizer(Config(), series, strategy="momentum", workers=1, restart_every=0)
    opt.run(rounds=2)
    path = tmp_path / "state.json"
    save_state(opt.state, path)
    reloaded = load_state(path)
    assert reloaded is not None
    assert reloaded.strategy == "momentum"
    assert reloaded.completed_rounds == 2


def test_optimizer_keeps_partial_state_on_keyboard_interrupt(monkeypatch):
    series = _uptrend_universe()
    opt = Optimizer(
        Config(),
        series,
        strategy="momentum",
        weights=ScoreWeights(min_trades=0),
        workers=1,
        restart_every=0,
    )

    def interrupt(_param_sets):
        raise KeyboardInterrupt

    monkeypatch.setattr(opt, "_evaluate_many", interrupt)
    state = opt.run(rounds=3)

    assert opt.interrupted
    assert state.completed_rounds == 0
    assert state.best_params == state.current_params


# ── param get/set helpers ─────────────────────────────────────────────────────
def test_set_param_respects_int_flag():
    cfg = Config()
    set_param(cfg, "strategies.momentum.fast_lookback", 17.6, is_int=True)
    assert cfg.strategies.momentum.fast_lookback == 18
    set_param(cfg, "risk.atr_stop_multiplier", 2.75, is_int=False)
    assert cfg.risk.atr_stop_multiplier == 2.75


def test_param_spec_is_frozen_dataclass():
    spec = ParamSpec("risk.max_positions", 1, 3, 30, is_int=True)
    assert spec.is_int and spec.hi == 30


# ── crypto strategy backtest replay ───────────────────────────────────────────
def test_backtester_momentum_handles_inverted_fast_slow():
    # A candidate the optimizer might propose: fast_lookback > slow_lookback.
    # Must not raise IndexError; should simply produce a (possibly empty) result.
    series = _uptrend_universe(n_symbols=3, length=120)
    cfg = Config()
    cfg.strategies.momentum.slow_lookback = 20
    cfg.strategies.momentum.fast_lookback = 60  # inverted on purpose
    bt = Backtester(cfg, strategy="momentum")
    result = bt.run_on_series(series)  # should not raise
    assert isinstance(result.total_return_pct, float)


def test_backtester_crypto_momentum_runs():
    # A steady crypto uptrend so dual-MA + ADX can trigger.
    closes = [100 + i * 0.8 for i in range(200)]
    series = {"BTC/USD": make_bars("BTC/USD", closes)}
    cfg = Config()
    cfg.strategies.crypto.momentum.slow_ma = 30
    cfg.strategies.crypto.momentum.fast_ma = 10
    cfg.strategies.crypto.momentum.adx_min = 0.0  # don't block the synthetic trend
    bt = Backtester(cfg, strategy="crypto_momentum")
    result = bt.run_on_series(series)
    assert isinstance(result.total_return_pct, float)
    assert isinstance(result.max_drawdown_pct, float)


# ── bar cache ─────────────────────────────────────────────────────────────────
def test_bar_cache_roundtrip(tmp_path):
    bars = {"AAA": make_bars("AAA", [1, 2, 3]), "BTC/USD": make_bars("BTC/USD", [4, 5, 6])}
    cache = BarCache(bars=bars, start=datetime(2024, 1, 1), end=datetime(2024, 6, 1))
    assert cache.stock_symbols == ["AAA"]
    assert cache.crypto_symbols == ["BTC/USD"]
    path = tmp_path / "bars.pkl"
    save_cache(cache, path)
    reloaded = load_cache(path)
    assert reloaded is not None
    assert set(reloaded.bars) == {"AAA", "BTC/USD"}
    assert reloaded.subset(["AAA"]).keys() == {"AAA"}


def test_cache_current_requires_requested_history_and_asset_class():
    now = datetime.now()
    cache = BarCache(
        bars={"BTC/USD": make_bars("BTC/USD", [4, 5, 6])},
        start=now - timedelta(days=365),
        end=now,
    )
    crypto_args = type("Args", (), {"days": 300, "strategy": "crypto_momentum"})()
    equity_args = type("Args", (), {"days": 300, "strategy": "momentum"})()

    assert _cache_is_current(cache, crypto_args)
    assert not _cache_is_current(cache, equity_args)
    crypto_args.days = 500
    assert not _cache_is_current(cache, crypto_args)


def test_load_cache_missing_returns_none(tmp_path):
    assert load_cache(tmp_path / "nope.pkl") is None
