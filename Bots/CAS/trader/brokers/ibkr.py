"""IBKR broker adapter (Client Portal Gateway Web API).

Implements :class:`trader.brokers.base.BrokerClient` on top of the IBKR Web API
served by the local Client Portal Gateway.  Key differences from Alpaca that
this adapter absorbs:

- Instruments are addressed by numeric ``conid`` (resolved from ticker via
  ``/iserver/secdef/search`` and cached).
- Order submission returns "questions" that must be confirmed with
  ``POST /iserver/reply/{id}``; we pre-suppress known message ids and loop
  through any remaining replies.
- Native protective stops are attached via bracket (parent/child) orders.
- Historical bars come from ``/iserver/marketdata/history``; snapshots from
  ``/iserver/marketdata/snapshot`` (which needs a pre-flight call).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import datetime, timezone

from trader.brokers._ibkr_session import IBKRSession
from trader.brokers.base import (
    BrokerAuthError,
    BrokerCapabilities,
    BrokerClient,
    InstrumentIdKind,
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

# Neutral TimeFrame → IBKR (bar size, period) for /marketdata/history.
_TF_TO_IBKR: dict[TimeFrame, tuple[str, str]] = {
    TimeFrame.MIN_1: ("1min", "2d"),
    TimeFrame.MIN_5: ("5min", "5d"),
    TimeFrame.MIN_15: ("15min", "10d"),
    TimeFrame.MIN_30: ("30min", "20d"),
    TimeFrame.HOUR_1: ("1h", "1m"),
    TimeFrame.DAY: ("1d", "1y"),
    TimeFrame.WEEK: ("1w", "2y"),
}

# Snapshot market-data field ids: 31=last, 84=bid, 86=ask, 88=bid size,
# 85=ask size, 70=high, 71=low, 7295=open, 7296=prior close, 87=volume.
_SNAP_FIELDS = ["31", "84", "85", "86", "88", "70", "71", "7295", "7296", "87"]


def _map_order_status(raw: str) -> OrderStatus:
    s = str(raw or "").lower()
    mapping = {
        "presubmitted": OrderStatus.PENDING,
        "pendingsubmit": OrderStatus.PENDING,
        "submitted": OrderStatus.NEW,
        "filled": OrderStatus.FILLED,
        "cancelled": OrderStatus.CANCELED,
        "pendingcancel": OrderStatus.CANCELED,
        "inactive": OrderStatus.REJECTED,
        "rejected": OrderStatus.REJECTED,
    }
    return mapping.get(s, OrderStatus.UNKNOWN)


class IBKRBroker(BrokerClient):
    """Interactive Brokers implementation of the neutral broker interface."""

    def __init__(self, cfg) -> None:
        ib = cfg.ibkr
        self._cfg = cfg
        self._account_id = ib.account_id
        self._suppress_ids = list(ib.suppress_message_ids or [])
        self.session = IBKRSession(
            base_url=ib.base_url,
            verify_ssl=ib.verify_ssl,
            keepalive_interval=ib.keepalive_interval,
            rate_per_sec=ib.rate_per_sec,
        )
        self._conid_cache: dict[str, str] = {}
        self._conid_lock = threading.Lock()
        self._caps = BrokerCapabilities(
            name="ibkr",
            id_kind=InstrumentIdKind.CONID,
            supports_streaming=True,
            supports_bar_history=True,
            supports_snapshots=True,
            supports_native_stops=True,
            supports_bracket_orders=True,
            supports_short=True,
            supports_fractional=False,
            supports_crypto=False,
            requires_session_keepalive=True,
        )

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._caps

    # ── Lifecycle ────────────────────────────────────────────
    def connect(self) -> None:
        try:
            self.session.connect()
        except RuntimeError as exc:
            raise BrokerAuthError(str(exc)) from exc
        # Resolve the account if not explicitly configured.
        if not self._account_id:
            self._account_id = self._discover_account()
        # Suppress known order-confirmation dialogs up front.
        if self._suppress_ids:
            try:
                self.session.post(
                    "/iserver/questions/suppress",
                    json={"messageIds": self._suppress_ids},
                )
            except Exception:
                logger.warning("Failed to suppress IBKR message ids", exc_info=True)
        logger.info("IBKR broker connected (account=%s)", self._account_id)

    def close(self) -> None:
        self.session.close()

    def _discover_account(self) -> str:
        # /portfolio/accounts must be called before other portfolio endpoints.
        accounts = self.session.get("/portfolio/accounts") or []
        if not accounts:
            raise BrokerAuthError("No IBKR accounts returned by /portfolio/accounts")
        acct = accounts[0]
        return str(acct.get("accountId") or acct.get("id") or acct.get("acctId"))

    # ── Account & positions ──────────────────────────────────
    def get_account(self) -> Account:
        summary = self.session.get(f"/portfolio/{self._account_id}/summary") or {}

        def _val(key: str, default: float = 0.0) -> float:
            node = summary.get(key)
            if isinstance(node, dict):
                return float(node.get("amount", node.get("value", default)) or default)
            try:
                return float(node)
            except (TypeError, ValueError):
                return default

        equity = _val("equitywithloanvalue") or _val("netliquidation")
        return Account(
            account_id=self._account_id,
            equity=equity,
            cash=_val("totalcashvalue"),
            buying_power=_val("buyingpower"),
            currency="USD",
            unrealized_pl=_val("unrealizedpnl"),
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        page = 0
        while True:
            batch = self.session.get(f"/portfolio/{self._account_id}/positions/{page}") or []
            if not batch:
                break
            for p in batch:
                qty = float(p.get("position", 0) or 0)
                if qty == 0:
                    continue
                avg = float(p.get("avgCost", p.get("avgPrice", 0)) or 0)
                mkt_price = float(p.get("mktPrice", 0) or 0)
                mkt_value = float(p.get("mktValue", qty * mkt_price) or 0)
                upl = float(p.get("unrealizedPnl", 0) or 0)
                cost_basis = abs(avg * qty) or 1.0
                out.append(
                    Position(
                        symbol=str(p.get("ticker") or p.get("contractDesc") or p.get("conid")),
                        qty=qty,
                        avg_entry_price=avg,
                        current_price=mkt_price,
                        market_value=mkt_value,
                        unrealized_pl=upl,
                        unrealized_pl_pct=(upl / cost_basis) * 100,
                        asset_class=AssetClass.US_EQUITY,
                    )
                )
            page += 1
            if len(batch) < 30:  # IBKR pages at 30 positions
                break
        return out

    # ── Instruments / conid resolution ───────────────────────
    def resolve_instrument(self, symbol: str) -> Instrument | None:
        conid = self._resolve_conid(symbol)
        if conid is None:
            return None
        return Instrument(
            symbol=symbol,
            broker_id=conid,
            asset_class=AssetClass.US_EQUITY,
            tradable=True,
            fractionable=False,
        )

    def _resolve_conid(self, symbol: str) -> str | None:
        with self._conid_lock:
            if symbol in self._conid_cache:
                return self._conid_cache[symbol]
        try:
            results = self.session.post(
                "/iserver/secdef/search", json={"symbol": symbol, "secType": "STK"}
            ) or []
        except Exception:
            logger.warning("conid search failed for %s", symbol, exc_info=True)
            return None
        conid = None
        for r in results:
            if str(r.get("symbol", "")).upper() == symbol.upper() and r.get("conid"):
                conid = str(r["conid"])
                break
        if conid is None and results and results[0].get("conid"):
            conid = str(results[0]["conid"])
        if conid is not None:
            with self._conid_lock:
                self._conid_cache[symbol] = conid
        return conid

    def list_tradable_equities(self) -> list[Instrument]:
        # IBKR has no cheap "all tradable US equities" endpoint. Callers should
        # supply an explicit universe (config.universe) for IBKR. Return empty so
        # the universe builder falls back to configured symbols/ETFs.
        logger.info("IBKR: list_tradable_equities not supported; use configured universe")
        return []

    # ── Market data ──────────────────────────────────────────
    def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: TimeFrame,
        start: datetime,
        end: datetime | None = None,
    ) -> dict[str, list[Bar]]:
        bar_size, default_period = _TF_TO_IBKR.get(timeframe, ("1d", "1y"))
        period = self._period_from_range(start, end, default_period)
        result: dict[str, list[Bar]] = {}
        for sym in symbols:
            conid = self._resolve_conid(sym)
            if conid is None:
                continue
            try:
                data = self.session.get(
                    "/iserver/marketdata/history",
                    params={"conid": conid, "bar": bar_size, "period": period, "outsideRth": "false"},
                ) or {}
            except Exception:
                logger.warning("history fetch failed for %s", sym, exc_info=True)
                continue
            bars: list[Bar] = []
            for row in data.get("data", []):
                ts = datetime.fromtimestamp(row["t"] / 1000, tz=timezone.utc)
                bars.append(
                    Bar(
                        symbol=sym,
                        timestamp=ts,
                        open=float(row["o"]),
                        high=float(row["h"]),
                        low=float(row["l"]),
                        close=float(row["c"]),
                        volume=float(row.get("v", 0) or 0),
                    )
                )
            if bars:
                result[sym] = bars
        return result

    @staticmethod
    def _period_from_range(start: datetime, end: datetime | None, default: str) -> str:
        end = end or datetime.now(tz=start.tzinfo)
        days = max(1, (end - start).days)
        if days <= 1:
            return "1d"
        if days <= 40:
            return f"{days}d"
        if days <= 400:
            return f"{max(1, days // 30)}m"
        return f"{max(1, days // 365)}y"

    def get_snapshots(self, symbols: Sequence[str]) -> dict[str, Snapshot]:
        conids = {s: self._resolve_conid(s) for s in symbols}
        valid = {s: c for s, c in conids.items() if c}
        if not valid:
            return {}
        conid_csv = ",".join(valid.values())
        params = {"conids": conid_csv, "fields": ",".join(_SNAP_FIELDS)}
        # IBKR requires a pre-flight snapshot call before data is populated.
        try:
            self.session.get("/iserver/marketdata/snapshot", params=params)
            rows = self.session.get("/iserver/marketdata/snapshot", params=params) or []
        except Exception:
            logger.warning("snapshot fetch failed", exc_info=True)
            return {}

        by_conid = {str(r.get("conid")): r for r in rows}
        out: dict[str, Snapshot] = {}
        for sym, conid in valid.items():
            r = by_conid.get(str(conid), {})

            def _f(field: str) -> float | None:
                v = r.get(field)
                if v is None:
                    return None
                try:
                    return float(str(v).lstrip("C").replace(",", ""))
                except (TypeError, ValueError):
                    return None

            bid, ask = _f("84"), _f("86")
            quote = None
            if bid is not None or ask is not None:
                quote = Quote(
                    symbol=sym,
                    bid_price=bid or 0.0,
                    ask_price=ask or 0.0,
                    bid_size=_f("88") or 0.0,
                    ask_size=_f("85") or 0.0,
                )
            out[sym] = Snapshot(
                symbol=sym,
                latest_price=_f("31"),
                daily_open=_f("7295"),
                daily_high=_f("70"),
                daily_low=_f("71"),
                daily_close=_f("31"),
                daily_volume=_f("87"),
                prev_daily_close=_f("7296"),
                quote=quote,
            )
        return out

    def get_latest_quotes(self, symbols: Sequence[str]) -> dict[str, Quote]:
        snaps = self.get_snapshots(symbols)
        return {s: snap.quote for s, snap in snaps.items() if snap.quote is not None}

    # ── Orders ───────────────────────────────────────────────
    def submit_order(self, request: OrderRequest) -> OrderResult:
        conid = self._resolve_conid(request.symbol)
        if conid is None:
            return OrderResult(
                order_id="", status=OrderStatus.REJECTED,
                symbol=request.symbol, submitted_qty=request.qty,
            )

        parent = self._build_order_payload(request, conid, request.qty)

        if request.attached_stop_price is not None:
            # Native bracket: parent entry + child stop, linked by cOID.
            coid = request.client_order_id or f"{request.symbol}-{int(datetime.now().timestamp())}"
            parent["cOID"] = coid
            child = {
                "conid": int(conid),
                "orderType": "STP",
                "side": "SELL" if request.side == Side.BUY else "BUY",
                "price": round(request.attached_stop_price, 2),
                "quantity": request.qty,
                "tif": self._tif(request.time_in_force),
                "parentId": coid,
            }
            payload = {"orders": [parent, child]}
        else:
            payload = {"orders": [parent]}

        replies = self.session.post(
            f"/iserver/account/{self._account_id}/orders", json=payload
        )
        order_ids = self._confirm_replies(replies)
        if not order_ids:
            return OrderResult(
                order_id="", status=OrderStatus.REJECTED,
                symbol=request.symbol, submitted_qty=request.qty,
            )
        return OrderResult(
            order_id=order_ids[0],
            status=OrderStatus.NEW,
            symbol=request.symbol,
            submitted_qty=request.qty,
            child_order_ids=tuple(order_ids[1:]),
        )

    def submit_stop_order(
        self, symbol: str, side, qty: float, stop_price: float, time_in_force
    ) -> OrderResult:
        conid = self._resolve_conid(symbol)
        if conid is None:
            return OrderResult(
                order_id="", status=OrderStatus.REJECTED, symbol=symbol, submitted_qty=qty
            )
        nside = side if isinstance(side, Side) else Side(side)
        ntif = time_in_force if isinstance(time_in_force, TimeInForce) else TimeInForce(time_in_force)
        payload = {
            "orders": [
                {
                    "conid": int(conid),
                    "orderType": "STP",
                    "side": nside.value.upper(),
                    "price": round(stop_price, 2),
                    "quantity": qty,
                    "tif": self._tif(ntif),
                }
            ]
        }
        replies = self.session.post(f"/iserver/account/{self._account_id}/orders", json=payload)
        order_ids = self._confirm_replies(replies)
        return OrderResult(
            order_id=order_ids[0] if order_ids else "",
            status=OrderStatus.NEW if order_ids else OrderStatus.REJECTED,
            symbol=symbol,
            submitted_qty=qty,
        )

    def _build_order_payload(self, request: OrderRequest, conid: str, qty: float) -> dict:
        order_type_map = {
            OrderType.MARKET: "MKT",
            OrderType.LIMIT: "LMT",
            OrderType.STOP: "STP",
            OrderType.STOP_LIMIT: "STP_LIMIT",
        }
        payload: dict = {
            "conid": int(conid),
            "orderType": order_type_map.get(request.order_type, "LMT"),
            "side": request.side.value.upper(),
            "quantity": qty,
            "tif": self._tif(request.time_in_force),
        }
        if request.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT) and request.limit_price:
            payload["price"] = round(request.limit_price, 2)
        if request.order_type in (OrderType.STOP, OrderType.STOP_LIMIT) and request.stop_price:
            payload["auxPrice"] = round(request.stop_price, 2)
        return payload

    @staticmethod
    def _tif(tif: TimeInForce) -> str:
        return {
            TimeInForce.DAY: "DAY",
            TimeInForce.GTC: "GTC",
            TimeInForce.IOC: "IOC",
            TimeInForce.FOK: "FOK",
            TimeInForce.OPG: "OPG",
        }.get(tif, "DAY")

    def _confirm_replies(self, replies) -> list[str]:
        """Walk the order-reply chain, confirming any questions, collect order ids."""
        order_ids: list[str] = []
        seen_reply_ids: set[str] = set()
        max_loops = 10
        while replies and max_loops > 0:
            max_loops -= 1
            if isinstance(replies, dict):
                if replies.get("error"):
                    logger.error("IBKR order error: %s", replies.get("error"))
                    return order_ids
                replies = [replies]
            next_replies = None
            for item in replies:
                if not isinstance(item, dict):
                    continue
                if item.get("order_id"):
                    order_ids.append(str(item["order_id"]))
                    continue
                reply_id = item.get("id") or item.get("messageIds")
                if reply_id and str(reply_id) not in seen_reply_ids:
                    seen_reply_ids.add(str(reply_id))
                    next_replies = self.session.post(
                        f"/iserver/reply/{reply_id}", json={"confirmed": True}
                    )
            if next_replies is None:
                break
            replies = next_replies
        return order_ids

    def cancel_all_orders(self) -> None:
        for o in self.get_open_orders():
            try:
                self.session.delete(f"/iserver/account/{self._account_id}/order/{o.order_id}")
            except Exception:
                logger.warning("Failed to cancel IBKR order %s", o.order_id, exc_info=True)

    def close_all_positions(self) -> None:
        for pos in self.get_positions():
            side = Side.SELL if pos.qty > 0 else Side.BUY
            req = OrderRequest(
                symbol=pos.symbol,
                side=side,
                qty=pos.abs_qty,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            try:
                self.submit_order(req)
            except Exception:
                logger.warning("Failed to flatten IBKR position %s", pos.symbol, exc_info=True)

    def get_open_orders(self) -> list[Order]:
        data = self.session.get("/iserver/account/orders") or {}
        orders_raw = data.get("orders", data) if isinstance(data, dict) else data
        out: list[Order] = []
        for o in orders_raw or []:
            status = _map_order_status(o.get("status"))
            if status in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED):
                continue
            side_raw = str(o.get("side", "")).upper()
            out.append(
                Order(
                    order_id=str(o.get("orderId") or o.get("order_id") or ""),
                    symbol=str(o.get("ticker") or o.get("symbol") or ""),
                    side=Side.BUY if side_raw.startswith("B") else Side.SELL,
                    qty=float(o.get("totalSize", o.get("remainingQuantity", 0)) or 0),
                    order_type=OrderType.LIMIT,
                    status=status,
                    limit_price=float(o["price"]) if o.get("price") else None,
                )
            )
        return out
