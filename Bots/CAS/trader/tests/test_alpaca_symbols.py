"""Tests for Alpaca-specific symbol normalization.

Alpaca's positions/orders endpoints return crypto symbols WITHOUT a slash
(``BTCUSD``), while the rest of the app uses the canonical ``BTC/USD`` form.
``_normalize_crypto_symbol`` reconciles the two so crypto positions are not
misclassified as equities (which previously left them un-flattened and unstopped).
"""

from __future__ import annotations

from trader.brokers.alpaca import _normalize_crypto_symbol

def test_deslashed_crypto_gets_slash_reinserted():
    assert _normalize_crypto_symbol("BTCUSD") == "BTC/USD"
    assert _normalize_crypto_symbol("UNIUSD") == "UNI/USD"
    assert _normalize_crypto_symbol("USDTUSD") == "USDT/USD"


def test_already_slashed_is_unchanged():
    assert _normalize_crypto_symbol("BTC/USD") == "BTC/USD"
    assert _normalize_crypto_symbol("ETH/USDT") == "ETH/USDT"


def test_equities_are_unchanged():
    # No crypto quote suffix → treated as equity, returned as-is.
    assert _normalize_crypto_symbol("AAPL") == "AAPL"
    assert _normalize_crypto_symbol("MSFT") == "MSFT"


def test_non_usd_quote_suffixes():
    assert _normalize_crypto_symbol("ETHBTC") == "ETH/BTC"
    assert _normalize_crypto_symbol("SOLUSDT") == "SOL/USDT"


# ── Order status mapping ──────────────────────────────────────────────────────
# Alpaca reports several intermediate crypto/after-hours statuses that must be
# treated as ACCEPTED (in-flight), not as rejections (which map to UNKNOWN and
# would trigger false "Order rejected" warnings + skip the trade).


def test_intermediate_statuses_are_accepted_not_rejected():
    from trader.brokers.alpaca import _map_order_status
    from trader.brokers.types import OrderStatus

    for raw in (
        "accepted_for_bidding",
        "pending_new",
        "pending_replace",
        "pending_cancel",
        "held",
        "calculated",
    ):
        assert _map_order_status(raw) is OrderStatus.PENDING, raw
    # PENDING is treated as accepted downstream.
    assert OrderStatus.PENDING not in (OrderStatus.REJECTED, OrderStatus.UNKNOWN)


def test_terminal_statuses_map_correctly():
    from trader.brokers.alpaca import _map_order_status
    from trader.brokers.types import OrderStatus

    assert _map_order_status("filled") is OrderStatus.FILLED
    assert _map_order_status("done_for_day") is OrderStatus.FILLED
    assert _map_order_status("rejected") is OrderStatus.REJECTED
    assert _map_order_status("suspended") is OrderStatus.REJECTED
    assert _map_order_status("canceled") is OrderStatus.CANCELED
    assert _map_order_status("expired") is OrderStatus.EXPIRED


def test_genuinely_unknown_status_stays_unknown():
    from trader.brokers.alpaca import _map_order_status
    from trader.brokers.types import OrderStatus

    assert _map_order_status("some_new_status_alpaca_invents") is OrderStatus.UNKNOWN


def test_enum_repr_prefixed_status_is_normalized():
    # Alpaca's SDK passes an OrderStatus enum whose str() is class-prefixed,
    # e.g. "OrderStatus.PENDING_NEW". Must strip the prefix and map correctly.
    from trader.brokers.alpaca import _map_order_status
    from trader.brokers.types import OrderStatus

    assert _map_order_status("OrderStatus.PENDING_NEW") is OrderStatus.PENDING
    assert _map_order_status("OrderStatus.FILLED") is OrderStatus.FILLED
    assert _map_order_status("OrderStatus.REJECTED") is OrderStatus.REJECTED


def test_enum_object_status_uses_value():
    # An actual enum object exposing .value should map via its wire value.
    from enum import Enum

    from trader.brokers.alpaca import _map_order_status
    from trader.brokers.types import OrderStatus

    class _AlpacaStatus(Enum):
        PENDING_NEW = "pending_new"
        FILLED = "filled"

    assert _map_order_status(_AlpacaStatus.PENDING_NEW) is OrderStatus.PENDING
    assert _map_order_status(_AlpacaStatus.FILLED) is OrderStatus.FILLED


def test_is_transient_detects_dropped_connection():
    from trader.brokers.alpaca import _is_transient

    # The real error seen in the wild: Alpaca closing an idle keep-alive.
    assert _is_transient(ConnectionError(
        "('Connection aborted.', RemoteDisconnected('Remote end closed "
        "connection without response'))"
    ))
    assert _is_transient(TimeoutError("Read timed out"))
    # A genuine programming error must NOT be treated as retryable.
    assert not _is_transient(ValueError("bad symbol"))
    assert not _is_transient(KeyError("missing"))


def test_retry_network_retries_then_succeeds(monkeypatch):
    from trader.brokers import alpaca

    # Don't actually sleep during the test.
    monkeypatch.setattr(alpaca.time, "sleep", lambda *_a, **_k: None)

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("Connection aborted. RemoteDisconnected")
        return "ok"

    assert alpaca._retry_network(flaky, what="test", attempts=3) == "ok"
    assert calls["n"] == 3  # failed twice, succeeded on the third


def test_retry_network_reraises_non_transient(monkeypatch):
    from trader.brokers import alpaca

    monkeypatch.setattr(alpaca.time, "sleep", lambda *_a, **_k: None)

    def boom():
        raise ValueError("not a network problem")

    import pytest

    with pytest.raises(ValueError):
        alpaca._retry_network(boom, what="test", attempts=3)


def test_retry_network_gives_up_after_attempts(monkeypatch):
    from trader.brokers import alpaca

    monkeypatch.setattr(alpaca.time, "sleep", lambda *_a, **_k: None)
    calls = {"n": 0}

    def always_flaky():
        calls["n"] += 1
        raise ConnectionError("Connection reset by peer")

    import pytest

    with pytest.raises(ConnectionError):
        alpaca._retry_network(always_flaky, what="test", attempts=3)
    assert calls["n"] == 3  # tried exactly `attempts` times


# ── close_position routing ────────────────────────────────────────────────────
# Alpaca's DELETE /v2/positions/{symbol} 404s for crypto (the "/" is a path
# separator, and %2F-encoding also 404s). Crypto is closed with a market SELL
# order instead; equities still use the native close_position.


class _RecordingTrading:
    def __init__(self, positions=None):
        self.closed: list[str] = []
        self.orders: list = []
        self._positions = positions or []

    def close_position(self, symbol):
        self.closed.append(symbol)

    def get_all_positions(self):
        return list(self._positions)

    def submit_order(self, req):
        self.orders.append(req)

        class _O:
            id = "order-1"
            status = "accepted"
            legs = None

        return _O()


class _FakePos:
    def __init__(self, symbol, qty):
        self.symbol = symbol
        self.qty = str(qty)
        self.avg_entry_price = "100"
        self.current_price = "100"
        self.market_value = "100"
        self.unrealized_pl = "0"
        self.unrealized_plpc = "0"


def _make_alpaca_broker(trading):
    from trader.brokers.alpaca import AlpacaBroker

    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker.trading = trading
    return broker


def test_close_position_crypto_submits_market_sell():
    trading = _RecordingTrading(positions=[_FakePos("BTCUSD", 0.5)])
    broker = _make_alpaca_broker(trading)

    assert broker.close_position("BTC/USD") is True
    # Did NOT call the broken DELETE endpoint...
    assert trading.closed == []
    # ...instead submitted a sell order for the full held qty.
    assert len(trading.orders) == 1
    req = trading.orders[0]
    assert req.symbol == "BTC/USD"
    assert float(req.qty) == 0.5
    assert str(req.side).lower().endswith("sell")


def test_close_position_crypto_no_position_returns_false():
    trading = _RecordingTrading(positions=[])
    broker = _make_alpaca_broker(trading)

    assert broker.close_position("BTC/USD") is False
    assert trading.orders == []


def test_close_position_equity_uses_native_close():
    trading = _RecordingTrading()
    broker = _make_alpaca_broker(trading)

    assert broker.close_position("AAPL") is True
    assert trading.closed == ["AAPL"]
    assert trading.orders == []


def test_close_position_equity_returns_false_on_error():
    class _Failing:
        def close_position(self, symbol):
            raise RuntimeError("boom")

    broker = _make_alpaca_broker(_Failing())
    assert broker.close_position("AAPL") is False


# ── Streaming: crypto must not reach the stock NBBO stream ─────────────────────
# Passing a crypto pair (with "/") to StockDataStream.subscribe_quotes causes a
# websocket "invalid syntax (400)". subscribe_quotes must drop crypto symbols.


class _RecordingStream:
    def __init__(self):
        self.subscribed: list[str] = []

    def subscribe_quotes(self, handler, *symbols):
        self.subscribed.extend(symbols)


def test_subscribe_quotes_filters_out_crypto():
    trading = _RecordingTrading()
    broker = _make_alpaca_broker(trading)
    stream = _RecordingStream()
    broker.stock_stream = stream

    broker.subscribe_quotes(["AAPL", "LTC/USD", "MSFT", "BTC/USD"], lambda *a: None)

    # Only equities are subscribed; crypto pairs are dropped.
    assert stream.subscribed == ["AAPL", "MSFT"]


def test_subscribe_quotes_all_crypto_subscribes_nothing():
    trading = _RecordingTrading()
    broker = _make_alpaca_broker(trading)
    stream = _RecordingStream()
    broker.stock_stream = stream

    broker.subscribe_quotes(["LTC/USD", "BTC/USD"], lambda *a: None)

    # Nothing valid to stream → the stock stream is never touched.
    assert stream.subscribed == []


# ── Historical bar window clamping ────────────────────────────────────────────
# Alpaca history starts in 2016, and the free Basic plan withholds only the
# latest 15 minutes of SIP data (IEX has no recency restriction at all).


def test_clamp_history_promotes_naive_datetimes_to_utc():
    from datetime import datetime, timezone

    from trader.brokers.alpaca import _clamp_history_window

    start, end = _clamp_history_window(
        datetime(2024, 1, 1), datetime(2024, 6, 1), now=datetime(2024, 6, 2)
    )
    assert start.tzinfo is timezone.utc
    assert end.tzinfo is timezone.utc


def test_clamp_history_start_limited_to_2016():
    from datetime import datetime, timezone

    from trader.brokers.alpaca import _ALPACA_HISTORY_START, _clamp_history_window

    start, _ = _clamp_history_window(
        datetime(2005, 3, 7, tzinfo=timezone.utc),
        None,
        now=datetime(2024, 6, 2, tzinfo=timezone.utc),
    )
    assert start == _ALPACA_HISTORY_START


def test_clamp_history_sip_end_pulled_behind_free_tier_cutoff():
    from datetime import datetime, timezone

    from trader.brokers.alpaca import _FREE_TIER_DELAY, _clamp_history_window

    now = datetime(2024, 6, 2, 15, 0, tzinfo=timezone.utc)
    _, end = _clamp_history_window(
        datetime(2024, 1, 1, tzinfo=timezone.utc), None, feed="sip", now=now
    )
    assert end == now - _FREE_TIER_DELAY


def test_clamp_history_iex_end_is_not_delayed():
    from datetime import datetime, timezone

    from trader.brokers.alpaca import _clamp_history_window

    now = datetime(2024, 6, 2, 15, 0, tzinfo=timezone.utc)
    _, end = _clamp_history_window(
        datetime(2024, 1, 1, tzinfo=timezone.utc), None, feed="iex", now=now
    )
    assert end == now


def test_clamp_history_past_end_is_untouched():
    from datetime import datetime, timezone

    from trader.brokers.alpaca import _clamp_history_window

    requested_end = datetime(2024, 5, 1, tzinfo=timezone.utc)
    _, end = _clamp_history_window(
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        requested_end,
        feed="sip",
        now=datetime(2024, 6, 2, tzinfo=timezone.utc),
    )
    assert end == requested_end


def test_feed_name_accepts_string_and_enum():
    from trader.brokers.alpaca import _feed_name

    class _Feed:
        value = "SIP"

    assert _feed_name("IEX") == "iex"
    assert _feed_name(_Feed()) == "sip"


class _RecordingBars:
    """Stands in for the Alpaca historical data clients."""

    def __init__(self):
        self.requests: list = []

    def _capture(self, req):
        import pandas as pd

        self.requests.append(req)

        class _Resp:
            df = pd.DataFrame()

        return _Resp()

    get_stock_bars = _capture
    get_crypto_bars = _capture


def test_get_bars_sends_clamped_window_and_warns_on_missing(caplog):
    from datetime import datetime, timezone

    from trader.brokers.alpaca import _FREE_TIER_DELAY
    from trader.brokers.types import TimeFrame

    broker = _make_alpaca_broker(_RecordingTrading())
    broker._feed = "sip"
    stock = _RecordingBars()
    broker.stock_data = stock

    with caplog.at_level("WARNING"):
        result = broker.get_bars(
            ["AAPL"], TimeFrame.DAY, start=datetime(2000, 1, 1)
        )

    assert result == {}
    req = stock.requests[0]
    # Start clamped to Alpaca's 2016 history floor...
    assert req.start.year == 2016
    # ...and the SIP end pulled behind the free-tier 15-minute cutoff. The SDK
    # normalises request datetimes to naive UTC, so compare in that form.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    assert req.end <= now_utc - _FREE_TIER_DELAY
    text = caplog.text
    assert "Alpaca data begins" in text
    assert "No bars returned" in text and "AAPL" in text