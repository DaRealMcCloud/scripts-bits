"""Tests for the OrderManager execution path (bracket + fallback stop)."""

from __future__ import annotations

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
