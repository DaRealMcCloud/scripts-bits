"""Alpaca broker adapter.

Implements :class:`trader.brokers.base.BrokerClient` on top of the ``alpaca-py``
SDK.  All Alpaca-native objects are translated into the neutral DTOs from
:mod:`trader.brokers.types` so that no vendor type leaks above this layer.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from trader.brokers.base import (
    BrokerCapabilities,
    BrokerClient,
    InstrumentIdKind,
    QuoteCallback,
)
from trader.brokers.types import (
    Account,
    AssetClass,
    Bar,
    Instrument,
    Order,
    OrderRequest,
    OrderResult,
    OrderStatus,
    OrderType,
    Position,
    Quote,
    Side,
    Snapshot,
    TimeFrame,
    TimeInForce,
)

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Substrings identifying a transient network hiccup (dropped connection, reset,
# timeout) that is safe to retry. Alpaca occasionally closes an idle keep-alive
# connection, surfacing as ``RemoteDisconnected`` / ``ConnectionError``.
_TRANSIENT_MARKERS = (
    "remotedisconnected",
    "connection aborted",
    "connection reset",
    "connection refused",
    "connectionerror",
    "read timed out",
    "timed out",
    "temporarily unavailable",
    "max retries exceeded",
    "bad gateway",
    "service unavailable",
)


def _is_transient(exc: Exception) -> bool:
    """Heuristically decide if an exception is a retryable network blip."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _retry_network(
    func: Callable[[], _T], *, what: str, attempts: int = 3, base_delay: float = 0.5
) -> _T:
    """Call ``func`` retrying transient network errors with backoff.

    Non-transient errors propagate immediately. Used for market-data reads
    (quotes/positions/snapshots) where a dropped keep-alive connection should
    not abort a safety-critical operation like the crypto stop check.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return func()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not transient
            if not _is_transient(exc) or attempt >= attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Transient network error on %s (attempt %d/%d): %s — retrying in %.1fs",
                what,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)


# Quote currencies Alpaca supports for crypto. Used to re-insert the ``/`` that
# Alpaca's positions endpoint strips (it returns ``BTCUSD``; the rest of the
# app — universe, signals, stops — uses the ``BTC/USD`` canonical form).
_CRYPTO_QUOTES = ("USDT", "USDC", "USD", "BTC", "USDG", "DAI")


def _normalize_crypto_symbol(symbol: str) -> str:
    """Return the canonical slashed crypto form (``BTCUSD`` → ``BTC/USD``).

    Alpaca's ``get_all_positions`` returns crypto symbols without the slash,
    while orders, quotes and the universe all use ``BASE/QUOTE``. Equities have
    no matching quote suffix and are returned unchanged.
    """
    if "/" in symbol:
        return symbol
    up = symbol.upper()
    for quote in _CRYPTO_QUOTES:
        if up.endswith(quote) and len(up) > len(quote):
            return f"{up[: -len(quote)]}/{quote}"
    return symbol


# ── TimeFrame mapping (neutral → alpaca) ─────────────────────────────────────
def _to_alpaca_timeframe(tf: TimeFrame):
    from alpaca.data.timeframe import TimeFrame as ATF
    from alpaca.data.timeframe import TimeFrameUnit

    mapping = {
        TimeFrame.MIN_1: ATF(1, TimeFrameUnit.Minute),
        TimeFrame.MIN_5: ATF(5, TimeFrameUnit.Minute),
        TimeFrame.MIN_15: ATF(15, TimeFrameUnit.Minute),
        TimeFrame.MIN_30: ATF(30, TimeFrameUnit.Minute),
        TimeFrame.HOUR_1: ATF(1, TimeFrameUnit.Hour),
        TimeFrame.DAY: ATF(1, TimeFrameUnit.Day),
        TimeFrame.WEEK: ATF(1, TimeFrameUnit.Week),
    }
    return mapping.get(tf, ATF(1, TimeFrameUnit.Day))


def _to_alpaca_side(side: Side):
    from alpaca.trading.enums import OrderSide

    return OrderSide.BUY if side == Side.BUY else OrderSide.SELL


def _to_alpaca_tif(tif: TimeInForce):
    from alpaca.trading.enums import TimeInForce as ATIF

    mapping = {
        TimeInForce.DAY: ATIF.DAY,
        TimeInForce.GTC: ATIF.GTC,
        TimeInForce.IOC: ATIF.IOC,
        TimeInForce.FOK: ATIF.FOK,
        TimeInForce.OPG: ATIF.OPG,
    }
    return mapping.get(tif, ATIF.DAY)


def _to_alpaca_feed(feed):
    """Convert a config feed value ("iex"/"sip"/"otc") to the SDK's DataFeed enum.

    ``StockDataStream`` expects a :class:`alpaca.data.enums.DataFeed` member
    (it accesses ``feed.value``), not a raw string. Accepts an existing enum
    member unchanged and falls back to IEX for anything unrecognised.
    """
    from alpaca.data.enums import DataFeed

    if isinstance(feed, DataFeed):
        return feed
    try:
        return DataFeed(str(feed).lower())
    except ValueError:
        logger.warning("Unknown Alpaca data_feed %r; falling back to iex", feed)
        return DataFeed.IEX


def _map_order_status(raw: str) -> OrderStatus:
    # Alpaca's SDK returns an OrderStatus *enum*, whose str() is
    # "OrderStatus.PENDING_NEW" (class-prefixed), not the wire value
    # "pending_new". Prefer the enum's .value; otherwise strip any
    # "SomeEnum." prefix so both enum and raw-string inputs normalize.
    value = getattr(raw, "value", None)
    s = str(value if value is not None else raw).lower()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    mapping = {
        "new": OrderStatus.NEW,
        "accepted": OrderStatus.NEW,
        # Crypto and after-hours orders often report these intermediate,
        # not-yet-final states. They are ACCEPTED (in-flight), not rejections —
        # mapping them to PENDING avoids false "Order rejected" warnings.
        "accepted_for_bidding": OrderStatus.PENDING,
        "pending_new": OrderStatus.PENDING,
        "pending_replace": OrderStatus.PENDING,
        "pending_cancel": OrderStatus.PENDING,
        "held": OrderStatus.PENDING,
        "calculated": OrderStatus.PENDING,
        "partially_filled": OrderStatus.PARTIALLY_FILLED,
        "filled": OrderStatus.FILLED,
        "done_for_day": OrderStatus.FILLED,
        "canceled": OrderStatus.CANCELED,
        "cancelled": OrderStatus.CANCELED,
        "rejected": OrderStatus.REJECTED,
        "suspended": OrderStatus.REJECTED,
        "stopped": OrderStatus.REJECTED,
        "expired": OrderStatus.EXPIRED,
    }
    return mapping.get(s, OrderStatus.UNKNOWN)


class AlpacaBroker(BrokerClient):
    """Alpaca implementation of the neutral broker interface."""

    def __init__(self, cfg) -> None:
        self._cfg = cfg
        self._feed = cfg.alpaca.data_feed
        self._paper = cfg.alpaca.mode == "paper"
        self._key = cfg.alpaca.api_key
        self._secret = cfg.alpaca.secret_key

        self.trading = None
        self.stock_data = None
        self.crypto_data = None
        self.stock_stream = None
        self._stream_thread: threading.Thread | None = None
        self._stream_running = False

        self._caps = BrokerCapabilities(
            name="alpaca",
            id_kind=InstrumentIdKind.SYMBOL,
            supports_streaming=True,
            supports_bar_history=True,
            supports_snapshots=True,
            supports_native_stops=True,
            supports_bracket_orders=True,
            supports_short=True,
            supports_fractional=True,
            supports_crypto=True,
            requires_session_keepalive=False,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    # ── Lifecycle ────────────────────────────────────────────
    def connect(self) -> None:
        from alpaca.data.historical import (
            CryptoHistoricalDataClient,
            StockHistoricalDataClient,
        )
        from alpaca.data.live import StockDataStream
        from alpaca.trading.client import TradingClient

        self.trading = TradingClient(self._key, self._secret, paper=self._paper)
        self.stock_data = StockHistoricalDataClient(self._key, self._secret)
        self.crypto_data = CryptoHistoricalDataClient(self._key, self._secret)
        self.stock_stream = StockDataStream(self._key, self._secret, feed=_to_alpaca_feed(self._feed))
        # Validate credentials early.
        self.trading.get_account()
        logger.info("Alpaca broker connected (mode=%s, feed=%s)", self._cfg.alpaca.mode, self._feed)

    def close(self) -> None:
        self.stop_stream()

    # ── Account & positions ──────────────────────────────────
    def get_account(self) -> Account:
        a = self.trading.get_account()
        return Account(
            account_id=str(getattr(a, "account_number", "") or getattr(a, "id", "")),
            equity=float(a.equity),
            cash=float(a.cash),
            buying_power=float(a.buying_power),
            currency=getattr(a, "currency", "USD") or "USD",
            unrealized_pl=float(getattr(a, "unrealized_pl", 0) or 0),
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        raw_positions = _retry_network(
            self.trading.get_all_positions, what="positions"
        )
        for p in raw_positions:
            qty = float(p.qty)
            symbol = _normalize_crypto_symbol(p.symbol)
            out.append(
                Position(
                    symbol=symbol,
                    qty=qty,
                    avg_entry_price=float(p.avg_entry_price),
                    current_price=float(p.current_price),
                    market_value=float(p.market_value),
                    unrealized_pl=float(p.unrealized_pl),
                    unrealized_pl_pct=float(p.unrealized_plpc) * 100,
                    asset_class=(
                        AssetClass.CRYPTO
                        if "/" in symbol
                        else AssetClass.US_EQUITY
                    ),
                )
            )
        return out

    # ── Instruments ──────────────────────────────────────────
    def resolve_instrument(self, symbol: str) -> Instrument | None:
        try:
            asset = self.trading.get_asset(symbol)
        except Exception:
            return None
        return Instrument(
            symbol=asset.symbol,
            broker_id=asset.symbol,
            asset_class=(
                AssetClass.CRYPTO
                if "/" in asset.symbol
                else AssetClass.US_EQUITY
            ),
            exchange=str(getattr(asset, "exchange", "") or ""),
            tradable=bool(getattr(asset, "tradable", True)),
            fractionable=bool(getattr(asset, "fractionable", False)),
        )

    def list_tradable_equities(self) -> list[Instrument]:
        from alpaca.trading.enums import AssetClass as AAssetClass
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        req = GetAssetsRequest(asset_class=AAssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
        assets = self.trading.get_all_assets(req)
        return [
            Instrument(
                symbol=a.symbol,
                broker_id=a.symbol,
                asset_class=AssetClass.US_EQUITY,
                exchange=str(getattr(a, "exchange", "") or ""),
                tradable=bool(a.tradable),
                fractionable=bool(a.fractionable),
            )
            for a in assets
            if a.tradable and a.fractionable
        ]

    def list_tradable_crypto(self) -> list[Instrument]:
        from alpaca.trading.enums import AssetClass as AAssetClass
        from alpaca.trading.enums import AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        req = GetAssetsRequest(asset_class=AAssetClass.CRYPTO, status=AssetStatus.ACTIVE)
        assets = self.trading.get_all_assets(req)
        return [
            Instrument(
                symbol=a.symbol,
                broker_id=a.symbol,
                asset_class=AssetClass.CRYPTO,
                exchange=str(getattr(a, "exchange", "") or ""),
                tradable=bool(a.tradable),
                fractionable=bool(getattr(a, "fractionable", True)),
            )
            for a in assets
            if a.tradable
        ]

    # ── Market data ──────────────────────────────────────────
    def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        crypto = [s for s in symbols if "/" in s]
        equity = [s for s in symbols if "/" not in s]
        result: dict[str, list[Bar]] = {}
        atf = _to_alpaca_timeframe(timeframe)

        if equity:
            from alpaca.data.requests import StockBarsRequest

            req = StockBarsRequest(
                symbol_or_symbols=list(equity),
                timeframe=atf,
                start=start,
                end=end,
                feed=self._feed,
            )
            self._extract_bars(self.stock_data.get_stock_bars(req).df, result)
        if crypto:
            from alpaca.data.requests import CryptoBarsRequest

            req = CryptoBarsRequest(
                symbol_or_symbols=list(crypto),
                timeframe=atf,
                start=start,
                end=end,
            )
            self._extract_bars(self.crypto_data.get_crypto_bars(req).df, result)
        return result

    @staticmethod
    def _extract_bars(df, result: dict[str, list[Bar]]) -> None:
        if df is None or df.empty:
            return
        for symbol in df.index.get_level_values(0).unique():
            sub = df.loc[symbol]
            bars: list[Bar] = []
            for ts, row in sub.iterrows():
                bars.append(
                    Bar(
                        symbol=symbol,
                        timestamp=ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                        vwap=float(row["vwap"]) if "vwap" in row else None,
                        trade_count=int(row["trade_count"]) if "trade_count" in row else None,
                    )
                )
            result[symbol] = bars

    def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        from alpaca.data.requests import StockSnapshotRequest

        syms = list(symbols)
        equities = [s for s in syms if "/" not in s]
        crypto = [s for s in syms if "/" in s]
        out: dict[str, Snapshot] = {}

        if equities:
            req = StockSnapshotRequest(symbol_or_symbols=equities, feed=self._feed)
            raw = self.stock_data.get_stock_snapshot(req)
            self._extract_snapshots(raw, out)
        if crypto:
            out.update(self._crypto_snapshots(crypto))
        return out

    def _extract_snapshots(self, raw: dict, out: dict[str, Snapshot]) -> None:
        for sym, snap in raw.items():
            db = getattr(snap, "daily_bar", None)
            pdb = getattr(snap, "previous_daily_bar", None)
            lt = getattr(snap, "latest_trade", None)
            lq = getattr(snap, "latest_quote", None)
            quote = None
            if lq is not None:
                quote = Quote(
                    symbol=sym,
                    bid_price=float(getattr(lq, "bid_price", 0) or 0),
                    ask_price=float(getattr(lq, "ask_price", 0) or 0),
                    bid_size=float(getattr(lq, "bid_size", 0) or 0),
                    ask_size=float(getattr(lq, "ask_size", 0) or 0),
                    timestamp=getattr(lq, "timestamp", None),
                )
            out[sym] = Snapshot(
                symbol=sym,
                latest_price=float(lt.price) if lt is not None else None,
                daily_open=float(db.open) if db is not None else None,
                daily_high=float(db.high) if db is not None else None,
                daily_low=float(db.low) if db is not None else None,
                daily_close=float(db.close) if db is not None else None,
                daily_volume=float(db.volume) if db is not None else None,
                prev_daily_close=float(pdb.close) if pdb is not None else None,
                quote=quote,
            )

    def _crypto_snapshots(self, symbols: list[str]) -> dict[str, Snapshot]:
        from alpaca.data.requests import CryptoSnapshotRequest

        req = CryptoSnapshotRequest(symbol_or_symbols=symbols)
        raw = self.crypto_data.get_crypto_snapshot(req)
        out: dict[str, Snapshot] = {}
        self._extract_snapshots(raw, out)
        return out

    def get_latest_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        from alpaca.data.requests import StockLatestQuoteRequest

        syms = list(symbols)
        equities = [s for s in syms if "/" not in s]
        crypto = [s for s in syms if "/" in s]
        out: dict[str, Quote] = {}

        if equities:
            req = StockLatestQuoteRequest(symbol_or_symbols=equities, feed=self._feed)
            raw = _retry_network(
                lambda: self.stock_data.get_stock_latest_quote(req),
                what="equity latest quotes",
            )
            self._extract_quotes(raw, out)
        if crypto:
            from alpaca.data.requests import CryptoLatestQuoteRequest

            creq = CryptoLatestQuoteRequest(symbol_or_symbols=crypto)
            craw = _retry_network(
                lambda: self.crypto_data.get_crypto_latest_quote(creq),
                what="crypto latest quotes",
            )
            self._extract_quotes(craw, out)
        return out

    @staticmethod
    def _extract_quotes(raw: dict, out: dict[str, Quote]) -> None:
        for sym, q in raw.items():
            out[sym] = Quote(
                symbol=sym,
                bid_price=float(getattr(q, "bid_price", 0) or 0),
                ask_price=float(getattr(q, "ask_price", 0) or 0),
                bid_size=float(getattr(q, "bid_size", 0) or 0),
                ask_size=float(getattr(q, "ask_size", 0) or 0),
                timestamp=getattr(q, "timestamp", None),
            )

    # ── Streaming ────────────────────────────────────────────
    def subscribe_quotes(self, symbols: Sequence[str], callback: QuoteCallback) -> None:
        if not symbols:
            return

        # The stock NBBO stream only accepts equity symbols. Crypto pairs (with a
        # "/") are rejected by the websocket ("invalid syntax (400)"), so drop
        # them defensively — crypto has no imbalance stream here anyway.
        equity_symbols = [s for s in symbols if "/" not in s]
        if not equity_symbols:
            logger.info("subscribe_quotes: no equity symbols to stream (crypto skipped)")
            return
        if len(equity_symbols) != len(symbols):
            skipped = [s for s in symbols if "/" in s]
            logger.debug("subscribe_quotes: skipping crypto symbols %s", skipped)

        async def _handler(data):
            q = Quote(
                symbol=data.symbol,
                bid_price=float(getattr(data, "bid_price", 0) or 0),
                ask_price=float(getattr(data, "ask_price", 0) or 0),
                bid_size=float(getattr(data, "bid_size", 0) or 0),
                ask_size=float(getattr(data, "ask_size", 0) or 0),
                timestamp=getattr(data, "timestamp", None),
            )
            callback(data.symbol, q.bid_size, q.ask_size, q)

        self.stock_stream.subscribe_quotes(_handler, *equity_symbols)

    def start_stream(self) -> None:
        if self._stream_running:
            return

        def _run():
            try:
                self.stock_stream.run()
            except Exception:
                logger.error("Alpaca quote stream died", exc_info=True)

        self._stream_thread = threading.Thread(target=_run, daemon=True, name="alpaca-stream")
        self._stream_thread.start()
        self._stream_running = True

    def stop_stream(self) -> None:
        if self._stream_running and self.stock_stream is not None:
            try:
                self.stock_stream.stop()
            except Exception:
                pass
            self._stream_running = False

    # ── Orders ───────────────────────────────────────────────
    def submit_order(self, request: OrderRequest) -> OrderResult:
        from alpaca.trading.enums import OrderClass
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            StopLossRequest,
        )

        side = _to_alpaca_side(request.side)
        tif = _to_alpaca_tif(request.time_in_force)

        common: dict = {
            "symbol": request.symbol,
            "side": side,
            "qty": request.qty,
            "time_in_force": tif,
        }
        # Attach a native stop-loss (bracket) if requested and this is an entry.
        if request.attached_stop_price is not None:
            common["order_class"] = OrderClass.OTO
            common["stop_loss"] = StopLossRequest(
                stop_price=round(request.attached_stop_price, 2)
            )

        if request.order_type == OrderType.MARKET:
            req = MarketOrderRequest(**common)
        else:
            # Crypto needs finer price precision than equity cents; rounding a
            # crypto limit to 2dp can violate Alpaca's tick size and get the
            # order rejected (status=unknown). Equities stay at cent precision.
            is_crypto = "/" in request.symbol
            raw_limit = request.limit_price or 0
            limit = round(raw_limit, 6) if is_crypto else round(raw_limit, 2)
            req = LimitOrderRequest(limit_price=limit, **common)

        order = self.trading.submit_order(req)
        child_ids = tuple(str(leg.id) for leg in (getattr(order, "legs", None) or []))
        raw_status = getattr(order, "status", "")
        mapped = _map_order_status(raw_status)
        if mapped is OrderStatus.UNKNOWN:
            # Surface the exact Alpaca status so rejections aren't opaque.
            logger.warning(
                "Alpaca returned unmapped order status %r for %s (qty=%s, limit=%s)",
                str(raw_status),
                request.symbol,
                request.qty,
                common.get("limit_price") if request.order_type != OrderType.MARKET else "mkt",
            )
        return OrderResult(
            order_id=str(order.id),
            status=mapped,
            symbol=request.symbol,
            submitted_qty=request.qty,
            child_order_ids=child_ids,
            raw={"alpaca_status": str(raw_status)},
        )

    def submit_stop_order(
        self, symbol: str, side, qty: float, stop_price: float, time_in_force
    ) -> OrderResult:
        from alpaca.trading.requests import StopOrderRequest

        aside = _to_alpaca_side(side if isinstance(side, Side) else Side(side))
        atif = _to_alpaca_tif(
            time_in_force if isinstance(time_in_force, TimeInForce) else TimeInForce(time_in_force)
        )
        req = StopOrderRequest(
            symbol=symbol,
            side=aside,
            qty=qty,
            stop_price=round(stop_price, 2),
            time_in_force=atif,
        )
        order = self.trading.submit_order(req)
        return OrderResult(
            order_id=str(order.id),
            status=_map_order_status(getattr(order, "status", "")),
            symbol=symbol,
            submitted_qty=qty,
        )

    def cancel_all_orders(self) -> None:
        self.trading.cancel_orders()

    def cancel_order(self, order_id: str) -> None:
        try:
            self.trading.cancel_order_by_id(order_id)
        except Exception:
            logger.warning("Failed to cancel order %s", order_id, exc_info=True)

    def close_all_positions(self) -> None:
        self.trading.close_all_positions(cancel_orders=True)

    def close_position(self, symbol: str) -> bool:
        """Close a single position.

        Crypto: Alpaca's DELETE /v2/positions/{symbol} endpoint chokes on the
        slash in "BTC/USD" (path separator -> 404) and URL-encoding it (%2F)
        also 404s. Instead we submit a plain market SELL for the full held
        quantity — the order endpoint accepts the slashed symbol fine.

        Equities: use the native close_position (handles fractional liquidation).
        """
        if "/" in symbol:
            return self._close_crypto_via_market_sell(symbol)
        try:
            self.trading.close_position(symbol)
            return True
        except Exception:
            logger.warning("Failed to close position %s", symbol, exc_info=True)
            return False

    def _close_crypto_via_market_sell(self, symbol: str) -> bool:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        # Find the held quantity for this crypto symbol.
        qty = 0.0
        for p in self.get_positions():
            if p.symbol == symbol:
                qty = float(p.qty)
                break
        if qty <= 0:
            logger.warning("No open crypto position found for %s to close", symbol)
            return False
        try:
            req = MarketOrderRequest(
                symbol=symbol,
                side=OrderSide.SELL,
                qty=qty,
                time_in_force=TimeInForce.GTC,
            )
            self.trading.submit_order(req)
            return True
        except Exception:
            logger.warning("Failed to close position %s", symbol, exc_info=True)
            return False

    def get_open_orders(self) -> list[Order]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self.trading.get_orders(req)
        out: list[Order] = []
        for o in orders:
            out.append(
                Order(
                    order_id=str(o.id),
                    symbol=_normalize_crypto_symbol(o.symbol),
                    side=Side.BUY if str(o.side).lower().endswith("buy") else Side.SELL,
                    qty=float(o.qty or 0),
                    order_type=OrderType(str(o.order_type).lower())
                    if str(o.order_type).lower() in {e.value for e in OrderType}
                    else OrderType.LIMIT,
                    status=_map_order_status(getattr(o, "status", "")),
                    limit_price=float(o.limit_price) if o.limit_price else None,
                    stop_price=float(o.stop_price) if o.stop_price else None,
                    filled_qty=float(o.filled_qty or 0),
                    filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
                    submitted_at=getattr(o, "submitted_at", None),
                )
            )
        return out

    def get_order_status(self, order_id: str) -> OrderStatus | None:
        """Look up a single order's current status by id (for reconciliation)."""
        try:
            o = self.trading.get_order_by_id(order_id)
        except Exception:
            logger.debug("Could not fetch order %s for status", order_id, exc_info=True)
            return None
        return _map_order_status(getattr(o, "status", ""))

    def get_recent_orders(self, lookback_days: int = 7) -> list[Order]:
        """Fetch recent orders (all statuses) with fill prices for backfill."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        after = datetime.now(timezone.utc) - timedelta(days=max(lookback_days, 1))
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.ALL,
                after=after,
                limit=500,
                nested=False,
            )
            orders = self.trading.get_orders(req)
        except Exception:
            logger.debug("Could not fetch recent orders for backfill", exc_info=True)
            return []

        out: list[Order] = []
        for o in orders:
            out.append(
                Order(
                    order_id=str(o.id),
                    symbol=_normalize_crypto_symbol(o.symbol),
                    side=Side.BUY if str(o.side).lower().endswith("buy") else Side.SELL,
                    qty=float(o.qty or 0),
                    order_type=OrderType(str(o.order_type).lower())
                    if str(o.order_type).lower() in {e.value for e in OrderType}
                    else OrderType.LIMIT,
                    status=_map_order_status(getattr(o, "status", "")),
                    limit_price=float(o.limit_price) if o.limit_price else None,
                    stop_price=float(o.stop_price) if o.stop_price else None,
                    filled_qty=float(o.filled_qty or 0),
                    filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price else None,
                    submitted_at=getattr(o, "submitted_at", None),
                )
            )
        return out
