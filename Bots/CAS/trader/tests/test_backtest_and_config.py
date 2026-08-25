"""Smoke test for the backtest engine and config loading."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from trader.backtest import (
    DEFAULT_MONTHS,
    Backtester,
    _resolve_start,
    _subtract_months,
)
from trader.config import Config, load_config

from .conftest import FakeBroker, make_bars


def test_backtest_runs_and_reports():
    # Two long uptrends so momentum has something to buy.
    bars = {
        "AAA": make_bars("AAA", [100 + i for i in range(120)]),
        "BBB": make_bars("BBB", [50 + i * 0.8 for i in range(120)]),
    }
    broker = FakeBroker(equity=100_000.0, bars=bars)
    cfg = Config()
    cfg.strategies.momentum.slow_lookback = 20
    cfg.strategies.momentum.top_n = 2
    bt = Backtester(cfg, starting_equity=100_000.0)
    result = bt.run(broker, ["AAA", "BBB"], start=datetime(2024, 1, 1))
    assert result.starting_equity == 100_000.0
    assert isinstance(result.total_return_pct, float)
    assert isinstance(result.summary(), str)
    assert result.num_trades >= 0


def test_backtest_no_data_is_flat():
    broker = FakeBroker(bars={})
    result = Backtester(Config()).run(broker, ["ZZZ"], start=datetime(2024, 1, 1))
    assert result.ending_equity == result.starting_equity


# ── CLI window selection (--months / --days) ──────────────────────────────────


def test_subtract_months_clamps_day_of_month():
    # 31 March minus one month has no 31st to land on.
    assert _subtract_months(datetime(2024, 3, 31), 1) == datetime(2024, 2, 29)
    assert _subtract_months(datetime(2023, 3, 31), 1) == datetime(2023, 2, 28)


def test_subtract_months_rolls_over_years():
    assert _subtract_months(datetime(2024, 2, 15), 3) == datetime(2023, 11, 15)
    assert _subtract_months(datetime(2024, 2, 15), 12) == datetime(2023, 2, 15)
    assert _subtract_months(datetime(2024, 2, 15), 24) == datetime(2022, 2, 15)


def test_resolve_start_prefers_months_then_days_then_default():
    now = datetime(2024, 6, 15)
    assert _resolve_start(now, months=6) == datetime(2023, 12, 15)
    assert _resolve_start(now, days=30) == datetime(2024, 5, 16)
    assert _resolve_start(now) == _subtract_months(now, DEFAULT_MONTHS)


def test_backtest_run_honours_the_requested_window():
    # 400 daily bars; a 3-month window must replay only the recent slice.
    bars = {"AAA": make_bars("AAA", [100 + i for i in range(400)])}
    broker = FakeBroker(equity=100_000.0, bars=bars)
    end = bars["AAA"][-1].timestamp
    start = _subtract_months(end, 3)

    bt = Backtester(Config(), starting_equity=100_000.0)
    result = bt.run(broker, ["AAA"], start=start, end=end)

    assert all(start.date() <= ts.date() <= end.date() for ts, _ in result.equity_curve)


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    """Isolate config tests from any real .env / env vars in the dev workspace.

    Runs in an empty temp CWD (so no local .env is auto-loaded) and clears the
    broker-related environment variables. Individual tests re-set what they need.
    """
    monkeypatch.chdir(tmp_path)
    for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "BROKER_PROVIDER",
                "IBKR_ACCOUNT_ID", "IBKR_BASE_URL"):
        monkeypatch.delenv(var, raising=False)


def test_load_config_defaults_alpaca_requires_creds(clean_env):
    with pytest.raises(ValueError):
        load_config(None)


def test_load_config_ibkr_provider_no_creds_ok(clean_env, monkeypatch):
    monkeypatch.setenv("BROKER_PROVIDER", "ibkr")
    cfg = load_config(None)
    assert cfg.broker.provider == "ibkr"


def test_load_config_unknown_provider_raises(clean_env, monkeypatch):
    monkeypatch.setenv("BROKER_PROVIDER", "etrade")
    with pytest.raises(ValueError):
        load_config(None)


def test_config_env_overrides_alpaca(clean_env, monkeypatch):
    monkeypatch.setenv("BROKER_PROVIDER", "alpaca")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    cfg = load_config(None)
    assert cfg.alpaca.api_key == "k"
    assert cfg.alpaca.secret_key == "s"
