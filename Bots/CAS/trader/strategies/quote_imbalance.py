"""NBBO quote imbalance scoring for entry timing."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from trader.config import Config
from trader.strategies.base import Signal, SignalDirection

logger = logging.getLogger(__name__)


@dataclass
class ImbalanceTracker:
    """Tracks rolling bid/ask size imbalance for a single symbol."""

    symbol: str
    window_size: int = 50
    _bid_sizes: deque = field(default_factory=deque)
    _ask_sizes: deque = field(default_factory=deque)

    def update(self, bid_size: float, ask_size: float) -> None:
        self._bid_sizes.append(bid_size)
        self._ask_sizes.append(ask_size)
        if len(self._bid_sizes) > self.window_size:
            self._bid_sizes.popleft()
            self._ask_sizes.popleft()

    @property
    def score(self) -> float:
        """Imbalance score: +1 = all buying pressure, -1 = all selling pressure."""
        if len(self._bid_sizes) < 5:
            return 0.0
        total_bid = sum(self._bid_sizes)
        total_ask = sum(self._ask_sizes)
        denom = total_bid + total_ask
        if denom == 0:
            return 0.0
        return (total_bid - total_ask) / denom

    @property
    def ready(self) -> bool:
        return len(self._bid_sizes) >= self.window_size


class QuoteImbalanceScorer:
    """Manages imbalance trackers for multiple symbols and emits timing signals."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg.strategies.quote_imbalance
        self._trackers: dict[str, ImbalanceTracker] = {}

    def get_tracker(self, symbol: str) -> ImbalanceTracker:
        if symbol not in self._trackers:
            self._trackers[symbol] = ImbalanceTracker(
                symbol=symbol, window_size=self.cfg.window_size
            )
        return self._trackers[symbol]

    def on_quote(self, symbol: str, bid_size: float, ask_size: float) -> None:
        """Feed a new NBBO quote into the tracker."""
        tracker = self.get_tracker(symbol)
        tracker.update(bid_size, ask_size)

    def check_entry_timing(self, symbol: str, desired_direction: SignalDirection) -> bool:
        """Check if current imbalance supports the desired trade direction."""
        if not self.cfg.enabled:
            return True  # pass-through if disabled

        tracker = self.get_tracker(symbol)
        if not tracker.ready:
            return True  # not enough data yet, allow entry

        score = tracker.score
        threshold = self.cfg.min_score

        if desired_direction == SignalDirection.LONG and score >= threshold:
            return True  # buying pressure supports going long
        if desired_direction == SignalDirection.SHORT and score <= -threshold:
            return True  # selling pressure supports going short

        logger.debug(
            "Imbalance timing rejected %s %s (score=%.2f, threshold=%.2f)",
            symbol,
            desired_direction.value,
            score,
            threshold,
        )
        return False

    def get_score(self, symbol: str) -> float:
        return self.get_tracker(symbol).score
