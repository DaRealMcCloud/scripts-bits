"""Lightweight historical backtest engine.

Replays daily bars fetched from any :class:`trader.brokers.base.BrokerClient`
through the momentum strategy logic + risk sizing, simulating fills at the next
bar's open and applying ATR stops.  It is intentionally simple — a research aid
to sanity-check parameters, not a tick-accurate simulator.

The engine reuses the neutral DTOs, so it stays broker-agnostic and shares the
exact indicator/ATR code paths used in live trading.

Run with::

    python -m trader.backtest --symbols AAPL,MSFT,NVDA --days 365
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.brokers.base import BrokerClient
from trader.brokers.factory import make_broker
from trader.brokers.types import Bar, TimeFrame
from trader.config import Config, load_config
from trader.logger import setup_logging
from trader.risk.stops import compute_atr

logger = logging.getLogger(__name__)


@dataclass
class Trade:
    symbol: str
    entry_date: datetime
    entry_price: float
    exit_date: datetime | None = None
    exit_price: float | None = None
    qty: float = 0.0
    reason: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price == 0:
            return 0.0
        return (self.exit_price / self.entry_price - 1) * 100


@dataclass
class BacktestResult:
    starting_equity: float
    ending_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        if self.starting_equity == 0:
            return 0.0
        return (self.ending_equity / self.starting_equity - 1) * 100

    @property
    def num_trades(self) -> int:
        return len([t for t in self.trades if t.exit_price is not None])

    @property
    def win_rate(self) -> float:
        closed = [t for t in self.trades if t.exit_price is not None]
        if not closed:
            return 0.0
        wins = sum(1 for t in closed if t.pnl > 0)
        return wins / len(closed) * 100

    def summary(self) -> str:
        return (
            f"Backtest: start=${self.starting_equity:,.0f} end=${self.ending_equity:,.0f} "
            f"return={self.total_return_pct:+.2f}% trades={self.num_trades} "
            f"win_rate={self.win_rate:.1f}%"
        )


class Backtester:
    """Simple daily momentum backtest with ATR stops and fixed-fractional sizing."""

    def __init__(self, cfg: Config, starting_equity: float = 100_000.0) -> None:
        self.cfg = cfg
        self.starting_equity = starting_equity

    def run(
        self,
        broker: BrokerClient,
        symbols: list[str],
        start: datetime,
        end: datetime | None = None,
    ) -> BacktestResult:
        end = end or datetime.now()
        bars_by_symbol = broker.get_bars(symbols, TimeFrame.DAY, start=start, end=end)
        # Align on sorted dates per symbol.
        series = {
            s: sorted(bars, key=lambda b: b.timestamp)
            for s, bars in bars_by_symbol.items()
            if bars
        }
        if not series:
            logger.warning("No historical data for backtest")
            return BacktestResult(self.starting_equity, self.starting_equity)

        equity = self.starting_equity
        open_trades: dict[str, Trade] = {}
        result = BacktestResult(self.starting_equity, self.starting_equity)

        # Build a unified, ordered list of dates from the longest series.
        anchor = max(series.values(), key=len)
        slow = self.cfg.strategies.momentum.slow_lookback

        for idx in range(slow + 1, len(anchor) - 1):
            date = anchor[idx].timestamp
            # 1. Manage exits (ATR stop or time exit).
            for sym, trade in list(open_trades.items()):
                sym_bars = series.get(sym, [])
                today = self._bar_on(sym_bars, date)
                if today is None:
                    continue
                stop_hit = today.low <= self._stop_price(sym_bars, trade.entry_price, idx)
                held_days = (date - trade.entry_date).days
                if stop_hit or held_days >= self.cfg.risk.max_hold_days:
                    trade.exit_date = date
                    trade.exit_price = today.close
                    trade.reason = "stop" if stop_hit else "time_exit"
                    equity += trade.pnl
                    del open_trades[sym]

            # 2. Entries: rank momentum, open top-N not already held.
            ranked = self._rank_momentum(series, idx)
            for sym, score in ranked[: self.cfg.strategies.momentum.top_n]:
                if sym in open_trades or len(open_trades) >= self.cfg.risk.max_positions:
                    continue
                sym_bars = series[sym]
                next_bar = self._next_bar(sym_bars, date)
                if next_bar is None or score <= 0:
                    continue
                entry = next_bar.open
                stop = self._stop_price(sym_bars, entry, idx)
                risk_per_share = abs(entry - stop) or (entry * 0.02)
                qty = float(int((equity * self.cfg.risk.max_risk_per_trade) / risk_per_share))
                if qty < 1:
                    continue
                open_trades[sym] = Trade(
                    symbol=sym, entry_date=date, entry_price=entry, qty=qty
                )
                result.trades.append(open_trades[sym])

            # 3. Mark-to-market equity curve.
            mtm = equity + sum(
                (self._bar_on(series[s], date).close - t.entry_price) * t.qty
                for s, t in open_trades.items()
                if self._bar_on(series[s], date)
            )
            result.equity_curve.append((date, mtm))

        # Close any remaining open trades at last price.
        for sym, trade in open_trades.items():
            last = series[sym][-1]
            trade.exit_date = last.timestamp
            trade.exit_price = last.close
            trade.reason = "eod_close"
            equity += trade.pnl

        result.ending_equity = equity
        return result

    # ── helpers ──────────────────────────────────────────────
    @staticmethod
    def _bar_on(bars: list[Bar], date: datetime) -> Bar | None:
        for b in bars:
            if b.timestamp.date() == date.date():
                return b
        return None

    @staticmethod
    def _next_bar(bars: list[Bar], date: datetime) -> Bar | None:
        for i, b in enumerate(bars):
            if b.timestamp.date() == date.date() and i + 1 < len(bars):
                return bars[i + 1]
        return None

    def _stop_price(self, bars: list[Bar], entry: float, idx: int) -> float:
        window = bars[max(0, idx - 20) : idx + 1]
        atr = compute_atr(window)
        if atr <= 0:
            return entry * 0.98
        return entry - atr * self.cfg.risk.atr_stop_multiplier

    def _rank_momentum(self, series: dict[str, list[Bar]], idx: int) -> list[tuple[str, float]]:
        fast = self.cfg.strategies.momentum.fast_lookback
        slow = self.cfg.strategies.momentum.slow_lookback
        scores: list[tuple[str, float]] = []
        for sym, bars in series.items():
            if len(bars) <= idx or idx < slow:
                continue
            closes = [b.close for b in bars[: idx + 1]]
            if len(closes) < slow + 1:
                continue
            fast_ret = closes[-1] / closes[-fast] - 1
            slow_ret = closes[-1] / closes[-slow] - 1
            scores.append((sym, 0.6 * fast_ret + 0.4 * slow_ret))
        scores.sort(key=lambda kv: kv[1], reverse=True)
        return scores


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a simple momentum backtest")
    parser.add_argument("--symbols", required=True, help="Comma-separated tickers")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--equity", type=float, default=100_000.0)
    args = parser.parse_args()

    cfg = load_config("config.yaml")
    setup_logging(cfg)

    broker = make_broker(cfg)
    broker.connect()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    start = datetime.now() - timedelta(days=args.days)

    bt = Backtester(cfg, starting_equity=args.equity)
    result = bt.run(broker, symbols, start)
    print(result.summary())
    for t in result.trades:
        if t.exit_price is not None:
            print(
                f"  {t.symbol} {t.entry_date.date()} → {t.exit_date.date()} "
                f"{t.return_pct:+.1f}% ({t.reason})"
            )


if __name__ == "__main__":
    main()
