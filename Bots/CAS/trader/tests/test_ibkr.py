"""Tests for the IBKR adapter's HTTP translation (conid, order-reply chain).

These use a fake in-memory session so no gateway/network is needed.  We verify
the conid resolution + cache, the bracket order payload, and that the
order-reply confirmation dance collects the resulting order ids.
"""

from __future__ import annotations

from trader.brokers.ibkr import IBKRBroker
from trader.brokers.types import OrderRequest, OrderStatus, Side, TimeInForce
from trader.config import Config


class FakeSession:
    """Records calls and replays canned responses keyed by (method, path prefix)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.reply_confirmed = False

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass

    def get(self, path: str, params: dict | None = None):
        self.calls.append(("GET", path, params or {}))
        if path == "/portfolio/accounts":
            return [{"accountId": "DU123"}]
        return {}

    def post(self, path: str, json: dict | None = None):
        self.calls.append(("POST", path, json or {}))
        if path == "/iserver/secdef/search":
            sym = (json or {}).get("symbol", "")
            return [{"symbol": sym, "conid": 265598}]
        if path == "/iserver/questions/suppress":
            return {"status": "submitted"}
        if path.endswith("/orders"):
            # First response is a "question" that must be confirmed.
            return [{"id": "reply-1", "message": ["Confirm order?"]}]
        if path.startswith("/iserver/reply/"):
            self.reply_confirmed = True
            return [{"order_id": "O-1001"}, {"order_id": "O-1001-stop"}]
        return {}

    def delete(self, path: str):
        self.calls.append(("DELETE", path, {}))
        return {}


def _broker() -> tuple[IBKRBroker, FakeSession]:
    cfg = Config()
    cfg.broker.provider = "ibkr"
    broker = IBKRBroker(cfg)
    fake = FakeSession()
    broker.session = fake  # inject fake transport
    return broker, fake


def test_conid_resolution_and_cache():
    broker, fake = _broker()
    inst = broker.resolve_instrument("AAPL")
    assert inst is not None
    assert inst.broker_id == "265598"
    # Second call should hit the cache (no extra search POST).
    searches_before = sum(1 for c in fake.calls if c[1] == "/iserver/secdef/search")
    broker.resolve_instrument("AAPL")
    searches_after = sum(1 for c in fake.calls if c[1] == "/iserver/secdef/search")
    assert searches_after == searches_before


def test_account_discovery_on_connect():
    broker, fake = _broker()
    broker.connect()
    assert broker._account_id == "DU123"


def test_bracket_order_confirm_chain_collects_ids():
    broker, fake = _broker()
    broker._account_id = "DU123"
    req = OrderRequest(
        symbol="AAPL",
        side=Side.BUY,
        qty=10,
        limit_price=150.0,
        attached_stop_price=145.0,
        time_in_force=TimeInForce.DAY,
    )
    result = broker.submit_order(req)
    assert fake.reply_confirmed is True  # reply dance happened
    assert result.status is OrderStatus.NEW
    assert result.order_id == "O-1001"
    assert result.child_order_ids == ("O-1001-stop",)

    # The submitted payload must contain a parent + linked child stop.
    order_call = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/orders"))
    payload = order_call[2]
    assert len(payload["orders"]) == 2
    parent, child = payload["orders"]
    assert "cOID" in parent
    assert child["orderType"] == "STP"
    assert child["parentId"] == parent["cOID"]
    assert child["side"] == "SELL"  # opposite of BUY entry


def test_capabilities_conid_kind():
    broker, _ = _broker()
    caps = broker.capabilities
    assert caps.name == "ibkr"
    assert caps.supports_fractional is False
    assert caps.requires_session_keepalive is True
