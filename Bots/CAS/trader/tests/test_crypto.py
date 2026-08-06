"""Tests for the 24/7 crypto layer: strategies, execution, stops, limits."""

from __future__ import annotations

from trader.brokers.types import (
    AssetClass,
    Instrument,
    Position,
    Quote,
)
from trader.config import Config
from trader.execution.order_manager import OrderManager
from trader.execution.state_store import StateStore
from trader.risk.manager import RiskManager
from trader.strategies.base import Signal, SignalDirection
from trader.strategies.crypto import (
    CryptoBreakoutStrategy,
    CryptoDcaStrategy,
    CryptoMeanReversionStrategy,
    CryptoMomentumStrategy,
    CryptoVolatilityStrategy,
)
from trader.strategies.quote_imbalance import QuoteImbalanceScorer

from .conftest import FakeBroker, make_bars


def _mgr(broker, tmp_path):
    cfg = Config()
    cfg.strategies.quote_imbalance.enabled = False
    risk = RiskManager(broker, cfg)
    risk.record_start_of_day()
    imb = QuoteImbalanceScorer(cfg)
    store = StateStore(db_path=tmp_path / "state.db")
    return OrderManager(broker, cfg, risk, imb, state=store), store, cfg


def _crypto_pos(symbol: str, qty: float, price: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=price,
        current_price=price,
        market_value=qty * price,
        unrealized_pl=0.0,
        unrealized_pl_pct=0.0,
        asset_class=AssetClass.CRYPTO,
    )


# ── Strategies: all long-only + asset_class=CRYPTO ────────────────────────────


def test_crypto_momentum_long_only_and_asset_class():
    up = [100 + i for i in range(120)]  # strong uptrend
    broker = FakeBroker(bars={"BTC/USD": make_bars("BTC/USD", up)})
    cfg = Config()
    cfg.strategies.crypto.momentum.enabled = True
    strat = CryptoMomentumStrategy(broker, cfg, ["BTC/USD"])
    signals = strat.scan()
    assert signals, "expected a momentum signal in a clear uptrend"
    for s in signals:
        assert s.direction == SignalDirection.LONG
        assert s.asset_class == AssetClass.CRYPTO
        assert s.strategy == "crypto_momentum"


def test_crypto_breakout_suppresses_breakdowns():
    # Downtrend ending at new lows → a short-biased strategy would fire; ours must not.
    down = [200 - i for i in range(90)]
    broker = FakeBroker(bars={"ETH/USD": make_bars("ETH/USD", down)})
    cfg = Config()
    cfg.strategies.crypto.breakout.enabled = True
    strat = CryptoBreakoutStrategy(broker, cfg, ["ETH/USD"])
    assert strat.scan() == []  # no long breakout on a breakdown


def test_crypto_breakout_fires_on_upper_break():
    prices = [100.0] * 40 + [130.0]  # flat then break above channel high
    broker = FakeBroker(bars={"ETH/USD": make_bars("ETH/USD", prices)})
    cfg = Config()
    cfg.strategies.crypto.breakout.enabled = True
    strat = CryptoBreakoutStrategy(broker, cfg, ["ETH/USD"])
    signals = strat.scan()
    assert signals
    assert all(s.direction == SignalDirection.LONG for s in signals)
    assert all(s.asset_class == AssetClass.CRYPTO for s in signals)


def test_crypto_mean_reversion_long_only():
    # Sharp drop → oversold + negative z-score → long entry.
    prices = [100.0] * 25 + [70.0, 60.0, 55.0]
    broker = FakeBroker(bars={"SOL/USD": make_bars("SOL/USD", prices)})
    cfg = Config()
    cfg.strategies.crypto.mean_reversion.enabled = True
    strat = CryptoMeanReversionStrategy(broker, cfg, ["SOL/USD"])
    signals = strat.scan()
    for s in signals:
        assert s.direction == SignalDirection.LONG
        assert s.asset_class == AssetClass.CRYPTO


def test_crypto_dca_respects_cadence():
    broker = FakeBroker()
    cfg = Config()
    cfg.strategies.crypto.dca.enabled = True
    cfg.strategies.crypto.dca.symbols = ["BTC/USD"]
    cfg.strategies.crypto.dca.cadence_hours = 24
    strat = CryptoDcaStrategy(broker, cfg, ["BTC/USD"])
    first = strat.scan()
    assert len(first) == 1
    assert first[0].direction == SignalDirection.LONG
    assert first[0].asset_class == AssetClass.CRYPTO
    # Immediately re-scanning must NOT re-buy (cadence not elapsed).
    assert strat.scan() == []


def test_crypto_volatility_long_only_asset_class():
    # Low-vol coil in a mild uptrend.
    prices = [100 + i * 0.05 for i in range(90)]
    broker = FakeBroker(bars={"BTC/USD": make_bars("BTC/USD", prices)})
    cfg = Config()
    cfg.strategies.crypto.volatility.enabled = True
    strat = CryptoVolatilityStrategy(broker, cfg, ["BTC/USD"])
    for s in strat.scan():
        assert s.direction == SignalDirection.LONG
        assert s.asset_class == AssetClass.CRYPTO


# ── Execution: GTC, no stop, fractional, shorts blocked ───────────────────────


def test_crypto_order_is_gtc_no_stop_and_fractional(tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=59_990.0, ask_price=60_010.0)},
    )
    mgr, store, _ = _mgr(broker, tmp_path)
    sig = Signal(
        "BTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_momentum",
        "unit",
        stop_hint=57_000.0,
        asset_class=AssetClass.CRYPTO,
    )
    result = mgr.execute_signal(sig)
    assert result is not None
    assert len(broker.submitted) == 1
    req = broker.submitted[0]
    from trader.brokers.types import TimeInForce

    assert req.time_in_force == TimeInForce.GTC
    assert req.attached_stop_price is None  # no broker-side stop for crypto
    assert broker.stop_orders == []  # no standalone stop either
    assert 0 < req.qty < 1  # fractional BTC, not floored to zero
    # Software stop persisted for the crypto scan loop to enforce.
    assert "BTC/USD" in store.get_crypto_stops()
    store.close()


def test_crypto_short_is_blocked(tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=59_990.0, ask_price=60_010.0)},
    )
    mgr, store, _ = _mgr(broker, tmp_path)
    sig = Signal(
        "BTC/USD",
        SignalDirection.SHORT,
        0.9,
        "crypto_momentum",
        "unit",
        stop_hint=63_000.0,
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is None
    assert broker.submitted == []
    store.close()


def test_crypto_buy_blocked_when_insufficient_cash(tmp_path):
    # Real-world case: equity is healthy (positions hold value) but free cash is
    # nearly exhausted. Spot crypto is cash-only, so the buy must be skipped
    # rather than sent to the broker where it would 403 "insufficient balance".
    broker = FakeBroker(
        equity=1000.0,
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=59_990.0, ask_price=60_010.0)},
    )
    # Equity stays $1000 (sizing basis) but only $1 cash is free to spend.
    broker.get_cash = lambda: 1.0  # type: ignore[method-assign]
    mgr, store, _ = _mgr(broker, tmp_path)
    sig = Signal(
        "BTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_momentum",
        "unit",
        stop_hint=57_000.0,
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is None
    assert broker.submitted == []  # never reached the broker
    store.close()


def test_crypto_buy_skipped_when_position_already_held(tmp_path):
    # Duplicate-entry guard: a strategy that keeps signalling the same coil must
    # not stack a second position in a symbol we already hold. Alpaca reports
    # the position in de-slashed form (LTCUSD) — the guard must still match the
    # slashed signal symbol (LTC/USD).
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"LTC/USD": Quote("LTC/USD", bid_price=44.6, ask_price=44.7)},
        positions=[_crypto_pos("LTCUSD", 100.0, 44.61)],
    )
    mgr, store, _ = _mgr(broker, tmp_path)
    sig = Signal(
        "LTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_volatility",
        "unit",
        stop_hint=42.0,
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is None
    assert broker.submitted == []  # no duplicate buy sent
    store.close()


def test_crypto_buy_skipped_when_open_order_exists(tmp_path):
    # Covers the fill-latency race: the first buy is submitted but not yet a
    # position. A second near-simultaneous scan must see the working order and
    # skip re-buying.
    from trader.brokers.types import Order, OrderStatus, OrderType, Side

    broker = FakeBroker(
        equity=100_000.0,
        quotes={"LTC/USD": Quote("LTC/USD", bid_price=44.6, ask_price=44.7)},
    )
    broker._open_orders = [
        Order(order_id="ltc-1", symbol="LTCUSD", side=Side.BUY, qty=100.0,
              order_type=OrderType.LIMIT, status=OrderStatus.NEW),
    ]
    mgr, store, _ = _mgr(broker, tmp_path)
    sig = Signal(
        "LTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_volatility",
        "unit",
        stop_hint=42.0,
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is None
    assert broker.submitted == []
    store.close()


def test_crypto_buy_allowed_when_no_existing_exposure(tmp_path):
    # Sanity: with no position and no open order, the buy still goes through.
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"LTC/USD": Quote("LTC/USD", bid_price=44.6, ask_price=44.7)},
    )
    mgr, store, _ = _mgr(broker, tmp_path)
    sig = Signal(
        "LTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_volatility",
        "unit",
        stop_hint=42.0,
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is not None
    assert len(broker.submitted) == 1
    store.close()


    # Stop is only ~0.3% from entry — below the taker-based fee floor
    # (taker 0.25% → 0.5% round-trip; × min_edge_multiple 2 → 1.0%).
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=59_990.0, ask_price=60_010.0)},
    )
    mgr, store, cfg = _mgr(broker, tmp_path)
    cfg.risk.taker_fee_pct = 0.0025
    cfg.risk.min_edge_multiple = 2.0
    sig = Signal(
        "BTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_momentum",
        "unit",
        stop_hint=59_820.0,  # ~0.32% below entry → fails the 1.0% edge floor
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is None
    assert broker.submitted == []  # blocked before reaching the broker
    store.close()


def test_fee_gate_allows_healthy_edge_trade(tmp_path):
    # Stop is ~5% from entry — comfortably above the fee floor → allowed.
    broker = FakeBroker(
        equity=100_000.0,
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=59_990.0, ask_price=60_010.0)},
    )
    mgr, store, cfg = _mgr(broker, tmp_path)
    cfg.risk.taker_fee_pct = 0.0025
    cfg.risk.min_edge_multiple = 2.0
    sig = Signal(
        "BTC/USD",
        SignalDirection.LONG,
        0.9,
        "crypto_momentum",
        "unit",
        stop_hint=57_000.0,  # ~5% edge, clears the gate
        asset_class=AssetClass.CRYPTO,
    )
    assert mgr.execute_signal(sig) is not None
    assert len(broker.submitted) == 1
    store.close()


# ── Flatten & cancel respect asset class ──────────────────────────────────────

def test_flatten_skips_crypto_by_default(tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        positions=[
            Position("AAA", 10, 100, 100, 1000, 0, 0),
            _crypto_pos("BTC/USD", 0.1, 60_000.0),
        ],
    )
    mgr, store, _ = _mgr(broker, tmp_path)
    mgr.flatten_all()  # default: crypto left open
    assert broker.closed_positions == ["AAA"]
    store.close()


def test_flatten_includes_crypto_when_configured(tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        positions=[
            Position("AAA", 10, 100, 100, 1000, 0, 0),
            _crypto_pos("BTC/USD", 0.1, 60_000.0),
        ],
    )
    mgr, store, _ = _mgr(broker, tmp_path)
    mgr.flatten_all(include_crypto=True)  # bulk close everything
    assert broker._positions == []
    store.close()


# ── Position limits counted separately ────────────────────────────────────────


def test_crypto_position_limit_separate_from_equity(tmp_path):
    # 3 crypto positions open; equity limit is high, crypto limit is 3.
    positions = [_crypto_pos(f"C{i}/USD", 1, 100) for i in range(3)]
    broker = FakeBroker(equity=100_000.0, positions=positions)
    cfg = Config()
    cfg.risk.max_crypto_positions = 3
    risk = RiskManager(broker, cfg)
    risk.record_start_of_day()
    assert risk.can_open_position("crypto") is False  # crypto cap reached
    assert risk.can_open_position("us_equity") is True  # equity unaffected


def test_crypto_position_limit_unlimited_when_zero(tmp_path):
    positions = [_crypto_pos(f"C{i}/USD", 1, 100) for i in range(20)]
    broker = FakeBroker(equity=100_000.0, positions=positions)
    cfg = Config()
    cfg.risk.max_crypto_positions = 0  # unlimited
    risk = RiskManager(broker, cfg)
    risk.record_start_of_day()
    assert risk.can_open_position("crypto") is True


# ── StateStore crypto stops CRUD ──────────────────────────────────────────────


def test_state_store_crypto_stops_crud(tmp_path):
    store = StateStore(db_path=tmp_path / "state.db")
    store.set_crypto_stop("BTC/USD", 55_000.0, 60_000.0)
    stops = store.get_crypto_stops()
    assert stops["BTC/USD"]["stop_price"] == 55_000.0
    assert stops["BTC/USD"]["entry_price"] == 60_000.0
    store.delete_crypto_stop("BTC/USD")
    assert store.get_crypto_stops() == {}
    store.close()


# ── list_tradable_crypto surfaced on FakeBroker ───────────────────────────────


def test_fake_broker_lists_crypto():
    assets = [Instrument(symbol="BTC/USD", broker_id="BTC/USD", asset_class=AssetClass.CRYPTO)]
    broker = FakeBroker(crypto_assets=assets)
    listed = broker.list_tradable_crypto()
    assert [i.symbol for i in listed] == ["BTC/USD"]


# ── Trader wiring: scheduler + software stop enforcement ──────────────────────


def _trader(broker, monkeypatch, tmp_path):
    from trader.main import Trader

    monkeypatch.chdir(tmp_path)  # keep StateStore/heartbeat inside temp
    cfg = Config()
    return Trader(cfg, broker=broker), cfg


def test_build_scheduler_adds_crypto_job_when_enabled(monkeypatch, tmp_path):
    from trader.main import build_scheduler

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    cfg.strategies.crypto.scan_interval_minutes = 15
    scheduler = build_scheduler(trader)
    funcs = {getattr(j.func, "__name__", "") for j in scheduler.get_jobs()}
    assert "crypto_scan" in funcs


def test_build_scheduler_adds_separate_stop_check_job(monkeypatch, tmp_path):
    from trader.main import build_scheduler

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    cfg.strategies.crypto.scan_interval_minutes = 15
    cfg.strategies.crypto.stop_check_interval_minutes = 5
    scheduler = build_scheduler(trader)
    funcs = {getattr(j.func, "__name__", "") for j in scheduler.get_jobs()}
    # Both the opportunity scan and the tighter stop-check are scheduled.
    assert "crypto_scan" in funcs
    assert "crypto_stop_check" in funcs


def test_crypto_stop_check_enforces_stop_without_scanning(monkeypatch, tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        positions=[_crypto_pos("BTC/USD", 0.1, 60_000.0)],
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=54_000.0, ask_price=54_010.0)},
    )
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    # Stop above current price → stop-check alone should close the position.
    trader.state.set_crypto_stop("BTC/USD", stop_price=58_000.0, entry_price=60_000.0)

    # Guard: crypto_stop_check must NOT scan for new opportunities.
    def _boom(*_a, **_k):
        raise AssertionError("crypto_stop_check must not scan strategies")

    trader._scan_and_process_crypto = _boom  # type: ignore[method-assign]

    trader.crypto_stop_check()

    assert "BTC/USD" in broker.closed_positions
    assert trader.state.get_crypto_stops() == {}
    # The software close is now recorded as a synthetic SELL so the dashboard
    # shows the closed round-trip with realized P/L.
    sells = [o for o in trader.state.all_orders() if o["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["symbol"] == "BTC/USD"
    assert sells[0]["status"] == "filled"
    assert sells[0]["qty"] == 0.1


def test_reconcile_order_statuses_updates_pending_to_filled(monkeypatch, tmp_path):
    from trader.brokers.types import OrderStatus
    from trader.execution.state_store import OrderRecord

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)

    # A persisted order still marked pending at the broker's submit time.
    trader.state.record_order(
        OrderRecord(
            order_id="ord-1",
            symbol="BTC/USD",
            side="buy",
            qty=0.1,
            limit_price=60_000.0,
            stop_price=None,
            strategy="unit",
            reason="test",
            status="pending",
        )
    )
    # Broker now reports the order as filled.
    broker.order_statuses["ord-1"] = OrderStatus.FILLED

    trader._reconcile_order_statuses()

    row = next(o for o in trader.state.all_orders() if o["order_id"] == "ord-1")
    assert row["status"] == "filled"
    # A now-terminal order is no longer picked up for re-checking.
    assert trader.state.non_terminal_orders() == []


def test_reconcile_order_statuses_leaves_unknown_untouched(monkeypatch, tmp_path):
    from trader.execution.state_store import OrderRecord

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    trader.state.record_order(
        OrderRecord(
            order_id="ord-9",
            symbol="ETH/USD",
            side="buy",
            qty=1.0,
            limit_price=3000.0,
            stop_price=None,
            strategy="unit",
            reason="test",
            status="pending",
        )
    )
    # Broker can't resolve the order → get_order_status returns None.
    trader._reconcile_order_statuses()
    row = next(o for o in trader.state.all_orders() if o["order_id"] == "ord-9")
    assert row["status"] == "pending"


def test_backfill_imports_missing_sell_with_fill_price(monkeypatch, tmp_path):
    from trader.brokers.types import Order, OrderStatus, OrderType, Side
    from trader.execution.state_store import OrderRecord

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)

    # A recorded buy at an approximate limit price.
    trader.state.record_order(
        OrderRecord(
            order_id="buy-1", symbol="ETH/USD", side="buy", qty=1.0,
            limit_price=1773.0, stop_price=None, strategy="unit",
            reason="test", status="filled",
        )
    )
    # Broker reports the buy filled at a slightly different real price, plus a
    # SELL close that was never recorded locally.
    broker.recent_orders = [
        Order(order_id="buy-1", symbol="ETH/USD", side=Side.BUY, qty=1.0,
              order_type=OrderType.LIMIT, status=OrderStatus.FILLED,
              filled_qty=1.0, filled_avg_price=1771.5),
        Order(order_id="sell-1", symbol="ETH/USD", side=Side.SELL, qty=1.0,
              order_type=OrderType.MARKET, status=OrderStatus.FILLED,
              filled_qty=1.0, filled_avg_price=1800.0),
    ]

    trader._backfill_orders_from_broker()

    orders = {o["order_id"]: o for o in trader.state.all_orders()}
    # Buy price corrected to the real fill.
    assert orders["buy-1"]["limit_price"] == 1771.5
    # Missing sell imported.
    assert "sell-1" in orders
    assert orders["sell-1"]["side"] == "sell"
    assert orders["sell-1"]["limit_price"] == 1800.0


def test_backfill_ignores_unfilled_orders(monkeypatch, tmp_path):
    from trader.brokers.types import Order, OrderStatus, OrderType, Side

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    # An open order with no fill price should NOT be imported.
    broker.recent_orders = [
        Order(order_id="open-1", symbol="POL/USD", side=Side.BUY, qty=10.0,
              order_type=OrderType.LIMIT, status=OrderStatus.NEW,
              filled_qty=0.0, filled_avg_price=None),
    ]
    trader._backfill_orders_from_broker()
    assert trader.state.get_order("open-1") is None


def test_reconcile_crypto_with_software_stop_is_not_warned(monkeypatch, tmp_path, caplog):
    import logging

    broker = FakeBroker(positions=[_crypto_pos("BTC/USD", 0.1, 60_000.0)])
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    # A registered software stop → crypto is protected; no scary WARNING.
    trader.state.set_crypto_stop("BTC/USD", stop_price=57_000.0, entry_price=60_000.0)

    with caplog.at_level(logging.INFO):
        trader._reconcile()

    text = caplog.text
    # No "NO protective stop" / "NO software stop" warning for the protected pos.
    assert "NO protective stop order at the broker" not in text
    assert "NO software stop registered" not in text
    # It IS logged, but only as an informational "protected by software stop".
    assert "protected by software stop" in text


def test_reconcile_crypto_without_software_stop_warns(monkeypatch, tmp_path, caplog):
    import logging

    broker = FakeBroker(positions=[_crypto_pos("ETH/USD", 1.0, 3_000.0)])
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    # No registered software stop → the stop-check loop can't protect it → warn.

    with caplog.at_level(logging.WARNING):
        trader._reconcile()

    assert "NO software stop registered" in caplog.text
    # The generic broker-stop warning is for equities, not this crypto case.
    assert "NO protective stop order at the broker" not in caplog.text


def test_build_scheduler_omits_crypto_job_when_disabled(monkeypatch, tmp_path):
    from trader.main import build_scheduler

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = False
    scheduler = build_scheduler(trader)
    funcs = {getattr(j.func, "__name__", "") for j in scheduler.get_jobs()}
    assert "crypto_scan" not in funcs
    assert "crypto_stop_check" not in funcs


def test_crypto_scan_enforces_software_stop(monkeypatch, tmp_path):
    broker = FakeBroker(
        equity=100_000.0,
        positions=[_crypto_pos("BTC/USD", 0.1, 60_000.0)],
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=54_000.0, ask_price=54_010.0)},
    )
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    # Store a stop above current price → should trigger a close.
    trader.state.set_crypto_stop("BTC/USD", 55_000.0, 60_000.0)
    trader.crypto_scan()
    assert "BTC/USD" in broker.closed_positions
    assert trader.state.get_crypto_stops() == {}  # stop cleared after close


def test_crypto_scan_takes_profit_at_target(monkeypatch, tmp_path):
    # Price is well above entry + take_profit_pct → position should be closed
    # to lock in the winner (the "7" in the 3-5-7 rule).
    broker = FakeBroker(
        equity=100_000.0,
        positions=[_crypto_pos("BTC/USD", 0.1, 60_000.0)],
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=66_000.0, ask_price=66_010.0)},
    )
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    cfg.risk.take_profit_pct = 0.07  # +7% target
    # Entry 60_000, stop below price so the stop does NOT fire; bid 66_000 = +10%.
    trader.state.set_crypto_stop("BTC/USD", 55_000.0, 60_000.0)
    trader.crypto_scan()
    assert "BTC/USD" in broker.closed_positions
    assert trader.state.get_crypto_stops() == {}  # stop cleared after take-profit


def test_crypto_scan_holds_below_take_profit(monkeypatch, tmp_path):
    # Price is up only ~+3%, below the +7% target and above the stop → hold.
    broker = FakeBroker(
        equity=100_000.0,
        positions=[_crypto_pos("BTC/USD", 0.1, 60_000.0)],
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=61_800.0, ask_price=61_810.0)},
    )
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    cfg.risk.take_profit_pct = 0.07
    trader.state.set_crypto_stop("BTC/USD", 55_000.0, 60_000.0)
    trader.crypto_scan()
    assert broker.closed_positions == []  # neither stop nor target hit
    assert "BTC/USD" in trader.state.get_crypto_stops()  # still open


def test_crypto_scan_noop_when_disabled(monkeypatch, tmp_path):
    broker = FakeBroker(
        positions=[_crypto_pos("BTC/USD", 0.1, 60_000.0)],
        quotes={"BTC/USD": Quote("BTC/USD", bid_price=1.0, ask_price=1.1)},
    )
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = False
    trader.state.set_crypto_stop("BTC/USD", 55_000.0, 60_000.0)
    trader.crypto_scan()
    assert broker.closed_positions == []  # disabled: no stop enforcement


# ── Stale crypto order sweep (TTL cancel) ─────────────────────────────────────


def _open_order(order_id, symbol, submitted_at):
    from trader.brokers.types import Order, OrderStatus, OrderType, Side

    return Order(
        order_id=order_id,
        symbol=symbol,
        side=Side.BUY,
        qty=1.0,
        order_type=OrderType.LIMIT,
        status=OrderStatus.NEW,
        submitted_at=submitted_at,
    )


def test_stale_crypto_order_is_cancelled(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.crypto_order_ttl_minutes = 30
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    broker._open_orders = [_open_order("stale-1", "LTC/USD", old)]

    cancelled = trader._cancel_stale_crypto_orders()

    assert cancelled == 1
    assert "stale-1" in broker.cancelled_orders


def test_fresh_crypto_order_is_kept(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.crypto_order_ttl_minutes = 30
    recent = datetime.now(timezone.utc) - timedelta(minutes=5)
    broker._open_orders = [_open_order("fresh-1", "LTC/USD", recent)]

    cancelled = trader._cancel_stale_crypto_orders()

    assert cancelled == 0
    assert broker.cancelled_orders == []


def test_stale_sweep_ignores_equity_orders(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.crypto_order_ttl_minutes = 30
    old = datetime.now(timezone.utc) - timedelta(minutes=90)
    # Equity symbol (no slash) must never be touched by the crypto sweep.
    broker._open_orders = [_open_order("aapl-1", "AAPL", old)]

    cancelled = trader._cancel_stale_crypto_orders()

    assert cancelled == 0
    assert broker.cancelled_orders == []


def test_stale_sweep_disabled_when_ttl_zero(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.crypto_order_ttl_minutes = 0  # disabled
    ancient = datetime.now(timezone.utc) - timedelta(days=3)
    broker._open_orders = [_open_order("old-1", "LTC/USD", ancient)]

    cancelled = trader._cancel_stale_crypto_orders()

    assert cancelled == 0
    assert broker.cancelled_orders == []


def test_stale_sweep_handles_naive_submitted_at(monkeypatch, tmp_path):
    from datetime import datetime, timedelta

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.crypto_order_ttl_minutes = 30
    # A tz-naive timestamp must be treated as UTC, not blow up the comparison.
    naive_old = datetime.utcnow() - timedelta(minutes=60)
    broker._open_orders = [_open_order("naive-1", "LTC/USD", naive_old)]

    cancelled = trader._cancel_stale_crypto_orders()

    assert cancelled == 1
    assert "naive-1" in broker.cancelled_orders


def test_crypto_stop_check_runs_stale_sweep(monkeypatch, tmp_path):
    from datetime import datetime, timedelta, timezone

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    cfg.strategies.crypto.crypto_order_ttl_minutes = 30
    old = datetime.now(timezone.utc) - timedelta(minutes=45)
    broker._open_orders = [_open_order("stale-2", "LTC/USD", old)]

    trader.crypto_stop_check()

    assert "stale-2" in broker.cancelled_orders


def test_describe_next_runs_says_trading_when_open(monkeypatch, tmp_path):
    import trader.main as main_mod
    from trader.main import _describe_next_runs, build_scheduler

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    scheduler = build_scheduler(trader)
    # Force "market open" so the message is deterministic regardless of wall clock.
    monkeypatch.setattr(main_mod, "_equity_session_phase", lambda *_a, **_k: "first_half")

    msg = _describe_next_runs(scheduler)

    # When open, it must say we're TRADING — never "waiting".
    assert "Market OPEN" in msg
    assert "trading" in msg.lower()
    assert "waiting" not in msg.lower()
    assert "closes at" in msg


def test_describe_next_runs_reports_next_open_when_closed(monkeypatch, tmp_path):
    import trader.main as main_mod
    from trader.main import _describe_next_runs, build_scheduler

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    scheduler = build_scheduler(trader)
    monkeypatch.setattr(main_mod, "_equity_session_phase", lambda *_a, **_k: "closed")

    msg = _describe_next_runs(scheduler)

    # When closed, it reports the next open and notes crypto keeps trading.
    assert "Next equity open at" in msg
    assert "crypto still trading" in msg.lower()
    assert "(in " in msg  # "(in Xh Ym)"
    assert "heartbeat" not in msg
    assert "crypto_scan" not in msg


def test_describe_next_runs_without_market_open_job():
    from apscheduler.schedulers.blocking import BlockingScheduler

    from trader.main import _describe_next_runs

    # No market_open job (and market treated as closed) → generic closed message.
    empty = BlockingScheduler(timezone="US/Eastern")
    msg = _describe_next_runs(empty)
    # Either the open message (if run during US hours) or the closed fallback —
    # in both cases it must not claim to be "waiting".
    assert "waiting" not in msg.lower()



def test_equity_jobs_use_us_eastern_timezone(monkeypatch, tmp_path):
    # Regression: CronTriggers built without an explicit timezone default to the
    # machine's LOCAL zone, so equity jobs fired at e.g. 09:30 Berlin instead of
    # 09:30 ET. Every job must carry US/Eastern.
    from trader.main import build_scheduler

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    cfg.strategies.crypto.enabled = True
    scheduler = build_scheduler(trader)

    for job in scheduler.get_jobs():
        tz_name = str(getattr(job.trigger.timezone, "key", job.trigger.timezone))
        assert tz_name == "US/Eastern", f"{job.func.__name__} uses {tz_name}, not US/Eastern"


def test_equity_session_phase_boundaries():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from trader.main import _equity_session_phase

    et = ZoneInfo("US/Eastern")

    # A weekday (2026-07-10 is a Friday).
    assert _equity_session_phase(datetime(2026, 7, 10, 8, 0, tzinfo=et)) == "closed"
    assert _equity_session_phase(datetime(2026, 7, 10, 9, 30, tzinfo=et)) == "first_half"
    assert _equity_session_phase(datetime(2026, 7, 10, 11, 0, tzinfo=et)) == "first_half"
    assert _equity_session_phase(datetime(2026, 7, 10, 12, 44, tzinfo=et)) == "first_half"
    assert _equity_session_phase(datetime(2026, 7, 10, 12, 45, tzinfo=et)) == "second_half"
    assert _equity_session_phase(datetime(2026, 7, 10, 15, 59, tzinfo=et)) == "second_half"
    assert _equity_session_phase(datetime(2026, 7, 10, 16, 0, tzinfo=et)) == "closed"
    # Weekend is always closed even during session hours.
    assert _equity_session_phase(datetime(2026, 7, 11, 11, 0, tzinfo=et)) == "closed"  # Sat


def test_equity_session_phase_converts_other_timezones():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from trader.main import _equity_session_phase

    # 17:00 CEST on Fri = 11:00 EDT → first half.
    berlin = datetime(2026, 7, 10, 17, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert _equity_session_phase(berlin) == "first_half"


def test_startup_catchup_runs_scan_in_first_half(monkeypatch, tmp_path):
    import trader.main as main_mod
    from trader.strategies.base import Strategy

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    # Pretend we're in the first half of the session.
    monkeypatch.setattr(main_mod, "_equity_session_phase", lambda *_a, **_k: "first_half")
    # Give it at least one (dummy) equity strategy so the catch-up isn't skipped.
    trader.strategies = [object()]  # type: ignore[list-item]

    calls = {"scan": 0, "stream": 0}
    trader._scan_and_process = lambda *_a, **_k: calls.__setitem__("scan", calls["scan"] + 1)
    trader._start_quote_stream = lambda *_a, **_k: calls.__setitem__("stream", calls["stream"] + 1)
    trader._detect_regime = lambda *_a, **_k: None

    trader._startup_equity_catchup()

    assert calls["scan"] == 1
    assert calls["stream"] == 1


def test_startup_catchup_skipped_in_second_half(monkeypatch, tmp_path):
    import trader.main as main_mod

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "_equity_session_phase", lambda *_a, **_k: "second_half")
    trader.strategies = [object()]  # type: ignore[list-item]

    calls = {"scan": 0}
    trader._scan_and_process = lambda *_a, **_k: calls.__setitem__("scan", calls["scan"] + 1)

    trader._startup_equity_catchup()

    assert calls["scan"] == 0  # too late in the day → no fresh intraday entries


def test_startup_catchup_skipped_when_market_closed(monkeypatch, tmp_path):
    import trader.main as main_mod

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "_equity_session_phase", lambda *_a, **_k: "closed")
    trader.strategies = [object()]  # type: ignore[list-item]

    calls = {"scan": 0}
    trader._scan_and_process = lambda *_a, **_k: calls.__setitem__("scan", calls["scan"] + 1)

    trader._startup_equity_catchup()

    assert calls["scan"] == 0


def test_startup_catchup_skipped_without_equity_strategies(monkeypatch, tmp_path):
    import trader.main as main_mod

    broker = FakeBroker()
    trader, cfg = _trader(broker, monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "_equity_session_phase", lambda *_a, **_k: "first_half")
    trader.strategies = []  # no equity strategies enabled

    calls = {"scan": 0}
    trader._scan_and_process = lambda *_a, **_k: calls.__setitem__("scan", calls["scan"] + 1)

    trader._startup_equity_catchup()

    assert calls["scan"] == 0

