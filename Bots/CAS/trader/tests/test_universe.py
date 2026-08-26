"""Tests for the universe builder, focused on crypto discovery (ALL/ALL)."""

from __future__ import annotations

from dataclasses import replace

from trader.brokers.types import AssetClass, Instrument
from trader.config import Config
from trader.data.universe import CRYPTO_ALL_SENTINEL, build_universe

from .conftest import FakeBroker, make_bars


def _crypto(symbol: str, tradable: bool = True) -> Instrument:
    return Instrument(
        symbol=symbol,
        broker_id=symbol,
        asset_class=AssetClass.CRYPTO,
        tradable=tradable,
    )


def _cfg(**universe_overrides) -> Config:
    cfg = Config()
    cfg.universe = replace(cfg.universe, **universe_overrides)
    # Keep the universe small/deterministic: no stocks, no etfs unless set.
    return cfg


def test_explicit_crypto_list_used_verbatim():
    broker = FakeBroker(crypto_assets=[_crypto("BTC/USD"), _crypto("DOGE/USD")])
    cfg = _cfg(stocks=[], etfs=[], crypto=["BTC/USD", "ETH/USD"])
    universe = build_universe(broker, cfg)
    crypto = [s for s in universe if "/" in s]
    # Explicit list is honoured as-is; discovery is NOT triggered.
    assert crypto == ["BTC/USD", "ETH/USD"]


def test_all_all_discovers_every_pair():
    broker = FakeBroker(
        crypto_assets=[_crypto("BTC/USD"), _crypto("ETH/USD"), _crypto("SOL/USD")]
    )
    cfg = _cfg(stocks=[], etfs=[], crypto=[CRYPTO_ALL_SENTINEL])
    universe = build_universe(broker, cfg)
    crypto = sorted(s for s in universe if "/" in s)
    assert crypto == ["BTC/USD", "ETH/USD", "SOL/USD"]


def test_all_all_is_case_insensitive():
    broker = FakeBroker(crypto_assets=[_crypto("BTC/USD"), _crypto("ETH/USD")])
    cfg = _cfg(stocks=[], etfs=[], crypto=["all/all"])
    universe = build_universe(broker, cfg)
    crypto = sorted(s for s in universe if "/" in s)
    assert crypto == ["BTC/USD", "ETH/USD"]


def test_all_all_filters_by_quote_currency():
    broker = FakeBroker(
        crypto_assets=[
            _crypto("BTC/USD"),
            _crypto("ETH/USD"),
            _crypto("BTC/USDT"),
            _crypto("ETH/USDC"),
        ]
    )
    cfg = _cfg(stocks=[], etfs=[], crypto=[CRYPTO_ALL_SENTINEL], crypto_quote="USD")
    universe = build_universe(broker, cfg)
    crypto = sorted(s for s in universe if "/" in s)
    # Only /USD pairs survive; USDT/USDC duplicates are dropped.
    assert crypto == ["BTC/USD", "ETH/USD"]


def test_all_all_excludes_stablecoins():
    broker = FakeBroker(
        crypto_assets=[
            _crypto("BTC/USD"),
            _crypto("ETH/USD"),
            _crypto("USDC/USD"),
            _crypto("USDT/USD"),
            _crypto("USDG/USD"),
            _crypto("DAI/USD"),
        ]
    )
    cfg = _cfg(stocks=[], etfs=[], crypto=[CRYPTO_ALL_SENTINEL], crypto_quote="USD")
    universe = build_universe(broker, cfg)
    crypto = sorted(s for s in universe if "/" in s)
    # USD-pegged stablecoins are filtered out; only real assets remain.
    assert crypto == ["BTC/USD", "ETH/USD"]


def test_all_all_non_usd_quote():
    broker = FakeBroker(
        crypto_assets=[_crypto("BTC/USD"), _crypto("ETH/USDT"), _crypto("SOL/USDT")]
    )
    cfg = _cfg(stocks=[], etfs=[], crypto=[CRYPTO_ALL_SENTINEL], crypto_quote="USDT")
    universe = build_universe(broker, cfg)
    crypto = sorted(s for s in universe if "/" in s)
    assert crypto == ["ETH/USDT", "SOL/USDT"]


def test_all_all_volume_filter():
    broker = FakeBroker(
        bars={
            "BTC/USD": make_bars("BTC/USD", [100.0] * 30, volume=5_000.0),
            "TINY/USD": make_bars("TINY/USD", [1.0] * 30, volume=10.0),
        },
        crypto_assets=[_crypto("BTC/USD"), _crypto("TINY/USD")],
    )
    cfg = _cfg(
        stocks=[],
        etfs=[],
        crypto=[CRYPTO_ALL_SENTINEL],
        crypto_quote="USD",
        min_crypto_volume=1_000,
    )
    universe = build_universe(broker, cfg)
    crypto = sorted(s for s in universe if "/" in s)
    # Only the high-volume pair clears the 1000-unit floor.
    assert crypto == ["BTC/USD"]


def test_all_all_defaults_to_top_100_by_volume():
    symbols = [f"COIN{i:03d}/USD" for i in range(101)]
    bars = {
        symbol: make_bars(symbol, [100.0] * 30, volume=index + 1)
        for index, symbol in enumerate(symbols)
    }
    broker = FakeBroker(
        bars=bars,
        crypto_assets=[_crypto(symbol) for symbol in symbols],
    )
    cfg = _cfg(stocks=[], etfs=[], crypto=[CRYPTO_ALL_SENTINEL])
    universe = build_universe(broker, cfg)
    crypto = [s for s in universe if "/" in s]
    assert len(crypto) == 100
    assert crypto[0] == "COIN100/USD"
    assert "COIN000/USD" not in crypto


def test_no_crypto_capability_adds_nothing_even_with_all_all():
    from trader.brokers.base import BrokerCapabilities, InstrumentIdKind

    caps = BrokerCapabilities(
        name="fake-nocrypto",
        id_kind=InstrumentIdKind.SYMBOL,
        supports_crypto=False,
    )
    broker = FakeBroker(caps=caps, crypto_assets=[_crypto("BTC/USD")])
    cfg = _cfg(stocks=[], etfs=[], crypto=[CRYPTO_ALL_SENTINEL])
    universe = build_universe(broker, cfg)
    crypto = [s for s in universe if "/" in s]
    assert crypto == []
