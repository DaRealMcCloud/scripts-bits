"""On-disk cache of historical daily bars for the optimizer.

Downloading bars for the entire tradable universe is slow and rate-limited, so
we do it once and pickle the result. Subsequent optimizer runs load from the
cache, letting thousands of parameter evaluations reuse a single download.

The cache is a plain pickle of a :class:`BarCache` (a dict of symbol → sorted
list of neutral :class:`~trader.brokers.types.Bar`). No third-party format is
used, so there is no extra dependency.
"""

from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from trader.brokers.base import BrokerClient
from trader.brokers.types import Bar, TimeFrame
from trader.config import Config
from trader.data.universe import (
    _rank_by_volume,  # volume-ranked selection (see universe.py)
    _resolve_crypto,
    split_universe,
)

logger = logging.getLogger(__name__)

DEFAULT_CACHE_DIR = Path("data") / "backtest_cache"
CACHE_FORMAT_VERSION = 1


@dataclass
class BarCache:
    """A pickled bundle of historical bars plus the metadata to trust it."""

    bars: dict[str, list[Bar]] = field(default_factory=dict)
    start: datetime | None = None
    end: datetime | None = None
    created_at: datetime = field(default_factory=datetime.now)
    version: int = CACHE_FORMAT_VERSION

    # ── convenience accessors ────────────────────────────────
    @property
    def symbols(self) -> list[str]:
        return list(self.bars.keys())

    @property
    def stock_symbols(self) -> list[str]:
        return [s for s in self.bars if "/" not in s]

    @property
    def crypto_symbols(self) -> list[str]:
        return [s for s in self.bars if "/" in s]

    def subset(self, symbols: list[str]) -> dict[str, list[Bar]]:
        """Return the cached series for the given symbols (present ones only)."""
        return {s: self.bars[s] for s in symbols if s in self.bars}

    def summary(self) -> str:
        return (
            f"BarCache: {len(self.bars)} symbols "
            f"({len(self.stock_symbols)} equity / {len(self.crypto_symbols)} crypto), "
            f"window {self.start} → {self.end}, built {self.created_at:%Y-%m-%d %H:%M}"
        )


def _select_symbols(
    broker: BrokerClient,
    cfg: Config,
    *,
    max_symbols: int | None,
    include_crypto: bool,
    include_equities: bool,
) -> tuple[list[str], list[str]]:
    """Resolve the equity + crypto symbol lists to cache.

    Equities are **volume-ranked** (most liquid first) so a ``max_symbols`` cap
    keeps the most tradable names rather than an arbitrary slice — this reuses
    :func:`trader.data.universe._rank_by_volume`, the same fix applied to live
    universe building.
    """
    equities: list[str] = []
    if include_equities:
        seed = list(cfg.universe.stocks)
        tradable = broker.list_tradable_equities()
        all_stocks = [i.symbol for i in tradable] if tradable else list(seed)
        # De-dupe while preserving the seed first.
        for s in seed:
            if s not in all_stocks:
                all_stocks.append(s)
        if max_symbols and len(all_stocks) > max_symbols:
            logger.info(
                "Volume-ranking %d equities down to %d for the cache",
                len(all_stocks),
                max_symbols,
            )
            equities = _rank_by_volume(broker, all_stocks, cfg, top_n=max_symbols)
        else:
            equities = all_stocks
        for etf in cfg.universe.etfs:
            if etf not in equities:
                equities.append(etf)

    crypto: list[str] = []
    if include_crypto and broker.capabilities.supports_crypto:
        crypto = _resolve_crypto(broker, cfg)
        if max_symbols and len(crypto) > max_symbols:
            crypto = crypto[:max_symbols]

    return equities, crypto


def build_cache(
    broker: BrokerClient,
    cfg: Config,
    *,
    days: int = 1095,
    max_symbols: int | None = None,
    include_equities: bool = True,
    include_crypto: bool = True,
    chunk_size: int = 200,
) -> BarCache:
    """Download daily bars for the selected universe and return a BarCache.

    ``days`` is the trailing history window (default ~3 years). ``max_symbols``
    caps *each* asset class (equities are volume-ranked before the cap).
    """
    end = datetime.now()
    start = end - timedelta(days=days)

    equities, crypto = _select_symbols(
        broker,
        cfg,
        max_symbols=max_symbols,
        include_crypto=include_crypto,
        include_equities=include_equities,
    )
    all_symbols = equities + crypto
    logger.info(
        "Building bar cache: %d symbols (%d equity, %d crypto), %d-day window",
        len(all_symbols),
        len(equities),
        len(crypto),
        days,
    )

    bars: dict[str, list[Bar]] = {}
    for i in range(0, len(all_symbols), chunk_size):
        chunk = all_symbols[i : i + chunk_size]
        try:
            fetched = broker.get_bars(chunk, TimeFrame.DAY, start=start, end=end)
        except Exception as exc:  # pragma: no cover - network path
            logger.warning("Bar fetch failed for chunk at %d (%s), skipping", i, exc)
            continue
        for sym, sym_bars in fetched.items():
            if sym_bars:
                bars[sym] = sorted(sym_bars, key=lambda b: b.timestamp)
        logger.info("Fetched %d/%d symbols", min(i + chunk_size, len(all_symbols)), len(all_symbols))

    cache = BarCache(bars=bars, start=start, end=end)
    logger.info("%s", cache.summary())
    return cache


def save_cache(cache: BarCache, path: str | Path = DEFAULT_CACHE_DIR / "bars.pkl") -> Path:
    """Pickle a BarCache to disk (creating parent dirs)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("wb") as fh:
        pickle.dump(cache, fh, protocol=pickle.HIGHEST_PROTOCOL)
    logger.info("Saved bar cache to %s (%d symbols)", p, len(cache.bars))
    return p


def load_cache(path: str | Path = DEFAULT_CACHE_DIR / "bars.pkl") -> BarCache | None:
    """Load a pickled BarCache, or None if it is missing/unreadable."""
    p = Path(path)
    if not p.exists():
        logger.info("No bar cache at %s", p)
        return None
    try:
        with p.open("rb") as fh:
            cache = pickle.load(fh)
    except Exception as exc:
        logger.warning("Could not read bar cache %s: %s", p, exc)
        return None
    if not isinstance(cache, BarCache):
        logger.warning("Cache file %s is not a BarCache", p)
        return None
    if getattr(cache, "version", None) != CACHE_FORMAT_VERSION:
        logger.warning(
            "Cache %s has version %s (expected %s); rebuild recommended",
            p,
            getattr(cache, "version", None),
            CACHE_FORMAT_VERSION,
        )
    logger.info("Loaded bar cache: %s", cache.summary())
    return cache
