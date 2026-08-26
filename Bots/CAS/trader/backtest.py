"""Lightweight historical backtest engine.

Replays daily bars fetched from any :class:`trader.brokers.base.BrokerClient`
through a chosen strategy's ranking logic + risk sizing, simulating fills at the
next bar's open and applying ATR stops. It is intentionally simple — a research
aid to sanity-check parameters, not a tick-accurate simulator.

The engine reuses the neutral DTOs and the *same* indicator/ATR code paths used
in live trading, so it stays broker-agnostic. Several strategy families can be
replayed by swapping the ranking function (see ``STRATEGY_RANKERS``):

- ``momentum``          — equity cross-sectional momentum (fast/slow lookbacks).
- ``crypto_momentum``   — dual-MA trend with ADX filter (long-only).
- ``crypto_volatility`` — low-volatility coil in an intact uptrend.

Run with::

    python -m trader.backtest --symbols AAPL,MSFT,NVDA --months 12
    python -m trader.backtest --symbols BTC/USD,ETH/USD --strategy crypto_momentum --months 6

Bars come from the active broker adapter (Alpaca by default, using the feed in
``alpaca.data_feed``). Alpaca's free Basic plan serves daily history back to
2016 and withholds only the *latest 15 minutes* of SIP data, so multi-year
backtests need no paid subscription — note that equity coverage on the free plan
is IEX-only, so volumes are a fraction of the consolidated tape.
"""

from __future__ import annotations

import argparse
import calendar
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trader.analysis.indicators import adx, realized_vol, sma
from trader.brokers.base import BrokerClient
from trader.brokers.factory import make_broker
from trader.brokers.types import Bar, TimeFrame
from trader.config import Config, load_config
from trader.logger import setup_logging
from trader.risk.stops import compute_atr

logger = logging.getLogger(__name__)

# History replayed when neither --months nor --days is given.
DEFAULT_MONTHS = 12

# A ranking function scores the symbols in ``series`` as-of bar index ``idx``
# using only bars up to and including ``idx`` (no look-ahead). It returns
# ``(symbol, score)`` pairs sorted best-first; the engine opens the top-N with a
# positive score that are not already held.
RankFn = Callable[[dict[str, list["Bar"]], int, Config], list[tuple[str, float]]]


@dataclass
class Trade:
    symbol: str
    entry_date: datetime
    entry_price: float
    exit_date: datetime | None = None
    exit_price: float | None = None
    qty: float = 0.0
    reason: str = ""
    entry_fee: float = 0.0
    exit_fee: float = 0.0

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        gross = (self.exit_price - self.entry_price) * self.qty
        return gross - self.entry_fee - self.exit_fee

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

    @property
    def max_drawdown_pct(self) -> float:
        """Largest peak-to-trough decline of the mark-to-market equity curve.

        Returned as a positive percentage (e.g. 12.5 == a 12.5% drawdown).
        A flat/empty curve has zero drawdown.
        """
        peak = self.starting_equity
        worst = 0.0
        for _, eq in self.equity_curve:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > worst:
                    worst = dd
        return worst * 100

    @property
    def per_symbol_pnl(self) -> dict[str, float]:
        """Total realised P&L per symbol across all closed trades.

        Used by the optimizer to penalise parameter sets whose profitability is
        concentrated in a single symbol (an over-fit red flag).
        """
        out: dict[str, float] = {}
        for t in self.trades:
            if t.exit_price is not None:
                out[t.symbol] = out.get(t.symbol, 0.0) + t.pnl
        return out

    @property
    def per_symbol_return_pct(self) -> dict[str, float]:
        """Average per-trade return% grouped by symbol (closed trades only)."""
        acc: dict[str, list[float]] = {}
        for t in self.trades:
            if t.exit_price is not None:
                acc.setdefault(t.symbol, []).append(t.return_pct)
        return {s: sum(v) / len(v) for s, v in acc.items() if v}

    def summary(self) -> str:
        return (
            f"Backtest: start=${self.starting_equity:,.0f} end=${self.ending_equity:,.0f} "
            f"return={self.total_return_pct:+.2f}% trades={self.num_trades} "
            f"win_rate={self.win_rate:.1f}% max_dd={self.max_drawdown_pct:.1f}%"
        )


class Backtester:
    """Daily strategy backtest with ATR stops and fixed-fractional sizing.

    ``strategy`` selects which ranking logic is replayed (see
    ``STRATEGY_RANKERS``). The default ``momentum`` preserves the original
    behaviour. Crypto strategies size fractionally (no whole-share flooring).
    """

    def __init__(
        self,
        cfg: Config,
        starting_equity: float = 100_000.0,
        strategy: str = "momentum",
    ) -> None:
        self.cfg = cfg
        self.starting_equity = starting_equity
        self.strategy = strategy
        if strategy not in STRATEGY_RANKERS:
            raise ValueError(
                f"Unknown backtest strategy '{strategy}'. "
                f"Choose from: {', '.join(sorted(STRATEGY_RANKERS))}"
            )
        self._rank_fn: RankFn = STRATEGY_RANKERS[strategy]
        # Crypto is fractionable and long-only spot; equities floor to whole shares.
        self._fractional = strategy.startswith("crypto")

    def run(
        self,
        broker: BrokerClient,
        symbols: list[str],
        start: datetime,
        end: datetime | None = None,
    ) -> BacktestResult:
        end = end or datetime.now()
        bars_by_symbol = broker.get_bars(symbols, TimeFrame.DAY, start=start, end=end)
        series = {
            s: sorted(bars, key=lambda b: b.timestamp)
            for s, bars in bars_by_symbol.items()
            if bars
        }
        # Restrict each series to the requested [start, end] window. FakeBroker
        # (and some adapters) ignore start/end, so we clip defensively — this is
        # what makes walk-forward windows in the optimizer honest.
        series = self._clip_window(series, start, end)
        return self.run_on_series(series)

    def run_on_series(self, series: dict[str, list[Bar]]) -> BacktestResult:
        """Replay a pre-fetched, pre-sorted bar series (no broker needed).

        The optimizer calls this directly on cached data so a single download
        can be reused across thousands of parameter evaluations.
        """
        series = {s: b for s, b in series.items() if b}
        if not series:
            logger.warning("No historical data for backtest")
            return BacktestResult(self.starting_equity, self.starting_equity)

        equity = self.starting_equity
        open_trades: dict[str, Trade] = {}
        result = BacktestResult(self.starting_equity, self.starting_equity)

        anchor = max(series.values(), key=len)
        warmup = self._warmup()

        for idx in range(warmup + 1, len(anchor) - 1):
            date = anchor[idx].timestamp
            # 1. Manage exits (ATR stop or time exit).
            for sym, trade in list(open_trades.items()):
                sym_bars = series.get(sym, [])
                today = self._bar_on(sym_bars, date)
                if today is None or date.date() < trade.entry_date.date():
                    continue
                stop_hit = today.low <= self._stop_price(sym_bars, trade.entry_price, idx)
                held_days = (date - trade.entry_date).days
                if stop_hit or held_days >= self.cfg.risk.max_hold_days:
                    trade.exit_date = date
                    trade.exit_price = today.close
                    trade.exit_fee = self._trading_cost(trade.exit_price, trade.qty)
                    trade.reason = "stop" if stop_hit else "time_exit"
                    equity += trade.pnl
                    del open_trades[sym]

            # 2. Entries: rank via the selected strategy, open top-N not held.
            ranked = self._rank_fn(series, idx, self.cfg)
            top_n = self._top_n()
            for sym, score in ranked[:top_n]:
                if sym in open_trades or len(open_trades) >= self.cfg.risk.max_positions:
                    continue
                sym_bars = series.get(sym, [])
                next_bar = self._next_bar(sym_bars, date)
                if next_bar is None or score <= 0:
                    continue
                entry = next_bar.open
                stop = self._stop_price(sym_bars, entry, idx)
                risk_per_share = abs(entry - stop) or (entry * 0.02)
                raw_qty = (equity * self.cfg.risk.max_risk_per_trade) / risk_per_share
                qty = raw_qty if self._fractional else float(int(raw_qty))
                if qty <= 0 or (not self._fractional and qty < 1):
                    continue
                open_trades[sym] = Trade(
                    symbol=sym,
                    entry_date=next_bar.timestamp,
                    entry_price=entry,
                    qty=qty,
                    entry_fee=self._trading_cost(entry, qty),
                )
                result.trades.append(open_trades[sym])

            # 3. Mark-to-market equity curve.
            mtm = equity + sum(
                (self._bar_on(series[s], date).close - t.entry_price) * t.qty
                for s, t in open_trades.items()
                if self._bar_on(series[s], date)
                and date.date() >= t.entry_date.date()
            )
            result.equity_curve.append((date, mtm))

        # Close any remaining open trades at last price.
        for sym, trade in open_trades.items():
            last = series[sym][-1]
            trade.exit_date = last.timestamp
            trade.exit_price = last.close
            trade.exit_fee = self._trading_cost(trade.exit_price, trade.qty)
            trade.reason = "eod_close"
            equity += trade.pnl

        result.ending_equity = equity
        return result

    # ── strategy sizing helpers ──────────────────────────────
    def _warmup(self) -> int:
        """Bars of history required before the first entry is allowed."""
        s = self.cfg.strategies
        if self.strategy == "momentum":
            return max(s.momentum.slow_lookback, s.momentum.fast_lookback)
        if self.strategy == "crypto_momentum":
            return max(s.crypto.momentum.slow_ma, s.crypto.momentum.fast_ma, 30)
        if self.strategy == "crypto_volatility":
            return max(s.crypto.volatility.lookback + 5, 30)
        return 30

    def _top_n(self) -> int:
        s = self.cfg.strategies
        if self.strategy == "momentum":
            return s.momentum.top_n
        if self.strategy == "crypto_momentum":
            return s.crypto.momentum.top_n
        if self.strategy == "crypto_volatility":
            return s.crypto.volatility.top_n
        return 10

    def _trading_cost(self, price: float, qty: float) -> float:
        """Return one-side estimated taker fee for a simulated fill."""
        fee_rate = max(0.0, float(getattr(self.cfg.risk, "taker_fee_pct", 0.0)))
        return abs(price * qty) * fee_rate

    # ── bar helpers ──────────────────────────────────────────
    @staticmethod
    def _clip_window(
        series: dict[str, list[Bar]], start: datetime, end: datetime
    ) -> dict[str, list[Bar]]:
        s_date, e_date = start.date(), end.date()
        clipped: dict[str, list[Bar]] = {}
        for sym, bars in series.items():
            kept = [b for b in bars if s_date <= b.timestamp.date() <= e_date]
            if kept:
                clipped[sym] = kept
        return clipped

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


# ─────────────────────────────────────────────────────────────────────────────
# Strategy ranking functions (as-of ``idx``, no look-ahead)
# ─────────────────────────────────────────────────────────────────────────────
def _rank_momentum(
    series: dict[str, list[Bar]], idx: int, cfg: Config
) -> list[tuple[str, float]]:
    """Equity cross-sectional momentum: weighted fast + slow returns."""
    fast = cfg.strategies.momentum.fast_lookback
    slow = cfg.strategies.momentum.slow_lookback
    # Guard against an inverted or degenerate configuration (a candidate the
    # optimizer might propose): both lookbacks must be >= 1 and we need enough
    # history for the *larger* of the two.
    need = max(fast, slow)
    if fast < 1 or slow < 1:
        return []
    scores: list[tuple[str, float]] = []
    for sym, bars in series.items():
        if len(bars) <= idx:
            continue
        closes = [b.close for b in bars[: idx + 1]]
        if len(closes) < need + 1:
            continue
        fast_ret = closes[-1] / closes[-fast] - 1
        slow_ret = closes[-1] / closes[-slow] - 1
        scores.append((sym, 0.6 * fast_ret + 0.4 * slow_ret))
    scores.sort(key=lambda kv: kv[1], reverse=True)
    return scores


def _rank_crypto_momentum(
    series: dict[str, list[Bar]], idx: int, cfg: Config
) -> list[tuple[str, float]]:
    """Dual-MA trend + ADX filter (mirrors ``CryptoMomentumStrategy``)."""
    c = cfg.strategies.crypto.momentum
    scores: list[tuple[str, float]] = []
    for sym, bars in series.items():
        if len(bars) <= idx:
            continue
        window = bars[: idx + 1]
        fast = sma(window, c.fast_ma)
        slow = sma(window, c.slow_ma)
        trend = adx(window, 14)
        if fast is None or slow is None or trend is None:
            continue
        if fast <= slow or trend < c.adx_min:
            continue
        sep = (fast - slow) / slow if slow else 0.0
        score = max(0.0, sep) * (trend / 100.0)
        if score > 0:
            scores.append((sym, score))
    scores.sort(key=lambda kv: kv[1], reverse=True)
    return scores


def _rank_crypto_volatility(
    series: dict[str, list[Bar]], idx: int, cfg: Config
) -> list[tuple[str, float]]:
    """Low-vol coil in an intact uptrend (mirrors ``CryptoVolatilityStrategy``).

    Lower volatility ranks higher, so scores are inverted (1/vol) to keep the
    engine's ``score > 0`` / top-N convention.
    """
    c = cfg.strategies.crypto.volatility
    candidates: list[tuple[str, float]] = []
    for sym, bars in series.items():
        if len(bars) <= idx:
            continue
        window = bars[: idx + 1]
        vol = realized_vol(window, c.lookback)
        if vol is None or vol > c.high_vol_pct or vol > c.low_vol_pct:
            continue
        fast = sma(window, 10)
        if fast is None or window[-1].close < fast:
            continue
        # Tighter coil (lower vol) → higher score.
        candidates.append((sym, 1.0 / (vol + 1e-9)))
    candidates.sort(key=lambda kv: kv[1], reverse=True)
    return candidates


STRATEGY_RANKERS: dict[str, RankFn] = {
    "momentum": _rank_momentum,
    "crypto_momentum": _rank_crypto_momentum,
    "crypto_volatility": _rank_crypto_volatility,
}


# ─────────────────────────────────────────────────────────────────────────────
# Window selection
# ─────────────────────────────────────────────────────────────────────────────
def _subtract_months(dt: datetime, months: int) -> datetime:
    """Shift ``dt`` back by whole calendar months, clamping the day of month."""
    index = dt.month - 1 - months
    year = dt.year + index // 12
    month = index % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _resolve_start(
    now: datetime, *, months: int | None = None, days: int | None = None
) -> datetime:
    """Start of the replay window; ``months`` wins, then ``days``, else default."""
    if months is not None:
        return _subtract_months(now, months)
    if days is not None:
        return now - timedelta(days=days)
    return _subtract_months(now, DEFAULT_MONTHS)


def _approx_bar_count(start: datetime, end: datetime, strategy: str) -> int:
    """Rough number of bars in a window — crypto trades 7 days a week, equities 5."""
    per_week = 7 if strategy.startswith("crypto") else 5
    return int(max((end - start).days, 0) * per_week / 7)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a simple daily backtest")
    parser.add_argument("--symbols", required=True, help="Comma-separated tickers")
    window = parser.add_mutually_exclusive_group()
    window.add_argument(
        "--months",
        type=int,
        default=None,
        help=f"How many past months to replay (default: {DEFAULT_MONTHS})",
    )
    window.add_argument(
        "--days",
        type=int,
        default=None,
        help="Alternative to --months: window length in calendar days",
    )
    parser.add_argument("--equity", type=float, default=100_000.0)
    parser.add_argument(
        "--strategy",
        default="momentum",
        choices=sorted(STRATEGY_RANKERS),
        help="Which strategy's ranking logic to replay",
    )
    args = parser.parse_args()
    if args.months is not None and args.months < 1:
        parser.error("--months must be >= 1")
    if args.days is not None and args.days < 1:
        parser.error("--days must be >= 1")

    cfg = load_config("config.yaml")
    setup_logging(cfg)

    broker = make_broker(cfg)
    broker.connect()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    end = datetime.now()
    start = _resolve_start(end, months=args.months, days=args.days)

    bt = Backtester(cfg, starting_equity=args.equity, strategy=args.strategy)
    print(
        f"Replaying '{args.strategy}' over {len(symbols)} symbol(s), "
        f"{start.date()} → {end.date()}"
    )
    warmup = bt._warmup()
    approx_bars = _approx_bar_count(start, end, args.strategy)
    if approx_bars <= warmup:
        print(
            f"WARNING: ~{approx_bars} bars fit in this window but '{args.strategy}' "
            f"needs {warmup} bars of warm-up before its first entry — expect no "
            f"trades. Widen the window with --months."
        )

    result = bt.run(broker, symbols, start, end)
    print(result.summary())
    for t in result.trades:
        if t.exit_price is not None:
            print(
                f"  {t.symbol} {t.entry_date.date()} → {t.exit_date.date()} "
                f"{t.return_pct:+.1f}% ({t.reason})"
            )


if __name__ == "__main__":
    main()
