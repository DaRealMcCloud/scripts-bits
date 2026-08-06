"""FastAPI dashboard: status, performance metrics, and lifecycle controls.

Endpoints
---------
- ``GET  /``             → single-page Chart.js dashboard (served from static/).
- ``GET  /api/status``   → supervisor status (running, pid, uptime, heartbeat…).
- ``GET  /api/metrics``  → equity curve + derived performance stats + positions.
- ``POST /api/restart``  → ask the supervisor to restart the trader child.
- ``POST /api/stop``     → ask the supervisor to stop the trader child.

All heavy imports (fastapi/uvicorn) happen lazily inside ``run_web`` so that a
bot without the ``web`` extra installed still runs — the supervisor degrades
gracefully when this module cannot be imported.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trader.execution.state_store import StateStore

if TYPE_CHECKING:  # avoid runtime import cycles / optional deps
    from trader.config import Config
    from trader.supervisor import Supervisor

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).with_name("static")


# ── Metric helpers ───────────────────────────────────────────

def _read_status_file() -> dict[str, Any]:
    from trader.supervisor import STATUS_FILE  # local import: optional context

    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "pid": None, "note": "status file unavailable"}


# Annualising a track record shorter than this just extrapolates noise into
# absurd numbers (a +0.5% day becomes several-thousand-percent "p.a."), so we
# withhold the figure until there's a meaningful sample.
_MIN_ANNUALISE_DAYS = 14.0


def _annualised_return(start: float, end: float, days: float) -> float | None:
    """Compound annual growth rate (CAGR) as a percentage.

    Returns ``None`` for track records shorter than ``_MIN_ANNUALISE_DAYS``:
    extrapolating a few hours/days out to a full year is meaningless and
    produces wildly misleading values, so we show "–" until we have enough data.
    """
    if start <= 0 or end <= 0 or days < _MIN_ANNUALISE_DAYS:
        return None
    years = days / 365.25
    if years <= 0:
        return None
    try:
        return (math.pow(end / start, 1.0 / years) - 1.0) * 100.0
    except (ValueError, OverflowError):
        return None


def _annotate_realized_pl(
    orders: list[dict[str, Any]],
    positions: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Return the given orders (newest-first) annotated with P/L.

    Orders don't store a fill price, so we use ``limit_price`` as the trade price
    and track a running average cost-basis per symbol. For each SELL we compute
    the realized profit/loss against the average cost of the shares held at that
    point:  realized_pl = (sell_price - avg_cost) * qty_sold.

    For each BUY we track how much of the lot is still held (FIFO) and, using the
    latest position snapshot's ``current_price``, expose an ``unrealized_pl`` for
    the portion still open:  unrealized_pl = (current_price - buy_price) * open_qty.

    Sells with no prior recorded buy (unknown basis) return ``realized_pl=None``.
    Buys that are fully closed (or have no price snapshot) return
    ``unrealized_pl=None``.
    """
    positions = positions or {}
    # Walk oldest -> newest so the cost basis is built up in chronological order.
    chronological = list(reversed(orders))

    # Per-symbol running position: total qty held and its aggregate cost.
    running: dict[str, dict[str, float]] = {}
    # Per-symbol FIFO queue of open buy lots (each references its order dict).
    open_lots: dict[str, list[dict[str, Any]]] = {}

    for o in chronological:
        symbol = o.get("symbol")
        side = (o.get("side") or "").lower()
        qty = float(o.get("qty") or 0.0)
        price = o.get("limit_price")
        price = float(price) if price is not None else None

        pos = running.setdefault(symbol, {"qty": 0.0, "cost": 0.0})

        if side == "buy":
            o["realized_pl"] = None
            if price is not None:
                pos["qty"] += qty
                pos["cost"] += qty * price
                # Track this buy as an open lot for FIFO matching against sells.
                lot = {"order": o, "buy_price": price, "open_qty": qty}
                open_lots.setdefault(symbol, []).append(lot)
        elif side == "sell":
            avg_cost = (pos["cost"] / pos["qty"]) if pos["qty"] > 0 else None
            if price is not None and avg_cost is not None:
                sell_qty = min(qty, pos["qty"])
                o["realized_pl"] = (price - avg_cost) * sell_qty
                o["realized_pl_pct"] = (
                    (price / avg_cost - 1.0) * 100.0 if avg_cost > 0 else None
                )
                o["avg_cost"] = avg_cost
                # Reduce the position by the shares sold, keeping avg cost constant.
                pos["cost"] -= avg_cost * sell_qty
                pos["qty"] -= sell_qty
                # Consume open buy lots FIFO so remaining lots reflect what's held.
                remaining = sell_qty
                lots = open_lots.get(symbol, [])
                while remaining > 0 and lots:
                    lot = lots[0]
                    take = min(lot["open_qty"], remaining)
                    lot["open_qty"] -= take
                    remaining -= take
                    if lot["open_qty"] <= 1e-12:
                        lots.pop(0)
            else:
                o["realized_pl"] = None
        else:
            o["realized_pl"] = None

    # Now value the still-open buy lots at the latest known market price.
    for symbol, lots in open_lots.items():
        snap = positions.get(symbol)
        current_price = None
        if snap is not None:
            try:
                current_price = float(snap.get("current_price") or 0.0) or None
            except (TypeError, ValueError):
                current_price = None
        for lot in lots:
            order = lot["order"]
            open_qty = lot["open_qty"]
            buy_price = lot["buy_price"]
            if open_qty > 1e-12 and current_price and buy_price:
                order["unrealized_pl"] = (current_price - buy_price) * open_qty
                order["unrealized_pl_pct"] = (current_price / buy_price - 1.0) * 100.0
                order["open_qty"] = open_qty
                order["current_price"] = current_price

    return orders



def _compute_metrics(store: StateStore) -> dict[str, Any]:
    series = store.equity_series(limit=5000)
    orders = store.all_orders(limit=1000)

    points = [
        {"t": row["ts"], "v": row["equity"], "cash": row.get("cash")}
        for row in series
    ]
    total_return_pct: float | None = None
    return_pa_pct: float | None = None
    if len(series) >= 2:
        start_v = series[0]["equity"]
        end_v = series[-1]["equity"]
        if start_v > 0:
            total_return_pct = (end_v / start_v - 1.0) * 100.0
        try:
            t0 = datetime.fromisoformat(series[0]["ts"])
            t1 = datetime.fromisoformat(series[-1]["ts"])
            days = max((t1 - t0).total_seconds() / 86400.0, 0.0)
            return_pa_pct = _annualised_return(start_v, end_v, days)
        except Exception:
            return_pa_pct = None

    # Annotate ALL known orders with realized P/L (needs full history for a
    # correct running cost-basis), then expose only the most recent 100.
    positions = store.latest_positions()
    _annotate_realized_pl(orders, positions)
    recent_transactions = orders[:100]

    # Aggregate win/loss across every closed (sell) trade with known basis.
    realized_total = 0.0
    realized_cost = 0.0
    wins = 0
    losses = 0
    for o in orders:
        pl = o.get("realized_pl")
        if pl is None:
            continue
        realized_total += pl
        # Reconstruct the cost of the sold shares to get an overall % return.
        avg_cost = o.get("avg_cost")
        qty = float(o.get("qty") or 0.0)
        if avg_cost is not None and qty > 0:
            realized_cost += avg_cost * qty
        if pl > 0:
            wins += 1
        elif pl < 0:
            losses += 1
    closed_trades = wins + losses
    realized_pl_pct = (
        (realized_total / realized_cost) * 100.0 if realized_cost > 0 else None
    )
    win_rate_pct = (wins / closed_trades * 100.0) if closed_trades > 0 else None

    last_txn = orders[0] if orders else None

    latest = store.latest_equity()

    return {
        "portfolio_value": series[-1]["equity"] if series else None,
        "current_equity": (latest or {}).get("equity"),
        "current_cash": (latest or {}).get("cash"),
        "current_unrealized_pl": (latest or {}).get("unrealized_pl"),
        "equity_as_of": (latest or {}).get("ts"),
        "equity_series": points,
        "total_return_pct": total_return_pct,
        "return_pa_pct": return_pa_pct,
        "num_transactions": len(orders),
        "realized_pl_total": realized_total if closed_trades > 0 else None,
        "realized_pl_pct": realized_pl_pct,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate_pct,
        "last_transaction": last_txn,
        "recent_transactions": recent_transactions,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── App factory ──────────────────────────────────────────────

def create_app(cfg: "Config", supervisor: "Supervisor | None" = None):
    """Build the FastAPI application (imports fastapi lazily)."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles

    store = StateStore()
    app = FastAPI(title="Trading Bot Dashboard", docs_url=None, redoc_url=None)

    @app.get("/api/status")
    def api_status() -> JSONResponse:
        status = _read_status_file()
        status.setdefault("broker_provider", getattr(cfg.broker, "provider", "unknown"))
        return JSONResponse(status)

    @app.get("/api/metrics")
    def api_metrics() -> JSONResponse:
        return JSONResponse(_compute_metrics(store))

    @app.post("/api/restart")
    def api_restart() -> JSONResponse:
        if supervisor is None:
            return JSONResponse({"ok": False, "error": "no supervisor"}, status_code=409)
        supervisor.request_restart()
        return JSONResponse({"ok": True, "action": "restart"})

    @app.post("/api/stop")
    def api_stop() -> JSONResponse:
        if supervisor is None:
            return JSONResponse({"ok": False, "error": "no supervisor"}, status_code=409)
        supervisor.request_stop()
        return JSONResponse({"ok": True, "action": "stop"})

    @app.post("/api/reset")
    def api_reset() -> JSONResponse:
        """Clear the equity graph, headline numbers and recent transactions.

        Wipes the equity_snapshots, orders and positions tables via the store.
        Live crypto stops are intentionally preserved. Fresh data repopulates
        on the next heartbeat/equity snapshot.
        """
        try:
            removed = store.reset_dashboard_data()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Dashboard reset failed", exc_info=True)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        logger.info("Dashboard data reset via API: %s", removed)
        return JSONResponse({"ok": True, "action": "reset", "removed": removed})

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        index_path = _STATIC_DIR / "index.html"
        try:
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        except Exception:
            return HTMLResponse("<h1>Dashboard asset missing</h1>", status_code=500)

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


def run_web(cfg: "Config", supervisor: "Supervisor | None" = None) -> None:
    """Run the dashboard with uvicorn (blocking; call in a daemon thread)."""
    import uvicorn

    host = getattr(cfg.web, "host", "127.0.0.1")
    port = int(getattr(cfg.web, "port", 8787))
    app = create_app(cfg, supervisor)
    # If bound to all interfaces, tell the user the loopback URL they can click.
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{browse_host}:{port}"
    logger.info("=" * 60)
    logger.info("Web dashboard is live — open it in your browser:")
    logger.info("    %s", url)
    logger.info("=" * 60)
    uvicorn.run(app, host=host, port=port, log_level="warning")
