"""Tests for the OrderManager execution path (bracket + fallback stop)."""

from __future__ import annotations

import pytest

from trader.brokers.base import BrokerCapabilities, InstrumentIdKind
from trader.brokers.types import Quote
from trader.config import Config
from trader.execution.order_manager import OrderManager
from trader.execution.state_store import StateStore
from trader.risk.manager import RiskManager
from trader.strategies.base import Signal, SignalDirection
from trader.strategies.quote_imbalance import QuoteImbalanceScorer

from .conftest import FakeBroker, make_bars


def _mgr(broker, tmp_path):
    cfg = Config()
    cfg.strategies.quote_imbalance.enabled = False  # pass-through timing
    risk = RiskManager(broker, cfg)
    risk.record_start_of_day()
    imb = QuoteImbalanceScorer(cfg)
    store = StateStore(db_path=tmp_path / "state.db")
    return OrderManager(broker, cfg, risk, imb, state=store), store


def test_bracket_order_attaches_stop(tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        bars={"AAA": make_bars("AAA", [100 + i * 0.5 for i in range(40)])},
        quotes={"AAA": Quote("AAA", bid_price=99.9, ask_price=100.1)},
    )
    mgr, store = _mgr(broker, tmp_path)
    sig = Signal("AAA", SignalDirection.LONG, 0.9, "test", "unit", stop_hint=95.0)
    result = mgr.execute_signal(sig)

    assert result is not None
    assert len(broker.submitted) == 1
    req = broker.submitted[0]
    assert req.attached_stop_price == 95.0  # bracket path used
    assert broker.stop_orders == []  # no standalone stop needed
    # Persisted.
    assert len(store.all_orders()) == 1
    store.close()


def test_fallback_standalone_stop_when_no_brackets(tmp_path):
    caps = BrokerCapabilities(
        name="fake-nobracket",
        id_kind=InstrumentIdKind.SYMBOL,
        supports_bracket_orders=False,
        supports_native_stops=True,
    )
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"BBB": Quote("BBB", bid_price=49.9, ask_price=50.1)},
        caps=caps,
    )
    mgr, store = _mgr(broker, tmp_path)
    sig = Signal("BBB", SignalDirection.LONG, 0.8, "test", "unit", stop_hint=47.0)
    mgr.execute_signal(sig)

    assert len(broker.submitted) == 1
    assert broker.submitted[0].attached_stop_price is None
    assert len(broker.stop_orders) == 1  # follow-up standalone stop placed
    store.close()


def test_latest_equity_returns_most_recent_snapshot(tmp_path):
    store = StateStore(db_path=tmp_path / "state.db")
    assert store.latest_equity() is None  # empty DB
    store.record_equity(1000.0, cash=800.0, unrealized_pl=0.0)
    store.record_equity(1050.0, cash=810.0, unrealized_pl=40.0)
    latest = store.latest_equity()
    assert latest is not None
    assert latest["equity"] == 1050.0
    assert latest["cash"] == 810.0
    assert latest["unrealized_pl"] == 40.0
    store.close()


def test_reset_dashboard_data_clears_graph_orders_positions(tmp_path):
    from trader.execution.state_store import OrderRecord

    store = StateStore(db_path=tmp_path / "state.db")
    # Populate all three dashboard-backing tables + a live crypto stop.
    store.record_equity(1000.0, cash=800.0, unrealized_pl=0.0)
    store.record_equity(1050.0, cash=810.0, unrealized_pl=40.0)
    store.record_order(
        OrderRecord(
            order_id="o1", symbol="BTC/USD", side="buy", qty=0.1,
            limit_price=60_000.0, stop_price=57_000.0, strategy="unit",
            reason="test", status="filled",
        )
    )
    store.replace_positions([
        {
            "symbol": "BTC/USD", "qty": 0.1, "avg_entry_price": 60_000.0,
            "current_price": 61_000.0, "market_value": 6_100.0,
            "unrealized_pl": 100.0, "unrealized_pl_pct": 1.67,
        }
    ])
    store.set_crypto_stop("BTC/USD", stop_price=57_000.0, entry_price=60_000.0)

    removed = store.reset_dashboard_data()

    # Reported counts match what was there.
    assert removed == {"orders": 1, "equity_snapshots": 2, "positions": 1}
    # Graph, numbers and transactions are gone.
    assert store.latest_equity() is None
    assert store.equity_series() == []
    assert store.all_orders() == []
    assert store.latest_positions() == {}
    # Live crypto stops are intentionally preserved.
    assert "BTC/USD" in store.get_crypto_stops()
    store.close()


def test_metrics_payload_includes_current_equity(tmp_path):
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    store.record_equity(1000.0, cash=800.0, unrealized_pl=0.0)
    store.record_equity(1050.0, cash=810.0, unrealized_pl=40.0)
    m = _compute_metrics(store)
    assert m["current_equity"] == 1050.0
    assert m["current_cash"] == 810.0
    assert m["current_unrealized_pl"] == 40.0
    assert m["equity_as_of"] is not None
    # The chart series carries both equity and cash for each point.
    assert m["equity_series"][-1]["v"] == 1050.0
    assert m["equity_series"][-1]["cash"] == 810.0
    store.close()


def test_metrics_aggregate_win_loss(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")

    def _order(oid, symbol, side, qty, price, ts):
        store.record_order(
            OrderRecord(
                order_id=oid,
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=price,
                stop_price=None,
                strategy="unit",
                reason="test",
                status="filled",
                created_at=ts,
            )
        )

    # AAA: buy 10 @ 100, sell 10 @ 110  → +100 (+10%), a WIN.
    _order("1", "AAA", "buy", 10, 100.0, "2026-01-01T00:00:00+00:00")
    _order("2", "AAA", "sell", 10, 110.0, "2026-01-02T00:00:00+00:00")
    # BBB: buy 10 @ 100, sell 10 @ 90   → -100 (-10%), a LOSS.
    _order("3", "BBB", "buy", 10, 100.0, "2026-01-03T00:00:00+00:00")
    _order("4", "BBB", "sell", 10, 90.0, "2026-01-04T00:00:00+00:00")

    m = _compute_metrics(store)
    # Net realized: +100 - 100 = 0 on 2000 total cost basis → 0%.
    assert m["realized_pl_total"] == 0.0
    assert m["realized_pl_pct"] == 0.0
    assert m["wins"] == 1
    assert m["losses"] == 1
    assert m["win_rate_pct"] == 50.0
    store.close()


def test_metrics_win_loss_none_without_closed_trades(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    store.record_order(
        OrderRecord(
            order_id="1",
            symbol="AAA",
            side="buy",
            qty=10,
            limit_price=100.0,
            stop_price=None,
            strategy="unit",
            reason="test",
            status="filled",
        )
    )
    m = _compute_metrics(store)
    # Only an open buy — no closed trades, so aggregates are None/zero.
    assert m["realized_pl_total"] is None
    assert m["win_rate_pct"] is None
    assert m["wins"] == 0
    assert m["losses"] == 0
    store.close()


def test_metrics_open_buy_shows_unrealized_pl(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")

    def _order(oid, symbol, side, qty, price, ts):
        store.record_order(
            OrderRecord(
                order_id=oid,
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=price,
                stop_price=None,
                strategy="unit",
                reason="test",
                status="filled",
                created_at=ts,
            )
        )

    # Open buy of 10 @ 100, no sell → still held.
    _order("1", "AAA", "buy", 10, 100.0, "2026-01-01T00:00:00+00:00")
    # Latest position snapshot marks it up to 120.
    store.replace_positions(
        [
            {
                "symbol": "AAA",
                "qty": 10,
                "avg_entry_price": 100.0,
                "current_price": 120.0,
                "market_value": 1200.0,
                "unrealized_pl": 200.0,
                "unrealized_pl_pct": 20.0,
            }
        ]
    )

    m = _compute_metrics(store)
    buy_row = next(t for t in m["recent_transactions"] if t["order_id"] == "1")
    # (120 - 100) * 10 = 200, +20%.
    assert buy_row["unrealized_pl"] == 200.0
    assert round(buy_row["unrealized_pl_pct"], 2) == 20.0
    store.close()


def test_metrics_closed_buy_has_no_unrealized_pl(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")

    def _order(oid, symbol, side, qty, price, ts):
        store.record_order(
            OrderRecord(
                order_id=oid,
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=price,
                stop_price=None,
                strategy="unit",
                reason="test",
                status="filled",
                created_at=ts,
            )
        )

    # Buy then fully sell → the buy lot is closed, no unrealized figure.
    _order("1", "AAA", "buy", 10, 100.0, "2026-01-01T00:00:00+00:00")
    _order("2", "AAA", "sell", 10, 110.0, "2026-01-02T00:00:00+00:00")
    m = _compute_metrics(store)
    buy_row = next(t for t in m["recent_transactions"] if t["order_id"] == "1")
    assert buy_row.get("unrealized_pl") is None
    store.close()


def test_annualised_return_withheld_for_short_track_record():
    """A few hours/days of history must NOT be annualised into a huge figure."""
    from trader.web.app import _MIN_ANNUALISE_DAYS, _annualised_return

    # A +0.5% gain over 1 day would naively annualise to thousands of percent.
    assert _annualised_return(1000.0, 1005.0, days=1.0) is None
    # Just under the threshold → still withheld.
    assert _annualised_return(1000.0, 1100.0, days=_MIN_ANNUALISE_DAYS - 0.1) is None
    # Zero / negative window → withheld.
    assert _annualised_return(1000.0, 1005.0, days=0.0) is None


def test_annualised_return_computed_for_long_enough_window():
    from trader.web.app import _annualised_return

    # +10% over exactly one year → ~10% p.a.
    pa = _annualised_return(1000.0, 1100.0, days=365.25)
    assert pa is not None
    assert round(pa, 2) == 10.0

    # +10% over half a year annualises to ~21% (compounding), and is allowed
    # because the window exceeds the minimum sample.
    pa_half = _annualised_return(1000.0, 1100.0, days=182.625)
    assert pa_half is not None
    assert 20.0 < pa_half < 22.0


def test_metrics_return_pa_none_for_fresh_bot(tmp_path):
    """A freshly-started bot (all snapshots seconds apart) shows no p.a. figure."""
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    store.record_equity(1000.0, cash=1000.0, unrealized_pl=0.0)
    store.record_equity(1005.0, cash=1000.0, unrealized_pl=5.0)
    m = _compute_metrics(store)
    # Total return is still shown; the annualised figure is withheld.
    assert m["total_return_pct"] is not None
    assert m["return_pa_pct"] is None
    store.close()


def test_drawdown_halt_blocks_new_orders(tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"CCC": Quote("CCC", bid_price=10, ask_price=10.1)},
    )
    mgr, store = _mgr(broker, tmp_path)
    broker._equity = 90_000.0  # 10% drawdown
    sig = Signal("CCC", SignalDirection.LONG, 0.9, "test", "unit", stop_hint=9.0)
    assert mgr.execute_signal(sig) is None
    assert broker.submitted == []
    store.close()


# ── Equity history: full-span curve + all-time baseline ───────────────────────
# The dashboard used to derive Total Return from the newest N snapshots, so a
# bot snapshotting every heartbeat showed the return of the last few days only.


def _seed_equity(store, rows):
    """Insert (ts, equity, cash) snapshots directly, bypassing 'now' stamping."""
    store._conn.executemany(
        "INSERT OR REPLACE INTO equity_snapshots (ts, equity, cash, unrealized_pl) "
        "VALUES (?,?,?,?)",
        [(ts, eq, cash, 0.0) for ts, eq, cash in rows],
    )
    store._conn.commit()


def _linear_history(n, start_equity, end_equity, cash=0.0):
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    step = (end_equity - start_equity) / (n - 1)
    return [
        ((t0 + timedelta(minutes=i)).isoformat(), start_equity + step * i, cash)
        for i in range(n)
    ]


def test_first_equity_returns_oldest_snapshot(tmp_path):
    store = StateStore(db_path=tmp_path / "state.db")
    _seed_equity(store, _linear_history(50, 5000.0, 5873.0))
    first = store.first_equity()
    assert first is not None
    assert first["equity"] == 5000.0
    assert first["ts"] < store.latest_equity()["ts"]
    store.close()


def test_equity_curve_spans_full_history_and_downsamples(tmp_path):
    store = StateStore(db_path=tmp_path / "state.db")
    _seed_equity(store, _linear_history(500, 5000.0, 5873.0))

    curve = store.equity_curve(max_points=50)
    assert 2 <= len(curve) <= 51
    # Both ends of the history survive the thinning.
    assert curve[0]["equity"] == 5000.0
    assert curve[-1]["equity"] == 5873.0
    store.close()


def test_metrics_total_return_uses_first_ever_snapshot(tmp_path):
    """5000 → 5873 is +17.5%, no matter how many snapshots sit in between."""
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    _seed_equity(store, _linear_history(6000, 5000.0, 5873.0))

    m = _compute_metrics(store)
    assert m["start_equity"] == 5000.0
    assert m["current_equity"] == 5873.0
    assert m["total_return_pct"] == pytest.approx(17.46, abs=0.01)
    store.close()


def _recent_history(days, per_day=4, start_equity=1000.0, step=1.0):
    """Snapshots spread over the last ``days`` days, ending now."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = []
    total = days * per_day
    for i in range(total):
        ts = now - timedelta(days=days) + timedelta(hours=i * 24 / per_day)
        rows.append((ts.isoformat(), start_equity + step * i, 0.0))
    return rows


def test_equity_curve_since_limits_the_window(tmp_path):
    from datetime import datetime, timedelta, timezone

    store = StateStore(db_path=tmp_path / "state.db")
    _seed_equity(store, _recent_history(days=30))

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    curve = store.equity_curve(since=cutoff.isoformat())

    assert curve, "expected snapshots inside the 7-day window"
    assert all(row["ts"] >= cutoff.isoformat() for row in curve)
    assert len(curve) < len(store.equity_curve())
    store.close()


def test_metrics_chart_days_narrows_curve_but_not_returns(tmp_path):
    from datetime import datetime, timedelta, timezone

    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    _seed_equity(store, _recent_history(days=30))

    all_time = _compute_metrics(store)
    windowed = _compute_metrics(store, chart_days=7)

    assert windowed["chart_days"] == 7
    assert len(windowed["equity_series"]) < len(all_time["equity_series"])
    # Headline figures stay all-time regardless of the chart window.
    assert windowed["total_return_pct"] == all_time["total_return_pct"]
    assert windowed["start_equity"] == all_time["start_equity"]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    assert windowed["equity_series"][0]["t"] >= cutoff
    store.close()


def test_metrics_positions_value_is_equity_minus_cash(tmp_path):
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    store.record_equity(5873.0, cash=1000.0, unrealized_pl=73.0)
    store.replace_positions(
        [
            {
                "symbol": "BTC/USD",
                "qty": 1.0,
                "avg_entry_price": 4000.0,
                "current_price": 4873.0,
                "market_value": 4873.0,
                "unrealized_pl": 873.0,
                "unrealized_pl_pct": 21.8,
            }
        ]
    )

    m = _compute_metrics(store)
    assert m["positions_value"] == 4873.0
    # Equity is cash + holdings, so the two cards no longer show the same number.
    assert m["current_equity"] == m["current_cash"] + m["positions_value"]
    store.close()


def test_metrics_positions_value_without_snapshot_falls_back(tmp_path):
    """With no cash recorded, the per-symbol snapshot supplies the holdings."""
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    store._conn.execute(
        "INSERT INTO equity_snapshots (ts, equity, cash, unrealized_pl) "
        "VALUES ('2026-01-01T00:00:00+00:00', 5873.0, NULL, 73.0)"
    )
    store._conn.commit()
    store.replace_positions(
        [
            {
                "symbol": "BTC/USD",
                "qty": 1.0,
                "avg_entry_price": 4000.0,
                "current_price": 4873.0,
                "market_value": 4873.0,
                "unrealized_pl": 873.0,
                "unrealized_pl_pct": 21.8,
            }
        ]
    )

    m = _compute_metrics(store)
    assert m["current_cash"] is None
    assert m["positions_value"] == pytest.approx(4873.0)
    store.close()


def test_metrics_top_winners_and_losers(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")

    def _order(oid, symbol, side, qty, price, ts):
        store.record_order(
            OrderRecord(
                order_id=oid,
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=price,
                stop_price=None,
                strategy="unit",
                reason="test",
                status="filled",
                created_at=ts,
            )
        )

    # 12 round-trips: 6 winners of +100..+600, 6 losers of -100..-600.
    day = 1
    for i in range(1, 7):
        _order(f"w{i}b", f"W{i}", "buy", 10, 100.0, f"2026-01-{day:02d}T00:00:00+00:00")
        _order(f"w{i}s", f"W{i}", "sell", 10, 100.0 + i * 10, f"2026-01-{day + 1:02d}T00:00:00+00:00")
        _order(f"l{i}b", f"L{i}", "buy", 10, 100.0, f"2026-02-{day:02d}T00:00:00+00:00")
        _order(f"l{i}s", f"L{i}", "sell", 10, 100.0 - i * 10, f"2026-02-{day + 1:02d}T00:00:00+00:00")
        day += 2

    m = _compute_metrics(store)
    winners, losers = m["top_winners"], m["top_losers"]

    assert [t["symbol"] for t in winners] == ["W6", "W5", "W4", "W3", "W2", "W1"]
    assert [t["symbol"] for t in losers] == ["L6", "L5", "L4", "L3", "L2", "L1"]
    assert winners[0]["realized_pl"] == 600.0
    assert losers[0]["realized_pl"] == -600.0
    # Only genuinely profitable / losing trades appear in each list.
    assert all(t["realized_pl"] > 0 for t in winners)
    assert all(t["realized_pl"] < 0 for t in losers)
    store.close()


def test_metrics_top_trades_capped_at_ten(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    for i in range(1, 15):
        for side, price, ts in (
            ("buy", 100.0, f"2026-03-{i:02d}T00:00:00+00:00"),
            ("sell", 100.0 + i, f"2026-03-{i:02d}T12:00:00+00:00"),
        ):
            store.record_order(
                OrderRecord(
                    order_id=f"{side}-{i}",
                    symbol=f"S{i}",
                    side=side,
                    qty=1,
                    limit_price=price,
                    stop_price=None,
                    strategy="unit",
                    reason="test",
                    status="filled",
                    created_at=ts,
                )
            )

    m = _compute_metrics(store)
    assert len(m["top_winners"]) == 10
    assert m["top_losers"] == []
    store.close()


# ── Per-strategy performance table ────────────────────────────────────────────


def _strategy_store(tmp_path):
    """Two strategies trading the same-ish setup: one profitable, one not."""
    from trader.execution.state_store import OrderRecord

    store = StateStore(db_path=tmp_path / "state.db")

    def _order(oid, symbol, side, qty, price, strategy, ts):
        store.record_order(
            OrderRecord(
                order_id=oid,
                symbol=symbol,
                side=side,
                qty=qty,
                limit_price=price,
                stop_price=None,
                strategy=strategy,
                reason="test",
                status="filled",
                created_at=ts,
            )
        )

    # momentum: buy 10 @100 → sold by a stop at 120 → +200 for MOMENTUM.
    _order("1", "AAA", "buy", 10, 100.0, "momentum", "2026-01-01T00:00:00+00:00")
    _order("2", "AAA", "sell", 10, 120.0, "crypto_stop", "2026-01-02T00:00:00+00:00")
    # crypto_momentum: buy 10 @100 → closed at 90 → -100.
    _order("3", "BBB", "buy", 10, 100.0, "crypto_momentum", "2026-01-03T00:00:00+00:00")
    _order("4", "BBB", "sell", 10, 90.0, "crypto_take_profit", "2026-01-04T00:00:00+00:00")
    return store


def test_strategy_performance_credits_the_opening_strategy(tmp_path):
    """The exit tag (crypto_stop) must not be credited with the P/L."""
    from trader.web.app import _compute_metrics

    store = _strategy_store(tmp_path)
    rows = {r["name"]: r for r in _compute_metrics(store)["strategies"]}

    assert rows["momentum"]["realized_pl"] == 200.0
    assert rows["momentum"]["wins"] == 1
    assert rows["momentum"]["closed_trades"] == 1
    assert rows["momentum"]["realized_pl_pct"] == pytest.approx(20.0)
    assert rows["crypto_momentum"]["realized_pl"] == -100.0
    assert rows["crypto_momentum"]["losses"] == 1
    # The exit tags carry no P/L of their own.
    assert "crypto_stop" not in rows
    assert "crypto_take_profit" not in rows
    store.close()


def test_strategy_performance_lists_configured_strategies_with_status(tmp_path):
    from trader.config import Config
    from trader.web.app import _compute_metrics

    cfg = Config()
    cfg.strategies.momentum.enabled = True
    cfg.strategies.breakout.enabled = False
    cfg.strategies.crypto.enabled = True
    cfg.strategies.crypto.momentum.enabled = True
    cfg.strategies.crypto.volatility.enabled = False

    store = _strategy_store(tmp_path)
    rows = {r["name"]: r for r in _compute_metrics(store, cfg=cfg)["strategies"]}

    assert rows["momentum"]["enabled"] is True
    assert rows["momentum"]["kind"] == "equity"
    assert rows["crypto_momentum"]["enabled"] is True
    assert rows["crypto_momentum"]["kind"] == "crypto"
    assert rows["crypto_volatility"]["enabled"] is False
    # Enabled but never traded still shows up, with zeros.
    assert rows["breakout"]["enabled"] is False
    assert rows["breakout"]["orders"] == 0
    assert rows["breakout"]["realized_pl"] == 0.0
    store.close()


def test_strategy_performance_crypto_off_disables_children(tmp_path):
    from trader.config import Config
    from trader.web.app import _compute_metrics

    cfg = Config()
    cfg.strategies.crypto.enabled = False
    cfg.strategies.crypto.momentum.enabled = True

    store = _strategy_store(tmp_path)
    rows = {r["name"]: r for r in _compute_metrics(store, cfg=cfg)["strategies"]}

    # The child switch is on but the crypto loop is off → not active.
    assert rows["crypto_momentum"]["enabled"] is False
    store.close()


def test_strategy_performance_tracks_open_exposure(tmp_path):
    from trader.execution.state_store import OrderRecord
    from trader.web.app import _compute_metrics

    store = StateStore(db_path=tmp_path / "state.db")
    store.record_order(
        OrderRecord(
            order_id="open-1",
            symbol="BTC/USD",
            side="buy",
            qty=1.0,
            limit_price=4000.0,
            stop_price=None,
            strategy="crypto_volatility",
            reason="test",
            status="filled",
            created_at="2026-01-05T00:00:00+00:00",
        )
    )
    store.replace_positions(
        [
            {
                "symbol": "BTC/USD",
                "qty": 1.0,
                "avg_entry_price": 4000.0,
                "current_price": 4500.0,
                "market_value": 4500.0,
                "unrealized_pl": 500.0,
                "unrealized_pl_pct": 12.5,
            }
        ]
    )

    rows = {r["name"]: r for r in _compute_metrics(store)["strategies"]}
    assert rows["crypto_volatility"]["open_positions"] == 1
    assert rows["crypto_volatility"]["unrealized_pl"] == pytest.approx(500.0)
    assert rows["crypto_volatility"]["closed_trades"] == 0
    assert rows["crypto_volatility"]["last_trade_at"] == "2026-01-05T00:00:00+00:00"
    store.close()
