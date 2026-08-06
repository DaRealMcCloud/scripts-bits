"""Decision-support / analysis layer.

- ``indicators`` — technical indicators (SMA/EMA/RSI/MACD/Bollinger/ADX/…).
- ``regime`` — market regime classification.
- ``aggregator`` — combine + rank signals across strategies (the trade brain).
- ``exposure`` — concentration/sector caps feeding the risk manager.
- ``sentiment`` — pluggable sentiment interface (no-op default).
"""

from __future__ import annotations

from trader.analysis.aggregator import ScoredSignal, SignalAggregator
from trader.analysis.exposure import ExposureManager
from trader.analysis.regime import RegimeClassifier
from trader.analysis.sentiment import (
    NullSentimentProvider,
    SentimentProvider,
    apply_sentiment_to_confidence,
)

__all__ = [
    "ExposureManager",
    "NullSentimentProvider",
    "RegimeClassifier",
    "ScoredSignal",
    "SentimentProvider",
    "SignalAggregator",
    "apply_sentiment_to_confidence",
]
