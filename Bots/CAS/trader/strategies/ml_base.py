"""ML strategy scaffold — interface + stub for a model-driven strategy.

Machine-learning support is optional and disabled by default.  This module
provides a base class that handles bar fetching and signal packaging; subclasses
implement :meth:`predict` to turn features into a directional score in
[-1, 1].  A trained model (scikit-learn / any picklable predictor) can be loaded
from ``cfg.strategies.ml.model_path``.

Requires the optional ``ml`` extra (scikit-learn) only if you actually load a
model; the default stub returns no signals so the system runs without it.
"""

from __future__ import annotations

import logging
from abc import abstractmethod

from trader.brokers.base import BrokerClient
from trader.brokers.types import Bar
from trader.config import Config
from trader.strategies._bar_source import fetch_daily_bars
from trader.strategies.base import MarketRegime, Signal, SignalDirection, Strategy

logger = logging.getLogger(__name__)


class MLStrategyBase(Strategy):
    name = "ml_base"
    preferred_regimes = ()

    def __init__(self, broker: BrokerClient, cfg: Config, stock_symbols: list[str]) -> None:
        self.broker = broker
        self.cfg = cfg.strategies.ml
        self.stock_symbols = stock_symbols
        self._model = None
        if self.cfg.enabled and self.cfg.model_path:
            self._model = self._load_model(self.cfg.model_path)

    def _load_model(self, path: str):
        try:
            import pickle

            with open(path, "rb") as fh:
                model = pickle.load(fh)  # noqa: S301 - user-provided trusted model
            logger.info("Loaded ML model from %s", path)
            return model
        except Exception:
            logger.error("Failed to load ML model from %s", path, exc_info=True)
            return None

    @abstractmethod
    def predict(self, symbol: str, bars: list[Bar]) -> float:
        """Return a directional score in [-1, 1] for the symbol."""

    def scan(self) -> list[Signal]:
        if not self.cfg.enabled:
            return []
        bars_by_symbol = fetch_daily_bars(self.broker, self.stock_symbols, 120)
        scored: list[tuple[str, float]] = []
        for sym, bars in bars_by_symbol.items():
            try:
                score = self.predict(sym, bars)
            except NotImplementedError:
                return []
            except Exception:
                continue
            if abs(score) > 0.1:
                scored.append((sym, score))

        scored.sort(key=lambda kv: abs(kv[1]), reverse=True)
        signals = [
            Signal(
                symbol=sym,
                direction=SignalDirection.LONG if score > 0 else SignalDirection.SHORT,
                strength=min(1.0, abs(score)),
                strategy=self.name,
                reason=f"ml score={score:+.2f}",
                confidence=min(1.0, abs(score)),
                regime=MarketRegime.UNKNOWN,
            )
            for sym, score in scored[: self.cfg.top_n]
        ]
        logger.info("ML strategy produced %d signals", len(signals))
        return signals


class NullMLStrategy(MLStrategyBase):
    """Default no-op ML strategy (returns no signals)."""

    name = "ml"

    def predict(self, symbol: str, bars: list[Bar]) -> float:
        return 0.0
