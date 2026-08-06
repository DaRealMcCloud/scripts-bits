"""Order manager — places and tracks orders through a neutral BrokerClient.

Improvements over the original:
- Broker-neutral (works with Alpaca or IBKR).
- Places a **real protective stop** — as a native bracket when the broker
  supports it, otherwise as a follow-up standalone stop order.
- Persists every order to the :class:`StateStore` (survives restarts).
- Retries transient submission failures.
"""

from __future__ import annotations

import logging

from trader.brokers._http import TransientError, with_retry
from trader.brokers.base import BrokerClient
from trader.brokers.types import (
    OrderRequest,
    OrderType,
    Side,
    TimeInForce,
)
from trader.config import Config
from trader.execution.state_store import OrderRecord, StateStore
from trader.risk.manager import RiskManager
from trader.risk.stops import get_stop_price
from trader.strategies.base import Signal, SignalDirection, is_crypto_symbol
from trader.strategies.quote_imbalance import QuoteImbalanceScorer

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(
        self,
        broker: BrokerClient,
        cfg: Config,
        risk: RiskManager,
        imbalance: QuoteImbalanceScorer,
        state: StateStore | None = None,
    ) -> None:
        self.broker = broker
        self.cfg = cfg
        self.risk = risk
        self.imbalance = imbalance
        self.state = state or StateStore()

    def execute_signal(self, signal: Signal) -> dict | None:
        """Evaluate a signal through risk checks, imbalance timing, then place an order."""
        is_crypto = is_crypto_symbol(signal.symbol)

        # 0. Crypto cannot be shorted on Alpaca — block defensively.
        if is_crypto and signal.direction == SignalDirection.SHORT:
            logger.debug("Skipping crypto short for %s (unsupported)", signal.symbol)
            return None

        # 1. Drawdown breaker
        if not self.risk.check_drawdown():
            logger.warning("Drawdown breaker active — skipping %s", signal.symbol)
            return None

        # 2. Position limit (counted separately for crypto vs equity)
        asset_class = "crypto" if is_crypto else "us_equity"
        if not self.risk.can_open_position(asset_class):
            return None

        # 2b. Duplicate-entry guard: never open a second position (or stack a
        # new order) in a symbol we already hold or already have a working
        # order for. Strategies re-emit the same setup every scan, and two
        # near-simultaneous scans (e.g. the startup scan racing the scheduled
        # one) would otherwise buy the same coin twice before the first fill is
        # visible. Only applies to entries (LONG); exits are unaffected.
        if signal.direction == SignalDirection.LONG and self._has_open_exposure(
            signal.symbol
        ):
            logger.info(
                "Skipping %s — already holding a position or have a working "
                "order for it (no pyramiding).",
                signal.symbol,
            )
            return None

        # 3. Quote imbalance timing gate (equity only; crypto has no NBBO stream here)
        if not is_crypto and not self.imbalance.check_entry_timing(
            signal.symbol, signal.direction
        ):
            logger.info("Imbalance timing rejected entry for %s", signal.symbol)
            return None

        # 4. Get current price for entry
        entry_price = self._entry_price(signal)
        if entry_price is None:
            return None

        # 5. Compute stop and position size
        side_str = signal.direction.value
        stop = signal.stop_hint or get_stop_price(
            self.broker, signal.symbol, entry_price, side_str, self.cfg
        )

        # 5b. Fee-aware edge gate: skip trades whose expected favourable move
        # can't clear round-trip trading costs by a safety multiple. This
        # protects thin-margin (scalp-style) crypto trades where fees would
        # otherwise eat the entire edge.
        if not self._passes_fee_gate(signal, entry_price, stop):
            return None

        qty = self.risk.compute_position_size(
            signal, entry_price, stop, allow_fractional=is_crypto
        )

        # Crypto is fractional; equity must be >= 1 whole share.
        if qty <= 0 or (not is_crypto and qty < 1):
            logger.info("Position size too small for %s (qty=%s)", signal.symbol, qty)
            return None

        # 6. Place the order (bracket if supported, else order + follow-up stop)
        return self._place(signal, entry_price, stop, qty, is_crypto)

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        """Normalise a symbol for comparison across broker id forms.

        Alpaca returns crypto positions/orders in de-slashed form (``LTCUSD``)
        even though we submit the slashed form (``LTC/USD``). Strip the slash
        and upper-case so both forms compare equal.
        """
        return symbol.replace("/", "").upper()

    def _has_open_exposure(self, symbol: str) -> bool:
        """Return True if we already hold ``symbol`` or have a working order for it.

        Checks live broker positions AND open orders so we don't re-enter during
        the window between submitting the first buy and it showing up as a
        position. Fails safe: if the broker read errors, returns True (skip the
        entry) rather than risk a duplicate buy.
        """
        key = self._symbol_key(symbol)
        try:
            for pos in self.broker.get_positions():
                if self._symbol_key(pos.symbol) == key and abs(pos.qty) > 0:
                    return True
            for order in self.broker.get_open_orders():
                if self._symbol_key(order.symbol) == key:
                    return True
        except Exception:
            logger.warning(
                "Could not verify existing exposure for %s; skipping entry to "
                "avoid a duplicate.",
                symbol,
                exc_info=True,
            )
            return True
        return False

    def _passes_fee_gate(
        self, signal: Signal, entry_price: float, stop: float
    ) -> bool:
        """Return True if the trade's expected edge clears round-trip fees.

        Uses the entry→stop distance as a conservative proxy for the expected
        reward per unit (a well-formed setup targets at least its risk). If the
        expected favourable move doesn't exceed round-trip fees by
        ``min_edge_multiple``, the trade is rejected before sizing.
        """
        # Prefer taker per-side fee when present (round-trip = taker * 2).
        taker = getattr(self.cfg.risk, "taker_fee_pct", None)
        if taker is not None and taker > 0:
            fee_pct = taker * 2.0
        else:
            fee_pct = getattr(self.cfg.risk, "round_trip_fee_pct", 0.0)

        if fee_pct <= 0 or entry_price <= 0:
            return True  # gate disabled or no price → don't block

        edge_pct = abs(entry_price - stop) / entry_price
        min_multiple = getattr(self.cfg.risk, "min_edge_multiple", 0.0)
        required = fee_pct * min_multiple
        if edge_pct < required:
            logger.info(
                "Fee gate rejected %s: edge %.3f%% < required %.3f%% "
                "(round-trip fee %.3f%% x %.1f)",
                signal.symbol,
                edge_pct * 100,
                required * 100,
                fee_pct * 100,
                min_multiple,
            )
            return False
        return True

    def _entry_price(self, signal: Signal) -> float | None:
        try:
            quotes = self.broker.get_latest_quotes([signal.symbol])
            quote = quotes.get(signal.symbol)
            if quote is None:
                logger.warning("No quote for %s", signal.symbol)
                return None
            price = quote.ask_price if signal.direction == SignalDirection.LONG else quote.bid_price
            if price <= 0:
                # Fall back to mid if one side missing.
                price = quote.mid
            return float(price) if price and price > 0 else None
        except Exception:
            logger.warning("Could not get quote for %s", signal.symbol, exc_info=True)
            return None

    @with_retry(max_attempts=3, base_delay=1.0, retry_on=(TransientError,))
    def _submit(self, request: OrderRequest):
        return self.broker.submit_order(request)

    @staticmethod
    def _round_price(price: float, is_crypto: bool) -> float:
        # Crypto needs finer precision than equity cents.
        return round(price, 6) if is_crypto else round(price, 2)

    def _place(
        self,
        signal: Signal,
        entry_price: float,
        stop: float,
        qty: float,
        is_crypto: bool = False,
    ) -> dict | None:
        side = Side.BUY if signal.direction == SignalDirection.LONG else Side.SELL
        caps = self.broker.capabilities

        # Crypto: no broker-side brackets/stops, and DAY orders are rejected — use GTC.
        if is_crypto:
            attach_stop = None
            tif = TimeInForce.GTC

            # Crypto is cash/spot only (no margin). Guard against submitting
            # buys we can't fund — otherwise Alpaca returns 403 "insufficient
            # balance" and we spam the same failing candidates every scan.
            if side == Side.BUY:
                try:
                    cash = self.broker.get_cash()
                except Exception:
                    cash = None
                if cash is not None:
                    notional = entry_price * qty
                    # Keep a small buffer for fees/slippage.
                    if notional > cash * 0.99:
                        logger.info(
                            "Skipping %s: order notional $%.2f exceeds available "
                            "cash $%.2f",
                            signal.symbol,
                            notional,
                            cash,
                        )
                        return None
        else:
            attach_stop = stop if caps.supports_bracket_orders else None
            tif = TimeInForce.DAY

        request = OrderRequest(
            symbol=signal.symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            limit_price=self._round_price(entry_price, is_crypto),
            time_in_force=tif,
            attached_stop_price=attach_stop,
            strategy=signal.strategy,
        )

        try:
            result = self._submit(request)
        except Exception:
            logger.error("Order submission failed for %s", signal.symbol, exc_info=True)
            return None

        if not result.accepted:
            logger.warning("Order rejected for %s (status=%s)", signal.symbol, result.status.value)
            return None

        child_ids = list(result.child_order_ids)

        # Fallback: broker can't bracket → place a standalone protective stop.
        # Crypto has no broker-side stops; a software stop is tracked separately.
        if not is_crypto and attach_stop is None and caps.supports_native_stops:
            stop_side = Side.SELL if side == Side.BUY else Side.BUY
            try:
                stop_res = self.broker.submit_stop_order(
                    signal.symbol, stop_side, qty, round(stop, 2), TimeInForce.GTC
                )
                if stop_res.accepted:
                    child_ids.append(stop_res.order_id)
                else:
                    logger.warning("Protective stop rejected for %s", signal.symbol)
            except Exception:
                logger.error("Failed to place protective stop for %s", signal.symbol, exc_info=True)

        # Crypto: persist a software stop for crypto_scan to enforce.
        if is_crypto and stop and stop > 0:
            try:
                self.state.set_crypto_stop(signal.symbol, float(stop), float(entry_price))
            except Exception:
                logger.debug("Failed to persist crypto stop for %s", signal.symbol, exc_info=True)

        record = OrderRecord(
            order_id=result.order_id,
            symbol=signal.symbol,
            side=side.value,
            qty=qty,
            limit_price=entry_price,
            stop_price=stop,
            strategy=signal.strategy,
            reason=signal.reason,
            status=result.status.value,
            child_order_ids=child_ids,
        )
        self.state.record_order(record)

        logger.info(
            "ORDER PLACED: %s %s %s units @ $%.6g (stop $%.6g) [%s]",
            side.value,
            signal.symbol,
            f"{qty:.8f}".rstrip("0").rstrip(".") if is_crypto else f"{qty:.0f}",
            entry_price,
            stop,
            signal.strategy,
        )
        return {
            "time": record.created_at,
            "symbol": signal.symbol,
            "side": side.value,
            "qty": qty,
            "limit_price": entry_price,
            "stop_price": stop,
            "strategy": signal.strategy,
            "reason": signal.reason,
            "order_id": result.order_id,
        }

    def cancel_all(self, include_crypto: bool = False) -> None:
        """Cancel open orders. By default crypto orders are left in place so
        the 24/7 crypto loop is not disrupted by the equity EOD routine."""
        if include_crypto:
            try:
                self.broker.cancel_all_orders()
                logger.info("All open orders cancelled")
            except Exception:
                logger.error("Failed to cancel orders", exc_info=True)
            return
        # Cancel equity orders individually, skipping crypto.
        try:
            for o in self.broker.get_open_orders():
                if is_crypto_symbol(o.symbol):
                    continue
                self.broker.cancel_order(o.order_id)
            logger.info("Equity open orders cancelled (crypto left intact)")
        except Exception:
            logger.error("Failed to cancel equity orders", exc_info=True)

    def flatten_all(self, include_crypto: bool = False) -> None:
        """Close positions. By default crypto positions are left open so 24/7
        crypto trades are not force-closed at the equity market close."""
        if include_crypto:
            try:
                self.broker.close_all_positions()
                logger.info("All positions closed")
            except Exception:
                logger.error("Failed to close positions", exc_info=True)
            return
        # Close equity positions individually, skipping crypto.
        try:
            for p in self.broker.get_positions():
                if is_crypto_symbol(p.symbol):
                    continue
                self.broker.close_position(p.symbol)
            logger.info("Equity positions closed (crypto left intact)")
        except Exception:
            logger.error("Failed to close equity positions", exc_info=True)

    @property
    def orders_today(self) -> list[dict]:
        return self.state.orders_today()

    def reset_daily(self) -> None:
        # Orders are persisted and date-scoped in the store; nothing to clear.
        return None
