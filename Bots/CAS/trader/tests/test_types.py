"""Tests for neutral DTO behaviour (derived properties)."""

from __future__ import annotations

from trader.brokers.types import (
    OrderResult,
    OrderStatus,
    Position,
    PositionSide,
    Quote,
    Side,
)


def test_position_side_and_abs_qty():
    long = Position("AAPL", 10, 100, 110, 1100, 100, 10.0)
    short = Position("TSLA", -5, 200, 190, -950, 50, 5.0)
    assert long.side is PositionSide.LONG
    assert long.abs_qty == 10
    assert short.side is PositionSide.SHORT
    assert short.abs_qty == 5


def test_quote_mid():
    q = Quote("AAPL", bid_price=100.0, ask_price=102.0)
    assert q.mid == 101.0
    # Falls back to whichever side is present.
    assert Quote("X", bid_price=0.0, ask_price=50.0).mid == 50.0
    assert Quote("X", bid_price=40.0, ask_price=0.0).mid == 40.0


def test_order_result_accepted():
    ok = OrderResult("1", OrderStatus.NEW, "AAPL", 10)
    rejected = OrderResult("2", OrderStatus.REJECTED, "AAPL", 10)
    unknown = OrderResult("3", OrderStatus.UNKNOWN, "AAPL", 10)
    assert ok.accepted is True
    assert rejected.accepted is False
    assert unknown.accepted is False


def test_side_enum_values():
    assert Side.BUY.value == "buy"
    assert Side.SELL.value == "sell"
