"""Persistent execution state — orders, fills, and equity snapshots.

Uses a single SQLite database so state survives crashes/restarts.  This
enables (a) crash recovery / reconciliation on startup and (b) the web
dashboard's performance metrics (equity curve, transaction count).

The store is intentionally dependency-light (stdlib ``sqlite3``) and
thread-safe via a per-connection lock.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class OrderRecord:
    order_id: str
    symbol: str
    side: str
    qty: float
    limit_price: float
    stop_price: float | None
    strategy: str
    reason: str
    status: str = "submitted"
    child_order_ids: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class StateStore:
    """SQLite-backed persistence for orders and equity snapshots."""

    def __init__(self, db_path: str | Path = "data/trader_state.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    limit_price REAL,
                    stop_price REAL,
                    strategy TEXT,
                    reason TEXT,
                    status TEXT,
                    child_order_ids TEXT,
                    created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    ts TEXT PRIMARY KEY,
                    equity REAL NOT NULL,
                    cash REAL,
                    unrealized_pl REAL
                );
                CREATE TABLE IF NOT EXISTS crypto_stops (
                    symbol TEXT PRIMARY KEY,
                    stop_price REAL NOT NULL,
                    entry_price REAL,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS positions (
                    symbol TEXT PRIMARY KEY,
                    qty REAL NOT NULL,
                    avg_entry_price REAL,
                    current_price REAL,
                    market_value REAL,
                    unrealized_pl REAL,
                    unrealized_pl_pct REAL,
                    updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
                """
            )
            self._conn.commit()

    # ── Orders ───────────────────────────────────────────────
    def record_order(self, rec: OrderRecord) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO orders
                (order_id, symbol, side, qty, limit_price, stop_price, strategy,
                 reason, status, child_order_ids, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.order_id,
                    rec.symbol,
                    rec.side,
                    rec.qty,
                    rec.limit_price,
                    rec.stop_price,
                    rec.strategy,
                    rec.reason,
                    rec.status,
                    json.dumps(rec.child_order_ids),
                    rec.created_at,
                ),
            )
            self._conn.commit()

    def update_order_status(self, order_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE orders SET status=? WHERE order_id=?", (status, order_id)
            )
            self._conn.commit()

    def orders_today(self) -> list[dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders WHERE created_at >= ? ORDER BY created_at",
                (today,),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def all_orders(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def non_terminal_orders(self, limit: int = 500) -> list[dict]:
        """Return orders whose stored status is not a terminal state.

        Terminal = filled / canceled / cancelled / rejected / expired. These
        are the orders worth re-checking against the broker so the dashboard
        reflects fills instead of a stale submit-time status.
        """
        terminal = ("filled", "canceled", "cancelled", "rejected", "expired")
        placeholders = ",".join("?" for _ in terminal)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM orders WHERE LOWER(COALESCE(status,'')) "
                f"NOT IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
                (*terminal, limit),
            ).fetchall()
        return [self._row_to_order(r) for r in rows]

    def get_order(self, order_id: str) -> dict | None:
        """Return a single stored order by id, or None if not present."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE order_id=?", (order_id,)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def update_order_fill(
        self, order_id: str, fill_price: float, status: str | None = None
    ) -> None:
        """Correct an order's recorded price (and optionally status) from a fill.

        Orders are recorded with ``limit_price`` as a fill-price proxy; once the
        broker reports the real ``filled_avg_price`` we overwrite it so P/L is
        exact. Only updates when the price actually differs.
        """
        with self._lock:
            if status is not None:
                self._conn.execute(
                    "UPDATE orders SET limit_price=?, status=? WHERE order_id=?",
                    (float(fill_price), status, order_id),
                )
            else:
                self._conn.execute(
                    "UPDATE orders SET limit_price=? WHERE order_id=?",
                    (float(fill_price), order_id),
                )
            self._conn.commit()

    @staticmethod
    def _row_to_order(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["child_order_ids"] = json.loads(d.get("child_order_ids") or "[]")
        return d

    # ── Equity snapshots ─────────────────────────────────────
    def record_equity(self, equity: float, cash: float = 0.0, unrealized_pl: float = 0.0) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO equity_snapshots (ts, equity, cash, unrealized_pl) "
                "VALUES (?,?,?,?)",
                (ts, equity, cash, unrealized_pl),
            )
            self._conn.commit()

    def equity_series(self, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, equity, cash FROM equity_snapshots ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def latest_equity(self) -> dict | None:
        """Return the most recent full equity snapshot (equity, cash, P&L, ts)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, equity, cash, unrealized_pl FROM equity_snapshots "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    # ── Crypto software stops ────────────────────────────────
    def set_crypto_stop(self, symbol: str, stop_price: float, entry_price: float = 0.0) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO crypto_stops (symbol, stop_price, entry_price, updated_at) "
                "VALUES (?,?,?,?)",
                (symbol, float(stop_price), float(entry_price), ts),
            )
            self._conn.commit()

    def get_crypto_stops(self) -> dict[str, dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, stop_price, entry_price, updated_at FROM crypto_stops"
            ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def delete_crypto_stop(self, symbol: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM crypto_stops WHERE symbol=?", (symbol,))
            self._conn.commit()

    # ── Position snapshots ───────────────────────────────────
    def replace_positions(self, positions: list[dict]) -> None:
        """Replace the stored open-position snapshot with the given rows.

        Each dict needs: symbol, qty, avg_entry_price, current_price,
        market_value, unrealized_pl, unrealized_pl_pct.
        """
        ts = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute("DELETE FROM positions")
            self._conn.executemany(
                "INSERT OR REPLACE INTO positions "
                "(symbol, qty, avg_entry_price, current_price, market_value, "
                "unrealized_pl, unrealized_pl_pct, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                [
                    (
                        p.get("symbol"),
                        float(p.get("qty") or 0.0),
                        float(p.get("avg_entry_price") or 0.0),
                        float(p.get("current_price") or 0.0),
                        float(p.get("market_value") or 0.0),
                        float(p.get("unrealized_pl") or 0.0),
                        float(p.get("unrealized_pl_pct") or 0.0),
                        ts,
                    )
                    for p in positions
                ],
            )
            self._conn.commit()

    def latest_positions(self) -> dict[str, dict]:
        """Return the latest stored open positions keyed by symbol."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, qty, avg_entry_price, current_price, market_value, "
                "unrealized_pl, unrealized_pl_pct, updated_at FROM positions"
            ).fetchall()
        return {r["symbol"]: dict(r) for r in rows}

    def reset_dashboard_data(self) -> dict[str, int]:
        """Clear everything the dashboard displays: equity history, orders and
        the positions snapshot.

        This wipes the graph (``equity_snapshots``), the recent-transactions
        table + realized-P/L / win-rate numbers (``orders``) and the cached
        positions used for per-row unrealized P/L (``positions``).

        Deliberately does NOT touch ``crypto_stops`` — those are live safety
        data for open positions; clearing them would leave real positions
        unprotected by the software stop loop. Returns the row counts removed.
        """
        with self._lock:
            removed = {
                "orders": self._conn.execute(
                    "SELECT COUNT(*) FROM orders"
                ).fetchone()[0],
                "equity_snapshots": self._conn.execute(
                    "SELECT COUNT(*) FROM equity_snapshots"
                ).fetchone()[0],
                "positions": self._conn.execute(
                    "SELECT COUNT(*) FROM positions"
                ).fetchone()[0],
            }
            self._conn.execute("DELETE FROM orders")
            self._conn.execute("DELETE FROM equity_snapshots")
            self._conn.execute("DELETE FROM positions")
            self._conn.commit()
        return removed

    def close(self) -> None:
        with self._lock:
            self._conn.close()
