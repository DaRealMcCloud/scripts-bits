"""Tests for the SignalAggregator decision brain."""

from __future__ import annotations

from trader.analysis.aggregator import SignalAggregator
from trader.strategies.base import MarketRegime, Signal, SignalDirection


def _sig(symbol, direction, strength, strategy, conf=1.0):
    return Signal(symbol, direction, strength, strategy, "test", confidence=conf)


def test_aggregate_ranks_by_score():
    agg = SignalAggregator(min_score=0.0, top_n=10)
    signals = [
        _sig("A", SignalDirection.LONG, 0.9, "s1"),
        _sig("B", SignalDirection.LONG, 0.3, "s2"),
        _sig("C", SignalDirection.LONG, 0.6, "s3"),
    ]
    out = agg.aggregate(signals, MarketRegime.UNKNOWN)
    symbols = [ss.signal.symbol for ss in out]
    assert symbols == ["A", "C", "B"]


def test_aggregate_nets_conflicting_signals():
    agg = SignalAggregator(min_score=0.0)
    signals = [
        _sig("A", SignalDirection.LONG, 0.8, "s1"),
        _sig("A", SignalDirection.SHORT, 0.3, "s2"),
    ]
    out = agg.aggregate(signals)
    assert len(out) == 1
    assert out[0].signal.direction is SignalDirection.LONG
    # net = 0.8 - 0.3
    assert abs(out[0].score - 0.5) < 1e-9


def test_aggregate_filters_below_min_score():
    agg = SignalAggregator(min_score=0.5)
    signals = [_sig("A", SignalDirection.LONG, 0.2, "s1")]
    assert agg.aggregate(signals) == []


def test_regime_routing_boosts_preferred_strategy():
    agg = SignalAggregator(
        min_score=0.0, use_regime_routing=True,
        regime_fit_bonus=2.0, regime_fit_penalty=0.5,
    )
    agg.register_strategy_regimes("trend", (MarketRegime.TRENDING,))
    agg.register_strategy_regimes("meanrev", (MarketRegime.MEAN_REVERTING,))
    signals = [
        _sig("A", SignalDirection.LONG, 0.5, "trend"),
        _sig("B", SignalDirection.LONG, 0.5, "meanrev"),
    ]
    out = agg.aggregate(signals, MarketRegime.TRENDING)
    # trend strategy should rank first thanks to the bonus.
    assert out[0].signal.strategy == "trend"


def test_strategy_weight_applied():
    agg = SignalAggregator(strategy_weights={"heavy": 3.0}, min_score=0.0)
    signals = [
        _sig("A", SignalDirection.LONG, 0.3, "heavy"),
        _sig("B", SignalDirection.LONG, 0.5, "light"),
    ]
    out = agg.aggregate(signals)
    assert out[0].signal.strategy == "heavy"
