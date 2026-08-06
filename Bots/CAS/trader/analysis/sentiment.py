"""Sentiment analysis — pluggable interface with a no-op default.

Sentiment is optional and disabled by default.  Provide a concrete
:class:`SentimentProvider` (e.g. news/social APIs, an LLM classifier) and wire
it in via config to bias signal confidence.  The default
:class:`NullSentimentProvider` returns neutral (0.0) so the rest of the system
behaves identically when sentiment is off.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class SentimentProvider(ABC):
    """Return a sentiment score in [-1.0, +1.0] for a symbol."""

    @abstractmethod
    def score(self, symbol: str) -> float:
        ...

    def score_many(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.score(s) for s in symbols}


class NullSentimentProvider(SentimentProvider):
    """Default provider: always neutral."""

    def score(self, symbol: str) -> float:
        return 0.0


def apply_sentiment_to_confidence(
    confidence: float, sentiment: float, weight: float = 0.2
) -> float:
    """Nudge a signal's confidence by sentiment, clamped to [0, 1].

    ``sentiment`` in [-1, 1]; positive sentiment raises confidence for longs.
    """
    adjusted = confidence + weight * sentiment
    return max(0.0, min(1.0, adjusted))
