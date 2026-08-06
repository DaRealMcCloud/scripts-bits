"""Technical indicators.

Thin wrappers over the ``ta`` library (MIT) when available, with small
pure-Python fallbacks so the analysis layer works even if ``ta``/pandas are not
installed.  All functions accept a sequence of :class:`trader.brokers.types.Bar`
and return plain floats/lists — no vendor types leak out.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from trader.brokers.types import Bar


def _closes(bars: Sequence[Bar]) -> list[float]:
    return [b.close for b in sorted(bars, key=lambda b: b.timestamp)]


def sma(bars: Sequence[Bar], period: int = 20) -> float | None:
    closes = _closes(bars)
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def ema(bars: Sequence[Bar], period: int = 20) -> float | None:
    closes = _closes(bars)
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    e = closes[0]
    for c in closes[1:]:
        e = c * k + e * (1 - k)
    return e


def rsi(bars: Sequence[Bar], period: int = 14) -> float | None:
    closes = _closes(bars)
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(bars: Sequence[Bar], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    closes = _closes(bars)
    if len(closes) < slow + signal:
        return None

    def _ema_series(values: list[float], p: int) -> list[float]:
        k = 2 / (p + 1)
        out = [values[0]]
        for v in values[1:]:
            out.append(v * k + out[-1] * (1 - k))
        return out

    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow, strict=False)]
    signal_line = _ema_series(macd_line, signal)
    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1],
        "hist": macd_line[-1] - signal_line[-1],
    }


def bollinger(bars: Sequence[Bar], period: int = 20, num_std: float = 2.0) -> dict | None:
    closes = _closes(bars)
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    std = math.sqrt(var)
    return {"upper": mid + num_std * std, "middle": mid, "lower": mid - num_std * std}


def realized_vol(bars: Sequence[Bar], period: int = 20) -> float | None:
    """Annualised realized volatility from daily log returns."""
    closes = _closes(bars)
    if len(closes) < period + 1:
        return None
    rets = [
        math.log(closes[i] / closes[i - 1])
        for i in range(len(closes) - period, len(closes))
        if closes[i - 1] > 0
    ]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(252)


def adx(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Average Directional Index — trend strength (0-100)."""
    ordered = sorted(bars, key=lambda b: b.timestamp)
    if len(ordered) < period * 2:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(ordered)):
        up = ordered[i].high - ordered[i - 1].high
        down = ordered[i - 1].low - ordered[i].low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr = max(
            ordered[i].high - ordered[i].low,
            abs(ordered[i].high - ordered[i - 1].close),
            abs(ordered[i].low - ordered[i - 1].close),
        )
        trs.append(tr)
    atr = sum(trs[-period:]) / period
    if atr == 0:
        return None
    pdi = 100 * (sum(plus_dm[-period:]) / period) / atr
    mdi = 100 * (sum(minus_dm[-period:]) / period) / atr
    denom = pdi + mdi
    if denom == 0:
        return None
    return 100 * abs(pdi - mdi) / denom


def zscore(bars: Sequence[Bar], period: int = 20) -> float | None:
    """Z-score of the latest close vs its trailing mean/std."""
    closes = _closes(bars)
    if len(closes) < period:
        return None
    window = closes[-period:]
    mean = sum(window) / period
    var = sum((c - mean) ** 2 for c in window) / period
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (closes[-1] - mean) / std


def donchian(bars: Sequence[Bar], period: int = 20) -> dict | None:
    ordered = sorted(bars, key=lambda b: b.timestamp)
    if len(ordered) < period:
        return None
    window = ordered[-period:]
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    return {"upper": hi, "lower": lo, "middle": (hi + lo) / 2}
