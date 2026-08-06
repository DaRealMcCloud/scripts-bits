"""Main entry point — scheduled trading loop using APScheduler."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from trader.brokers.base import BrokerAuthError, BrokerClient
from trader.brokers.factory import make_broker
from trader.brokers.types import TimeFrame
from trader.config import Config, load_config
from trader.data.universe import build_universe, split_universe
from trader.execution.order_manager import OrderManager
from trader.execution.portfolio import log_portfolio
from trader.execution.state_store import OrderRecord, StateStore
from trader.logger import setup_logging
from trader.risk.manager import RiskManager
from trader.strategies.base import MarketRegime, Signal, Strategy
from trader.strategies.breakout import BreakoutStrategy
from trader.strategies.dca import DcaStrategy
from trader.strategies.gap_fade import GapFadeStrategy
from trader.strategies.mean_reversion import MeanReversionStrategy
from trader.strategies.ml_base import NullMLStrategy
from trader.strategies.momentum import MomentumStrategy
from trader.strategies.pairs_trading import PairsTradingStrategy
from trader.strategies.quote_imbalance import QuoteImbalanceScorer
from trader.strategies.rebalance import RebalanceStrategy
from trader.strategies.trend_following import TrendFollowingStrategy
from trader.strategies.volatility import VolatilityStrategy
from trader.analysis.aggregator import SignalAggregator
from trader.analysis.regime import RegimeClassifier
from trader.strategies.crypto import (
    CryptoBreakoutStrategy,
    CryptoDcaStrategy,
    CryptoMeanReversionStrategy,
    CryptoMomentumStrategy,
    CryptoVolatilityStrategy,
)

logger = logging.getLogger(__name__)

# Heartbeat file the supervisor watches to detect a stalled scheduler.
HEARTBEAT_FILE = Path(os.environ.get("TRADER_HEARTBEAT", "data/heartbeat"))


class Trader:
    """Top-level trading orchestrator."""

    def __init__(self, cfg: Config, broker: BrokerClient | None = None) -> None:
        self.cfg = cfg
        self.broker: BrokerClient = broker or make_broker(cfg)
        self.state = StateStore()
        self.risk = RiskManager(self.broker, cfg)
        self.imbalance = QuoteImbalanceScorer(cfg)
        self.order_mgr = OrderManager(self.broker, cfg, self.risk, self.imbalance, self.state)

        # Decision-support layer.
        agg_cfg = cfg.strategies.aggregator
        self.regime_classifier = RegimeClassifier()
        self.aggregator = SignalAggregator(
            strategy_weights=self._strategy_weights(),
            top_n=agg_cfg.top_n,
            min_score=agg_cfg.min_score,
            use_regime_routing=agg_cfg.use_regime_routing,
            regime_fit_bonus=agg_cfg.regime_fit_bonus,
            regime_fit_penalty=agg_cfg.regime_fit_penalty,
        )
        self.current_regime: MarketRegime = MarketRegime.UNKNOWN

        # Separate decision-support layer for 24/7 crypto (distinct regime + weights).
        cagg = cfg.strategies.crypto.aggregator
        self.crypto_aggregator = SignalAggregator(
            strategy_weights=self._crypto_strategy_weights(),
            top_n=cagg.top_n,
            min_score=cagg.min_score,
            use_regime_routing=cagg.use_regime_routing,
            regime_fit_bonus=cagg.regime_fit_bonus,
            regime_fit_penalty=cagg.regime_fit_penalty,
        )
        self.current_crypto_regime: MarketRegime = MarketRegime.UNKNOWN
        self._last_crypto_regime_at: datetime | None = None

        # Universe — built once at startup, refreshed daily
        self.stock_symbols: list[str] = []
        self.crypto_symbols: list[str] = []
        # Throttle for broker order backfill (epoch seconds of last run).
        self._last_order_backfill: float = 0.0

        # Strategies (built after the universe is known).
        self.momentum: MomentumStrategy | None = None
        self.gap_fade: GapFadeStrategy | None = None
        self.strategies: list[Strategy] = []
        self.crypto_strategies: list[Strategy] = []

    def _strategy_weights(self) -> dict[str, float]:
        s = self.cfg.strategies
        return {
            "trend_following": s.trend_following.weight,
            "mean_reversion": s.mean_reversion.weight,
            "breakout": s.breakout.weight,
            "volatility": s.volatility.weight,
            "pairs_trading": s.pairs_trading.weight,
            "dca": s.dca.weight,
            "rebalance": s.rebalance.weight,
            "ml": s.ml.weight,
            "momentum": 1.0,
            "gap_fade": 1.0,
        }

    def _crypto_strategy_weights(self) -> dict[str, float]:
        c = self.cfg.strategies.crypto
        return {
            "crypto_momentum": c.momentum.weight,
            "crypto_mean_reversion": c.mean_reversion.weight,
            "crypto_breakout": c.breakout.weight,
            "crypto_dca": c.dca.weight,
            "crypto_volatility": c.volatility.weight,
        }

    # ── Lifecycle ────────────────────────────────────────────

    def startup(self) -> None:
        """Run once at bot startup: connect, reconcile, build universe."""
        logger.info("=" * 60)
        logger.info("Trader starting up (broker=%s)", self.cfg.broker.provider)

        self.broker.connect()

        acct = self.broker.get_account()
        logger.info("Account: equity=$%.2f, buying_power=$%.2f", acct.equity, acct.buying_power)

        # Crash recovery: reconcile live positions + open orders from the broker.
        self._reconcile()

        self._refresh_universe()
        self.heartbeat()

        # Crypto trades 24/7, so run one scan immediately rather than waiting
        # for the first cron boundary. The scheduler handles subsequent scans.
        if self.cfg.strategies.crypto.enabled:
            logger.info("Running initial crypto scan at startup...")
            try:
                self.crypto_scan()
            except Exception:
                logger.error("Initial crypto scan failed", exc_info=True)

        # Equities: if we start up while the market is already open AND we're
        # still within the first half of the session, don't idle until the next
        # cron boundary — run an initial scan now so we can participate today.
        self._startup_equity_catchup()

        logger.info("Startup complete")
        logger.info("=" * 60)

    def _startup_equity_catchup(self) -> None:
        """Run an initial equity scan if we start up mid-session.

        Normally the equity strategies fire on cron boundaries (pre_market
        08:30, market_open 09:30, intraday every 15 min). If the bot is
        (re)started while the market is already open we would otherwise sit
        idle until the next boundary. To avoid missing the day, we run a catch-up
        scan — but ONLY within the FIRST HALF of the regular session, so we don't
        open fresh intraday positions late in the day (they'd just get flattened
        at pre_close).
        """
        if not any(s for s in self.strategies):
            return  # no equity strategies enabled

        phase = _equity_session_phase()
        if phase != "first_half":
            logger.info(
                "Skipping startup equity catch-up (session phase: %s). "
                "Equity strategies will run on their normal schedule.",
                phase,
            )
            return

        logger.info("Market already open (first half) — running startup equity catch-up...")
        try:
            # Mirror the pre_market daily scan + the market_open gap-fade so a
            # mid-morning restart participates like a normal open would.
            self.risk.record_start_of_day()
            self.order_mgr.reset_daily()
            self._snapshot_equity()
            self._detect_regime()
            self._scan_and_process(list(self.strategies))
            self._start_quote_stream()
        except Exception:
            logger.error("Startup equity catch-up failed", exc_info=True)

    def _reconcile(self) -> None:
        """Reconcile in-memory/persisted state with the broker after a restart."""
        try:
            positions = self.broker.get_positions()
            open_orders = self.broker.get_open_orders()
        except Exception:
            logger.warning("Reconciliation failed to read broker state", exc_info=True)
            return

        logger.info(
            "Reconcile: %d live position(s), %d open order(s) at broker",
            len(positions),
            len(open_orders),
        )
        for pos in positions:
            logger.info(
                "  live position %s qty=%.0f @ $%.2f (P&L $%.2f)",
                pos.symbol,
                pos.qty,
                pos.avg_entry_price,
                pos.unrealized_pl,
            )
        # Warn about positions that lack a protective stop order.
        #
        # Broker-side stops only apply to equities: Alpaca rejects stop /
        # stop-limit / bracket orders for crypto (only market & limit are
        # allowed), so crypto is protected by our software stop loop
        # (``_check_crypto_stops``) instead. Treat the two cases differently so
        # the log isn't spammed with a scary WARNING for every crypto position
        # that is, in fact, protected.
        stop_symbols = {o.symbol for o in open_orders if o.stop_price}
        crypto_stops = self.state.get_crypto_stops()
        for pos in positions:
            is_crypto = "/" in pos.symbol
            if is_crypto:
                # Alpaca can't hold a broker-side stop for crypto; rely on the
                # software stop registry + the crypto_stop_check poll.
                if pos.symbol in crypto_stops:
                    logger.info(
                        "Position %s protected by software stop @ $%.6f "
                        "(no broker-side stop \u2014 Alpaca crypto doesn't support them)",
                        pos.symbol,
                        crypto_stops[pos.symbol].get("stop_price", 0.0),
                    )
                else:
                    logger.warning(
                        "Crypto position %s has NO software stop registered; the "
                        "stop-check loop can't protect it. It will be re-evaluated "
                        "on the next crypto scan.",
                        pos.symbol,
                    )
            elif pos.symbol not in stop_symbols:
                logger.warning(
                    "Position %s has NO protective stop order at the broker", pos.symbol
                )

    def _refresh_universe(self) -> None:
        logger.info("Building tradeable universe...")
        universe = build_universe(self.broker, self.cfg)
        self.stock_symbols, self.crypto_symbols = split_universe(universe)
        self._build_strategies()

    def _build_strategies(self) -> None:
        """Instantiate all enabled strategies and register their regime prefs."""
        s = self.cfg.strategies
        syms = self.stock_symbols

        # Keep direct handles for the scheduler's momentum/gap_fade phases.
        self.momentum = MomentumStrategy(self.broker, self.cfg, syms)
        self.gap_fade = GapFadeStrategy(self.broker, self.cfg, syms)

        candidates: list[tuple[bool, Strategy]] = [
            (s.momentum.enabled, self.momentum),
            (s.gap_fade.enabled, self.gap_fade),
            (s.trend_following.enabled, TrendFollowingStrategy(self.broker, self.cfg, syms)),
            (s.mean_reversion.enabled, MeanReversionStrategy(self.broker, self.cfg, syms)),
            (s.breakout.enabled, BreakoutStrategy(self.broker, self.cfg, syms)),
            (s.volatility.enabled, VolatilityStrategy(self.broker, self.cfg, syms)),
            (s.pairs_trading.enabled, PairsTradingStrategy(self.broker, self.cfg, syms)),
            (s.dca.enabled, DcaStrategy(self.broker, self.cfg, syms)),
            (s.rebalance.enabled, RebalanceStrategy(self.broker, self.cfg, syms)),
            (s.ml.enabled, NullMLStrategy(self.broker, self.cfg, syms)),
        ]
        self.strategies = [strat for enabled, strat in candidates if enabled]
        for strat in self.strategies:
            self.aggregator.register_strategy_regimes(strat.name, strat.preferred_regimes)
        logger.info(
            "Active strategies: %s",
            ", ".join(sorted({s.name for s in self.strategies})) or "(none)",
        )

        self._build_crypto_strategies()

    def _build_crypto_strategies(self) -> None:
        """Instantiate enabled crypto strategies (long-only, 24/7)."""
        c = self.cfg.strategies.crypto
        csyms = self.crypto_symbols

        if not c.enabled:
            self.crypto_strategies = []
            logger.info("Crypto strategies disabled")
            return
        if not csyms:
            self.crypto_strategies = []
            logger.info("Crypto strategies enabled but no crypto symbols in universe")
            return

        candidates: list[tuple[bool, Strategy]] = [
            (c.momentum.enabled, CryptoMomentumStrategy(self.broker, self.cfg, csyms)),
            (c.mean_reversion.enabled, CryptoMeanReversionStrategy(self.broker, self.cfg, csyms)),
            (c.breakout.enabled, CryptoBreakoutStrategy(self.broker, self.cfg, csyms)),
            (c.dca.enabled, CryptoDcaStrategy(self.broker, self.cfg, csyms)),
            (c.volatility.enabled, CryptoVolatilityStrategy(self.broker, self.cfg, csyms)),
        ]
        self.crypto_strategies = [strat for enabled, strat in candidates if enabled]
        for strat in self.crypto_strategies:
            self.crypto_aggregator.register_strategy_regimes(strat.name, strat.preferred_regimes)
        logger.info(
            "Active crypto strategies: %s",
            ", ".join(sorted({s.name for s in self.crypto_strategies})) or "(none)",
        )

    def _detect_regime(self) -> None:
        """Classify the overall market regime from the benchmark symbol."""
        benchmark = self.cfg.strategies.aggregator.regime_benchmark
        try:
            from datetime import timedelta

            end = datetime.now()
            start = end - timedelta(days=120)
            bars = self.broker.get_bars([benchmark], TimeFrame.DAY, start=start, end=end)
            series = bars.get(benchmark, [])
            if series:
                self.current_regime = self.regime_classifier.classify(series)
                logger.info("Market regime (%s): %s", benchmark, self.current_regime.value)
        except Exception:
            logger.warning("Regime detection failed", exc_info=True)

    def _detect_crypto_regime(self) -> None:
        """Classify the crypto market regime from the crypto benchmark (BTC/USD)."""
        benchmark = self.cfg.strategies.crypto.aggregator.regime_benchmark
        try:
            from datetime import timedelta

            end = datetime.now()
            start = end - timedelta(days=120)
            bars = self.broker.get_bars([benchmark], TimeFrame.DAY, start=start, end=end)
            series = bars.get(benchmark, [])
            if series:
                self.current_crypto_regime = self.regime_classifier.classify(series)
                self._last_crypto_regime_at = datetime.now(timezone.utc)
                logger.info(
                    "Crypto regime (%s): %s", benchmark, self.current_crypto_regime.value
                )
        except Exception:
            logger.warning("Crypto regime detection failed", exc_info=True)

    def heartbeat(self) -> None:
        """Touch the heartbeat file + record an equity snapshot."""
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except Exception:
            logger.debug("Failed to write heartbeat", exc_info=True)
        # Keep the dashboard's equity fresh (heartbeat runs every minute, 24/7).
        self._snapshot_equity()
        # Refresh non-terminal order statuses so the dashboard shows real fills
        # instead of the submit-time "pending" snapshot.
        self._reconcile_order_statuses()
        # Backfill fills from the broker (missing software-close SELLs + real
        # fill prices) at most every ~5 minutes to keep API usage light.
        now = time.time()
        if now - self._last_order_backfill >= 300:
            self._last_order_backfill = now
            self._backfill_orders_from_broker()

    def _reconcile_order_statuses(self) -> None:
        """Update persisted orders whose stored status is still non-terminal.

        Orders are recorded at submit time (usually ``pending``/``new``). Their
        true state (filled, canceled, rejected…) only settles later at the
        broker, so we poll each non-terminal order and write back any change.
        """
        try:
            pending = self.state.non_terminal_orders(limit=500)
        except Exception:
            logger.debug("Could not read non-terminal orders", exc_info=True)
            return
        for o in pending:
            order_id = o.get("order_id")
            if not order_id:
                continue
            try:
                status = self.broker.get_order_status(order_id)
            except Exception:
                logger.debug("Status lookup failed for order %s", order_id, exc_info=True)
                continue
            if status is None:
                continue
            new_status = status.value
            if new_status and new_status != (o.get("status") or ""):
                try:
                    self.state.update_order_status(order_id, new_status)
                    logger.debug(
                        "Order %s status %s -> %s", order_id, o.get("status"), new_status
                    )
                except Exception:
                    logger.debug("Failed to persist status for %s", order_id, exc_info=True)

    def _backfill_orders_from_broker(self, lookback_days: int = 7) -> None:
        """Import broker fills the app didn't record + correct fill prices.

        Software stop / take-profit closes go through ``close_position`` (a
        market SELL) and, historically, weren't recorded — so closed round
        trips showed no realized P/L. This pulls recent filled orders from the
        broker and (a) inserts any missing ones using the real fill price and
        (b) overwrites approximate limit-price fills with the true
        ``filled_avg_price`` so P/L is exact.
        """
        try:
            recent = self.broker.get_recent_orders(lookback_days=lookback_days)
        except Exception:
            logger.debug("Order backfill: broker fetch failed", exc_info=True)
            return

        for o in recent:
            try:
                fill_price = o.filled_avg_price
                status = o.status.value if hasattr(o.status, "value") else str(o.status)
                side = o.side.value if hasattr(o.side, "value") else str(o.side)
                existing = self.state.get_order(o.order_id)
                if existing is None:
                    # Only import orders that actually filled (avoid noise from
                    # open/canceled orders we never tracked).
                    if fill_price and o.filled_qty and o.filled_qty > 0:
                        self.state.record_order(
                            OrderRecord(
                                order_id=o.order_id,
                                symbol=o.symbol,
                                side=side,
                                qty=float(o.filled_qty or o.qty or 0.0),
                                limit_price=float(fill_price),
                                stop_price=o.stop_price,
                                strategy="backfill",
                                reason="imported from broker fill history",
                                status=status,
                            )
                        )
                        logger.debug("Backfilled order %s (%s)", o.order_id, o.symbol)
                else:
                    # Correct the stored fill price to the broker's real value.
                    if fill_price and abs(
                        float(existing.get("limit_price") or 0.0) - float(fill_price)
                    ) > 1e-12:
                        self.state.update_order_fill(o.order_id, float(fill_price), status)
            except Exception:
                logger.debug(
                    "Backfill: skipping order %s",
                    getattr(o, "order_id", "?"),
                    exc_info=True,
                )

    def _snapshot_equity(self) -> None:
        try:
            acct = self.broker.get_account()
            unrealized_pl = acct.unrealized_pl
            # Alpaca's account-level unrealized_pl is unreliable for crypto /
            # paper accounts (often 0 or missing). Positions always carry their
            # own unrealized_pl, so sum those as the source of truth.
            try:
                positions = self.broker.get_positions()
                pos_pl = sum(p.unrealized_pl for p in positions)
                if positions:
                    unrealized_pl = pos_pl
                # Persist the per-symbol snapshot so the dashboard can show live
                # unrealized P/L next to each open BUY transaction.
                self.state.replace_positions(
                    [
                        {
                            "symbol": p.symbol,
                            "qty": p.qty,
                            "avg_entry_price": p.avg_entry_price,
                            "current_price": p.current_price,
                            "market_value": p.market_value,
                            "unrealized_pl": p.unrealized_pl,
                            "unrealized_pl_pct": p.unrealized_pl_pct,
                        }
                        for p in positions
                    ]
                )
            except Exception:
                logger.debug("Could not sum position P&L for snapshot", exc_info=True)
            self.state.record_equity(acct.equity, acct.cash, unrealized_pl)
        except Exception:
            logger.debug("Failed to record equity snapshot", exc_info=True)

    # ── Scheduled Jobs ───────────────────────────────────────

    def pre_market(self) -> None:
        """08:30 ET — record SOD equity, detect regime, run daily scanners."""
        logger.info("── PRE-MARKET ──")
        self.heartbeat()
        self.risk.record_start_of_day()
        self.order_mgr.reset_daily()
        self._snapshot_equity()

        # Refresh universe weekly (Monday)
        if datetime.now().weekday() == 0:
            self._refresh_universe()

        # Classify the market regime for aggregator routing.
        self._detect_regime()

        # Run every daily strategy except the intraday ones (gap_fade runs at
        # the open, quote_imbalance is streaming-only). Route through the brain.
        daily = [s for s in self.strategies if s.name not in ("gap_fade",)]
        self._scan_and_process(daily)

    def market_open(self) -> None:
        """09:30 ET — gap fade entries at market open."""
        logger.info("── MARKET OPEN ──")
        self.heartbeat()
        if self.gap_fade and self.cfg.strategies.gap_fade.enabled:
            self._scan_and_process([self.gap_fade])

        # Start streaming for quote imbalance (capability-gated).
        self._start_quote_stream()

    def intraday_check(self) -> None:
        """Every 15 min during market hours — check drawdown, log portfolio."""
        self.heartbeat()
        self._snapshot_equity()
        if not self.risk.check_drawdown():
            logger.warning("Drawdown breaker active — cancelling open orders")
            self.order_mgr.cancel_all()
            return

        log_portfolio(self.broker)

    def pre_close(self) -> None:
        """15:55 ET — flatten intraday positions if configured."""
        logger.info("── PRE-CLOSE ──")
        self.heartbeat()
        if self.cfg.risk.flatten_eod:
            include_crypto = self.cfg.risk.flatten_crypto_eod
            self.order_mgr.cancel_all(include_crypto=include_crypto)
            self.order_mgr.flatten_all(include_crypto=include_crypto)
            logger.info(
                "EOD flatten complete (crypto %s)",
                "included" if include_crypto else "left open",
            )

    def end_of_day(self) -> None:
        """16:05 ET — EOD summary."""
        logger.info("── END OF DAY SUMMARY ──")
        self.heartbeat()
        self._stop_quote_stream()
        log_portfolio(self.broker)

        orders = self.order_mgr.orders_today
        logger.info("Orders placed today: %d", len(orders))
        for o in orders:
            logger.info(
                "  %s %s %s qty=%.0f @ $%.2f [%s]",
                o.get("created_at"),
                o.get("side"),
                o.get("symbol"),
                o.get("qty", 0),
                o.get("limit_price", 0),
                o.get("strategy"),
            )

        acct = self.broker.get_account()
        self._snapshot_equity()
        logger.info("EOD equity: $%.2f", acct.equity)
        logger.info("=" * 60)

    # ── Signal Processing ────────────────────────────────────

    def _scan_and_process(self, strategies: list[Strategy]) -> None:
        """Scan the given strategies, aggregate/rank, then execute top candidates."""
        raw: list[Signal] = []
        for strat in strategies:
            try:
                raw.extend(strat.scan())
            except Exception:
                logger.error("Strategy %s scan failed", strat.name, exc_info=True)

        if not raw:
            return

        if self.cfg.strategies.aggregator.enabled:
            ranked = self.aggregator.aggregate(raw, self.current_regime)
            signals = [rs.signal for rs in ranked]
            logger.info("Executing %d aggregated candidate(s)", len(signals))
        else:
            signals = raw

        for sig in signals:
            self.order_mgr.execute_signal(sig)

    # ── Crypto (24/7) ────────────────────────────────────────

    def crypto_scan(self) -> None:
        """Runs every ``scan_interval_minutes``, 7 days a week.

        Order matters: enforce software stops FIRST (protect open positions),
        then refresh the crypto regime (throttled ~hourly), then scan/execute.
        """
        self.heartbeat()
        if not self.cfg.strategies.crypto.enabled:
            return

        # 1. Enforce software protective stops before anything else.
        self._check_crypto_stops()

        # 2. Refresh crypto regime at most ~hourly to limit data calls.
        now = datetime.now(timezone.utc)
        if (
            self._last_crypto_regime_at is None
            or (now - self._last_crypto_regime_at) >= timedelta(hours=1)
        ):
            self._detect_crypto_regime()

        # 3. Scan crypto strategies and execute.
        if self.crypto_strategies:
            self._scan_and_process_crypto(self.crypto_strategies)

    def crypto_stop_check(self) -> None:
        """Runs every ``stop_check_interval_minutes`` (more often than the scan).

        Only enforces protective stops / take-profit on OPEN positions — it does
        NOT scan for new opportunities. This keeps our loss-save mechanisms
        running on a tight cadence so a fast move can't wipe out a position in
        the gap between the (less frequent) opportunity scans.
        """
        self.heartbeat()
        if not self.cfg.strategies.crypto.enabled:
            return
        self._check_crypto_stops()
        self._cancel_stale_crypto_orders()

    def _scan_and_process_crypto(self, strategies: list[Strategy]) -> None:
        raw: list[Signal] = []
        for strat in strategies:
            try:
                raw.extend(strat.scan())
            except Exception:
                logger.error("Crypto strategy %s scan failed", strat.name, exc_info=True)

        if not raw:
            return

        if self.cfg.strategies.crypto.aggregator.enabled:
            ranked = self.crypto_aggregator.aggregate(raw, self.current_crypto_regime)
            signals = [rs.signal for rs in ranked]
            logger.info("Executing %d aggregated crypto candidate(s)", len(signals))
        else:
            signals = raw

        for sig in signals:
            self.order_mgr.execute_signal(sig)

    def _record_close_order(self, symbol, pos, exit_price: float, strategy: str) -> None:
        """Persist a synthetic SELL order for a software-driven crypto close.

        Crypto stop / take-profit exits go through ``broker.close_position``
        (a market SELL) which bypasses the OrderManager, so nothing was ever
        recorded. Without this, the closed round-trip is invisible to the
        dashboard (the buy shows "closed" with no realized P/L). We record the
        sell at the exit price so the transaction list shows a real close and
        the realized-P/L annotator can pair it with the prior buys.
        """
        try:
            qty = abs(float(getattr(pos, "qty", 0.0) or 0.0)) if pos is not None else 0.0
            if qty <= 0:
                return
            oid = f"close-{symbol.replace('/', '')}-{int(time.time() * 1000)}"
            self.state.record_order(
                OrderRecord(
                    order_id=oid,
                    symbol=symbol,
                    side="sell",
                    qty=qty,
                    limit_price=float(exit_price),
                    stop_price=None,
                    strategy=strategy,
                    reason="software exit (stop/take-profit)",
                    status="filled",
                )
            )
        except Exception:
            logger.debug("Failed to record close order for %s", symbol, exc_info=True)

    def _cancel_stale_crypto_orders(self) -> int:
        """Cancel resting (unfilled) crypto entry orders older than the TTL.

        Crypto orders are GTC and the equity EOD flatten leaves them alone, so
        a limit order that never fills would otherwise linger forever — and the
        no-pyramiding exposure guard treats a working order as existing
        exposure, silently blocking new entries for that symbol. This sweep
        keeps the book clean. Returns the number of orders cancelled.
        """
        ttl = int(getattr(self.cfg.strategies.crypto, "crypto_order_ttl_minutes", 0) or 0)
        if ttl <= 0:
            return 0

        try:
            open_orders = self.broker.get_open_orders()
        except Exception as exc:
            logger.warning(
                "Could not read open orders for stale-order sweep (%s); "
                "will retry next cycle",
                exc,
            )
            logger.debug("Open-order read failure detail", exc_info=True)
            return 0

        now = datetime.now(timezone.utc)
        cutoff = timedelta(minutes=ttl)
        cancelled = 0
        for o in open_orders:
            if "/" not in o.symbol:  # only crypto (slashed symbols)
                continue
            submitted = getattr(o, "submitted_at", None)
            if submitted is None:
                continue
            # Normalise to an aware UTC datetime for the age comparison.
            if submitted.tzinfo is None:
                submitted = submitted.replace(tzinfo=timezone.utc)
            age = now - submitted
            if age < cutoff:
                continue
            try:
                self.broker.cancel_order(o.order_id)
                cancelled += 1
                logger.info(
                    "Cancelled stale crypto order %s (%s, age %.0f min > TTL %d min)",
                    o.order_id,
                    o.symbol,
                    age.total_seconds() / 60.0,
                    ttl,
                )
            except Exception:
                logger.warning(
                    "Failed to cancel stale crypto order %s (%s)",
                    o.order_id,
                    o.symbol,
                    exc_info=True,
                )
        return cancelled

    def _check_crypto_stops(self) -> None:
        """Enforce software stops for open crypto positions.

        Alpaca crypto has no broker-side stops, so we exit in software:
        if the latest price is at/below the stored stop, market-close the
        position and clear the stop. Optionally ratchet a trailing stop up.
        """
        stops = self.state.get_crypto_stops()
        if not stops:
            return

        ccfg = self.cfg.strategies.crypto
        try:
            positions = {p.symbol: p for p in self.broker.get_positions()}
        except Exception as exc:
            logger.warning(
                "Could not read positions for crypto stop check (%s); "
                "will retry next cycle",
                exc,
            )
            logger.debug("Position read failure detail", exc_info=True)
            return

        symbols = [s for s in stops if s in positions]
        if not symbols:
            # Clean up stale stops for positions no longer held.
            for s in list(stops):
                if s not in positions:
                    self.state.delete_crypto_stop(s)
            return

        try:
            quotes = self.broker.get_latest_quotes(symbols)
        except Exception as exc:
            logger.warning(
                "Could not read quotes for crypto stop check (%s); "
                "will retry next cycle",
                exc,
            )
            logger.debug("Quote read failure detail", exc_info=True)
            return

        for symbol in symbols:
            quote = quotes.get(symbol)
            if quote is None:
                continue
            price = quote.bid_price if quote.bid_price and quote.bid_price > 0 else quote.mid
            if not price or price <= 0:
                continue
            stop_price = float(stops[symbol]["stop_price"])

            if price <= stop_price:
                logger.warning(
                    "CRYPTO STOP HIT %s: price $%.6g <= stop $%.6g — closing",
                    symbol,
                    price,
                    stop_price,
                )
                pos = positions.get(symbol)
                try:
                    # Cancel any resting orders on this symbol, then flatten it.
                    for o in self.broker.get_open_orders():
                        if o.symbol == symbol:
                            self.broker.cancel_order(o.order_id)
                    if self.broker.close_position(symbol):
                        self._record_close_order(symbol, pos, price, "crypto_stop")
                except Exception:
                    logger.error("Failed to close crypto stop for %s", symbol, exc_info=True)
                finally:
                    self.state.delete_crypto_stop(symbol)
                continue

            # Take-profit: lock in winners at the configured target (the "7" in
            # the 3-5-7 rule). Sell when price is up take_profit_pct from entry.
            tp_pct = getattr(self.cfg.risk, "take_profit_pct", 0.0)
            entry_price = float(stops[symbol].get("entry_price") or 0.0)
            if tp_pct > 0 and entry_price > 0 and price >= entry_price * (1.0 + tp_pct):
                logger.info(
                    "CRYPTO TAKE-PROFIT %s: price $%.6g >= target $%.6g "
                    "(+%.1f%% from entry $%.6g) — closing",
                    symbol,
                    price,
                    entry_price * (1.0 + tp_pct),
                    tp_pct * 100,
                    entry_price,
                )
                pos = positions.get(symbol)
                try:
                    for o in self.broker.get_open_orders():
                        if o.symbol == symbol:
                            self.broker.cancel_order(o.order_id)
                    if self.broker.close_position(symbol):
                        self._record_close_order(symbol, pos, price, "crypto_take_profit")
                except Exception:
                    logger.error(
                        "Failed to close crypto take-profit for %s", symbol, exc_info=True
                    )
                finally:
                    self.state.delete_crypto_stop(symbol)
                continue

            # Optional trailing stop: ratchet up only, never down.
            if ccfg.trailing_stop:
                new_stop = price * (1.0 - ccfg.trail_pct)
                if new_stop > stop_price:
                    entry = float(stops[symbol].get("entry_price") or 0.0)
                    self.state.set_crypto_stop(symbol, new_stop, entry)
                    logger.info(
                        "Trailing crypto stop %s: $%.6g -> $%.6g",
                        symbol,
                        stop_price,
                        new_stop,
                    )

    def _start_quote_stream(self) -> None:
        if not self.cfg.strategies.quote_imbalance.enabled:
            return
        if not self.broker.capabilities.supports_streaming:
            logger.info("Broker does not support streaming; imbalance timing degraded")
            return

        watch_symbols = [o["symbol"] for o in self.order_mgr.orders_today if o.get("symbol")]
        # Quote-imbalance uses the equity NBBO stream only. Crypto symbols (with
        # a "/") are NOT valid on Alpaca's StockDataStream and cause a websocket
        # "invalid syntax (400)" error, so filter them out here.
        watch_symbols = [s for s in watch_symbols if "/" not in s]
        if not watch_symbols:
            return

        def on_quote(symbol: str, bid_size: float, ask_size: float, quote) -> None:
            self.imbalance.on_quote(symbol, bid_size, ask_size)

        try:
            self.broker.subscribe_quotes(watch_symbols, on_quote)
            self.broker.start_stream()
            logger.info("Quote imbalance stream started for %d symbols", len(watch_symbols))
        except Exception:
            logger.error("Failed to start quote stream", exc_info=True)

    def _stop_quote_stream(self) -> None:
        try:
            self.broker.stop_stream()
        except Exception:
            pass


def build_scheduler(trader: Trader) -> BlockingScheduler:
    # US equity market timezone. IMPORTANT: pass this to every CronTrigger too —
    # a CronTrigger built without an explicit timezone defaults to the machine's
    # LOCAL zone (e.g. Europe/Berlin), NOT the scheduler's. Without this the
    # "09:30" market-open job would fire at 09:30 local time instead of 09:30 ET.
    et = "US/Eastern"
    scheduler = BlockingScheduler(timezone=et)
    scheduler.add_job(
        trader.pre_market, CronTrigger(day_of_week="mon-fri", hour=8, minute=30, timezone=et)
    )
    scheduler.add_job(
        trader.market_open, CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=et)
    )
    scheduler.add_job(
        trader.intraday_check,
        CronTrigger(day_of_week="mon-fri", hour="10-15", minute="*/15", timezone=et),
    )
    scheduler.add_job(
        trader.pre_close, CronTrigger(day_of_week="mon-fri", hour=15, minute=55, timezone=et)
    )
    scheduler.add_job(
        trader.end_of_day, CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=et)
    )
    # Crypto trades 24/7 — scan for opportunities every N minutes, all week.
    # These are interval-style (every N min) so the timezone is immaterial, but
    # we set it for consistency.
    if trader.cfg.strategies.crypto.enabled:
        interval = max(1, int(trader.cfg.strategies.crypto.scan_interval_minutes))
        scheduler.add_job(trader.crypto_scan, CronTrigger(minute=f"*/{interval}", timezone=et))
        # Protective stops / take-profit run on a tighter cadence than the scan
        # so a fast drop can't wipe out a position between opportunity scans.
        stop_interval = max(
            1, int(trader.cfg.strategies.crypto.stop_check_interval_minutes)
        )
        scheduler.add_job(
            trader.crypto_stop_check, CronTrigger(minute=f"*/{stop_interval}", timezone=et)
        )
    # Frequent heartbeat so the supervisor can detect a stalled scheduler.
    scheduler.add_job(trader.heartbeat, CronTrigger(minute="*", timezone=et))
    return scheduler


def load_default_config() -> Config:
    config_path = Path("config.yaml")
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / "config.yaml"
    return load_config(config_path if config_path.exists() else None)


# US regular equity session (ET): 09:30–16:00. The midpoint (12:45 ET) splits
# the day into halves for the startup catch-up decision.
_MARKET_TZ = ZoneInfo("US/Eastern")
_SESSION_OPEN = (9, 30)   # 09:30 ET
_SESSION_MID = (12, 45)   # midpoint of a 6.5h session
_SESSION_CLOSE = (16, 0)  # 16:00 ET


def _equity_session_phase(now: datetime | None = None) -> str:
    """Classify where 'now' falls in the US equity session (ET).

    Returns one of: ``"closed"`` (weekend or outside 09:30–16:00),
    ``"first_half"`` (09:30–12:45 ET), or ``"second_half"`` (12:45–16:00 ET).

    Note: this is a wall-clock check and does NOT account for market holidays
    or half-days. On a holiday the broker simply rejects/queues equity orders,
    so a spurious "first_half" only means a harmless no-op scan.
    """
    now = now.astimezone(_MARKET_TZ) if now else datetime.now(_MARKET_TZ)
    if now.weekday() >= 5:  # Sat/Sun
        return "closed"

    def _at(hm: tuple[int, int]) -> datetime:
        return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)

    if _at(_SESSION_OPEN) <= now < _at(_SESSION_MID):
        return "first_half"
    if _at(_SESSION_MID) <= now < _at(_SESSION_CLOSE):
        return "second_half"
    return "closed"


def _describe_next_runs(scheduler: BlockingScheduler) -> str:
    """Describe the current scheduler state in plain language.

    If the US equity market is open we say we're TRADING (and until when).
    Otherwise we report when the market next opens. APScheduler only populates
    ``next_run_time`` after ``start()``, so we ask the ``market_open`` trigger
    directly. Times are shown in US/Eastern.
    """
    tz = scheduler.timezone
    now = datetime.now(tz)
    phase = _equity_session_phase(now)

    if phase in ("first_half", "second_half"):
        close = now.replace(
            hour=_SESSION_CLOSE[0], minute=_SESSION_CLOSE[1], second=0, microsecond=0
        )
        left = close - now
        hours = int(left.total_seconds() // 3600)
        mins = int((left.total_seconds() % 3600) // 60)
        return (
            f"Market OPEN ({phase.replace('_', ' ')}) — trading equities + crypto. "
            f"Regular session closes at {close.strftime('%H:%M %Z')} "
            f"(in {hours}h {mins}m)."
        )

    # Market closed → report the next open (crypto keeps trading 24/7).
    for job in scheduler.get_jobs():
        if getattr(job.func, "__name__", job.id) != "market_open":
            continue
        try:
            nxt = job.trigger.get_next_fire_time(None, now)
        except Exception:
            nxt = None
        if nxt is None:
            break
        wait = nxt - now
        hours = int(wait.total_seconds() // 3600)
        mins = int((wait.total_seconds() % 3600) // 60)
        when = nxt.strftime("%a %Y-%m-%d %H:%M %Z")
        return (
            f"Equity market closed; crypto still trading 24/7. "
            f"Next equity open at {when} (in {hours}h {mins}m)."
        )
    return "Equity market closed; crypto still trading 24/7."


def main() -> None:
    cfg = load_default_config()
    setup_logging(cfg)

    trader = Trader(cfg)
    try:
        trader.startup()
    except BrokerAuthError as exc:
        logger.error("Broker not ready: %s", exc)
        sys.exit(2)

    scheduler = build_scheduler(trader)

    def graceful_shutdown(signum, frame):
        logger.info("Shutdown signal received")
        trader._stop_quote_stream()
        try:
            trader.broker.close()
        except Exception:
            pass
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, graceful_shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, graceful_shutdown)

    logger.info("Scheduler started. %s", _describe_next_runs(scheduler))
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Trader shut down.")


if __name__ == "__main__":
    main()
