"""Tests for pure-Python technical indicators and regime classification."""

from __future__ import annotations

from trader.analysis.indicators import (
    adx,
    bollinger,
    donchian,
    ema,
    realized_vol,
    rsi,
    sma,
    zscore,
)
from trader.analysis.regime import RegimeClassifier
from trader.strategies.base import MarketRegime

from .conftest import make_bars


def test_sma_and_ema():
    bars = make_bars("X", [10] * 25)
    assert sma(bars, 20) == 10
    assert ema(bars, 20) == 10
    assert sma(make_bars("X", [1, 2, 3]), 20) is None


def test_rsi_all_gains_is_100():
    bars = make_bars("X", [100 + i for i in range(20)])
    assert rsi(bars, 14) == 100.0


def test_rsi_all_losses_is_low():
    bars = make_bars("X", [100 - i for i in range(20)])
    assert rsi(bars, 14) == 0.0 or rsi(bars, 14) < 5


def test_bollinger_bands_ordered():
    bars = make_bars("X", [100 + (i % 5) for i in range(30)])
    b = bollinger(bars, 20)
    assert b["lower"] <= b["middle"] <= b["upper"]


def test_donchian_channel():
    bars = make_bars("X", [100 + i for i in range(25)])
    d = donchian(bars, 20)
    assert d["upper"] > d["lower"]


def test_zscore_flat_series_zero():
    bars = make_bars("X", [50] * 25)
    assert zscore(bars, 20) == 0.0


def test_realized_vol_positive_for_noisy_series():
    bars = make_bars("X", [100 + (5 if i % 2 else -5) for i in range(30)])
    v = realized_vol(bars, 20)
    assert v is not None and v > 0


def test_adx_trending_series_high():
    bars = make_bars("X", [100 + i * 2 for i in range(40)])
    val = adx(bars, 14)
    assert val is not None and val > 25


def test_regime_classifier_trending():
    bars = make_bars("SPY", [100 + i * 1.5 for i in range(120)])
    regime = RegimeClassifier().classify(bars)
    assert regime in (MarketRegime.TRENDING, MarketRegime.HIGH_VOL)


def test_regime_classifier_unknown_when_empty():
    assert RegimeClassifier().classify([]) is MarketRegime.UNKNOWN
