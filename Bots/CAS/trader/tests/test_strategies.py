"""Tests for individual strategy signal generation."""

from __future__ import annotations

from trader.brokers.types import Quote, Snapshot
from trader.config import Config
from trader.strategies.base import SignalDirection
from trader.strategies.gap_fade import GapFadeStrategy

from .conftest import FakeBroker


def test_gap_fade_shorts_gap_up():
    # prev close 100, opens at 110 → +10% gap up → fade short.
    snaps = {"AAA": Snapshot("AAA", daily_open=110.0, prev_daily_close=100.0)}
    broker = FakeBroker(snapshots=snaps)
    cfg = Config()
    cfg.strategies.gap_fade.enabled = True
    cfg.strategies.gap_fade.min_gap_pct = 2.0
    strat = GapFadeStrategy(broker, cfg, ["AAA"])
    signals = strat.scan()
    assert len(signals) == 1
    assert signals[0].direction is SignalDirection.SHORT


def test_gap_fade_longs_gap_down():
    snaps = {"BBB": Snapshot("BBB", daily_open=90.0, prev_daily_close=100.0)}
    broker = FakeBroker(snapshots=snaps)
    cfg = Config()
    cfg.strategies.gap_fade.enabled = True
    strat = GapFadeStrategy(broker, cfg, ["BBB"])
    signals = strat.scan()
    assert signals and signals[0].direction is SignalDirection.LONG


def test_gap_fade_ignores_small_gaps():
    snaps = {"CCC": Snapshot("CCC", daily_open=100.5, prev_daily_close=100.0)}
    broker = FakeBroker(snapshots=snaps)
    cfg = Config()
    cfg.strategies.gap_fade.enabled = True
    cfg.strategies.gap_fade.min_gap_pct = 2.0
    strat = GapFadeStrategy(broker, cfg, ["CCC"])
    assert strat.scan() == []


def test_gap_fade_disabled_returns_empty():
    broker = FakeBroker(snapshots={"X": Snapshot("X", daily_open=120.0, prev_daily_close=100.0)})
    cfg = Config()
    cfg.strategies.gap_fade.enabled = False
    strat = GapFadeStrategy(broker, cfg, ["X"])
    assert strat.scan() == []


def test_gap_fade_preferred_regimes_declared():
    from trader.strategies.base import MarketRegime

    assert MarketRegime.MEAN_REVERTING in GapFadeStrategy.preferred_regimes
