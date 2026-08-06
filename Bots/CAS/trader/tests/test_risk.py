"""Tests for risk sizing, drawdown breaker, and ATR-based stops."""

from __future__ import annotations

from trader.config import Config
from trader.risk.manager import RiskManager
from trader.risk.stops import compute_atr, get_stop_price
from trader.strategies.base import Signal, SignalDirection

from .conftest import FakeBroker, make_bars


def _signal() -> Signal:
    return Signal("AAPL", SignalDirection.LONG, 0.8, "test", "unit")


def test_position_size_respects_risk_per_trade():
    broker = FakeBroker(equity=100_000.0)
    cfg = Config()
    cfg.risk.max_risk_per_trade = 0.01  # $1,000 risk
    rm = RiskManager(broker, cfg)
    # entry 100, stop 95 → $5 risk/share → 200 shares; but 5% value cap = $5000/100 = 50.
    shares = rm.compute_position_size(_signal(), entry_price=100.0, stop_price=95.0)
    assert shares == 50  # value cap binds


def test_position_size_zero_when_no_stop_distance():
    broker = FakeBroker(equity=100_000.0)
    rm = RiskManager(broker, Config())
    assert rm.compute_position_size(_signal(), 100.0, 100.0) == 0.0


def test_drawdown_breaker_trips():
    broker = FakeBroker(equity=100_000.0)
    cfg = Config()
    cfg.risk.max_daily_drawdown = 0.05
    rm = RiskManager(broker, cfg)
    rm.record_start_of_day()
    assert rm.check_drawdown() is True
    broker._equity = 94_000.0  # 6% loss
    assert rm.check_drawdown() is False
    assert rm.is_halted is True


def test_can_open_position_max_limit():
    broker = FakeBroker(equity=100_000.0)
    cfg = Config()
    cfg.risk.max_positions = 1
    from trader.brokers.types import Position

    broker._positions = [Position("AAPL", 1, 1, 1, 1, 0, 0.0)]
    rm = RiskManager(broker, cfg)
    assert rm.can_open_position() is False


def test_compute_atr_positive():
    bars = make_bars("AAPL", [100 + i for i in range(30)])
    atr = compute_atr(bars, period=14)
    assert atr > 0


def test_compute_atr_insufficient_bars():
    bars = make_bars("AAPL", [100, 101, 102])
    assert compute_atr(bars, period=14) == 0.0


def test_get_stop_price_long_below_entry():
    bars = {"AAPL": make_bars("AAPL", [100 + i * 0.5 for i in range(40)])}
    broker = FakeBroker(bars=bars)
    cfg = Config()
    stop = get_stop_price(broker, "AAPL", entry_price=120.0, side="long", cfg=cfg)
    assert stop < 120.0


def test_get_stop_price_short_above_entry():
    bars = {"AAPL": make_bars("AAPL", [100 + i * 0.5 for i in range(40)])}
    broker = FakeBroker(bars=bars)
    stop = get_stop_price(broker, "AAPL", entry_price=120.0, side="short", cfg=Config())
    assert stop > 120.0
