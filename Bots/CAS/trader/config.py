"""Configuration loader — reads config.yaml, overlays environment variables.

Secrets (Alpaca keys, IBKR settings) are read from the process environment.  To
make local development easy, a ``.env`` file is loaded automatically if
``python-dotenv`` is installed (see the optional ``env`` extra).  Real
environment variables always take precedence over ``.env`` values, which in turn
take precedence over anything inlined in ``config.yaml``.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _load_dotenv(path: str | Path | None = None) -> None:
    """Load a ``.env`` file into ``os.environ`` if python-dotenv is available.

    Looks for a ``.env`` next to the given config file first, then falls back to
    the current working directory.  Existing environment variables are NOT
    overridden (real env wins over the file).  Silently no-ops if python-dotenv
    is not installed.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.debug("python-dotenv not installed; skipping .env loading")
        return

    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path).resolve().parent / ".env")
    candidates.append(Path.cwd() / ".env")

    for env_path in candidates:
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)
            logger.info("Loaded environment from %s", env_path)
            return


@dataclass
class AlpacaConfig:
    mode: str = "paper"
    api_key: str = ""
    secret_key: str = ""
    data_feed: str = "iex"


@dataclass
class IBKRConfig:
    """Interactive Brokers Client Portal Gateway settings."""

    base_url: str = "https://localhost:5000/v1/api"
    account_id: str = ""
    verify_ssl: bool = False
    keepalive_interval: float = 45.0
    rate_per_sec: float = 9.0
    # Order-confirmation dialog ids to auto-suppress (see IBKR docs).
    suppress_message_ids: list[str] = field(
        default_factory=lambda: ["o354", "o163", "o382", "o451"]
    )


@dataclass
class BrokerConfig:
    """Selects the active broker adapter."""

    provider: str = "alpaca"  # "alpaca" | "ibkr"


@dataclass
class UniverseConfig:
    min_avg_volume: int = 500_000
    # Explicit equity symbols. Required for IBKR (no bulk asset listing);
    # optional for Alpaca (used in addition to the auto-discovered universe).
    stocks: list[str] = field(default_factory=list)
    etfs: list[str] = field(default_factory=lambda: ["GLD", "SLV", "USO", "XLE"])
    # Explicit crypto pairs, OR the single sentinel ["ALL/ALL"] to auto-discover
    # every tradable crypto pair from the broker (Alpaca only). When ALL/ALL is
    # used, discovery is restricted to pairs quoted in ``crypto_quote`` and, if
    # ``min_crypto_volume`` > 0, filtered by average daily volume.
    crypto: list[str] = field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    # Quote currency used to filter auto-discovered crypto (ALL/ALL only). Keeps
    # the universe clean by dropping USDT/USDC/BTC-quoted duplicates. Explicit
    # crypto lists are never filtered.
    crypto_quote: str = "USD"
    # Optional average-daily-volume floor applied ONLY to auto-discovered crypto
    # (ALL/ALL). 0 disables it. Note: crypto volume is in base-currency units, so
    # magnitudes differ from equity share volume — tune with care.
    min_crypto_volume: int = 0
    # When a broker exposes a very large bulk listing (e.g. 7000+ equities)
    # building volume history for every symbol can be slow on constrained
    # hardware (Raspberry Pi). Set `max_discovered` to a positive integer to
    # pre-filter the discovered list to the first N symbols before applying
    # the (expensive) volume filter. 0 means no pre-filtering.
    max_discovered: int = 500



@dataclass
class MomentumConfig:
    enabled: bool = True
    fast_lookback: int = 20
    slow_lookback: int = 60
    top_n: int = 10
    min_roe: float = 0.05
    max_debt_equity: float = 3.0
    min_gross_margin: float = 0.20


@dataclass
class GapFadeConfig:
    enabled: bool = True
    min_gap_pct: float = 2.0
    target_retracement: float = 0.5
    max_daily_trades: int = 5


@dataclass
class QuoteImbalanceConfig:
    enabled: bool = True
    window_size: int = 50
    min_score: float = 0.3


@dataclass
class TrendFollowingConfig:
    enabled: bool = False
    weight: float = 1.0
    fast_ma: int = 20
    slow_ma: int = 50
    adx_min: float = 20.0
    top_n: int = 10


@dataclass
class MeanReversionConfig:
    enabled: bool = False
    weight: float = 1.0
    lookback: int = 20
    zscore_entry: float = -2.0
    rsi_oversold: float = 30.0
    top_n: int = 10


@dataclass
class BreakoutConfig:
    enabled: bool = False
    weight: float = 1.0
    channel: int = 20
    top_n: int = 10


@dataclass
class VolatilityConfig:
    enabled: bool = False
    weight: float = 1.0
    lookback: int = 20
    # Long low-vol / short high-vol style bias.
    low_vol_pct: float = 0.15
    high_vol_pct: float = 0.50
    top_n: int = 5


@dataclass
class PairsTradingConfig:
    enabled: bool = False
    weight: float = 1.0
    lookback: int = 60
    zscore_entry: float = 2.0
    # List of "SYM_A/SYM_B" pairs.
    pairs: list[str] = field(default_factory=list)


@dataclass
class DcaConfig:
    enabled: bool = False
    weight: float = 1.0
    symbols: list[str] = field(default_factory=list)
    # e.g. buy on the 1st scheduled run of each week.
    cadence_days: int = 7


@dataclass
class RebalanceConfig:
    enabled: bool = False
    weight: float = 1.0
    # target weights, e.g. {"SPY": 0.6, "GLD": 0.2, "TLT": 0.2}
    targets: dict = field(default_factory=dict)
    drift_pct: float = 0.05


@dataclass
class MlConfig:
    enabled: bool = False
    weight: float = 1.0
    model_path: str = ""
    top_n: int = 10


@dataclass
class AggregatorConfig:
    enabled: bool = True
    top_n: int = 10
    min_score: float = 0.05
    use_regime_routing: bool = True
    regime_fit_bonus: float = 1.5
    regime_fit_penalty: float = 0.5
    # Benchmark symbol used to classify the overall market regime.
    regime_benchmark: str = "SPY"


@dataclass
class SentimentConfig:
    enabled: bool = False
    weight: float = 0.2


# ── Crypto-specific strategies (long-only, 24/7) ─────────────────────────────
@dataclass
class CryptoMomentumConfig:
    enabled: bool = True
    weight: float = 1.0
    fast_ma: int = 20
    slow_ma: int = 50
    adx_min: float = 20.0
    top_n: int = 10


@dataclass
class CryptoMeanReversionConfig:
    enabled: bool = False
    weight: float = 1.0
    lookback: int = 20
    zscore_entry: float = -2.0
    rsi_oversold: float = 30.0
    top_n: int = 10


@dataclass
class CryptoBreakoutConfig:
    enabled: bool = False
    weight: float = 1.0
    channel: int = 20
    top_n: int = 10


@dataclass
class CryptoDcaConfig:
    enabled: bool = False
    weight: float = 1.0
    symbols: list[str] = field(default_factory=list)
    # Accumulate on the first scan after this many hours have elapsed.
    cadence_hours: int = 24


@dataclass
class CryptoVolatilityConfig:
    enabled: bool = False
    weight: float = 1.0
    lookback: int = 20
    # Crypto realised vol is far higher than equities; thresholds reflect that.
    low_vol_pct: float = 0.40
    high_vol_pct: float = 1.20
    top_n: int = 5


@dataclass
class CryptoAggregatorConfig:
    enabled: bool = True
    top_n: int = 10
    min_score: float = 0.05
    use_regime_routing: bool = True
    regime_fit_bonus: float = 1.5
    regime_fit_penalty: float = 0.5
    # Benchmark used to classify the crypto market regime (distinct from equities).
    regime_benchmark: str = "BTC/USD"


@dataclass
class CryptoStrategiesConfig:
    """24/7 crypto trading — long-only, runs independently of equity hours."""

    # Master switch for the whole crypto trading loop.
    enabled: bool = False
    # How often the crypto opportunity scan runs (minutes), all 7 days.
    scan_interval_minutes: int = 15
    # How often open crypto positions are checked against their protective
    # stops / take-profit (minutes). Runs more often than the opportunity scan
    # so a fast drop can't wipe out a position between scans. Should be <=
    # scan_interval_minutes.
    stop_check_interval_minutes: int = 5
    # Cancel a resting (unfilled) crypto entry order once it is older than this
    # many minutes. Crypto orders are submitted GTC and are NOT touched by the
    # equity EOD flatten, so without a TTL a stuck order lingers forever and
    # (via the no-pyramiding exposure guard) blocks new entries for that symbol.
    # 0 disables the sweep (orders live until filled/cancelled manually).
    crypto_order_ttl_minutes: int = 30
    # Software trailing stop (Alpaca crypto has no broker-side stops).
    trailing_stop: bool = False
    trail_pct: float = 0.05
    momentum: CryptoMomentumConfig = field(default_factory=CryptoMomentumConfig)
    mean_reversion: CryptoMeanReversionConfig = field(
        default_factory=CryptoMeanReversionConfig
    )
    breakout: CryptoBreakoutConfig = field(default_factory=CryptoBreakoutConfig)
    dca: CryptoDcaConfig = field(default_factory=CryptoDcaConfig)
    volatility: CryptoVolatilityConfig = field(default_factory=CryptoVolatilityConfig)
    aggregator: CryptoAggregatorConfig = field(default_factory=CryptoAggregatorConfig)


@dataclass
class StrategiesConfig:
    momentum: MomentumConfig = field(default_factory=MomentumConfig)
    gap_fade: GapFadeConfig = field(default_factory=GapFadeConfig)
    quote_imbalance: QuoteImbalanceConfig = field(default_factory=QuoteImbalanceConfig)
    trend_following: TrendFollowingConfig = field(default_factory=TrendFollowingConfig)
    mean_reversion: MeanReversionConfig = field(default_factory=MeanReversionConfig)
    breakout: BreakoutConfig = field(default_factory=BreakoutConfig)
    volatility: VolatilityConfig = field(default_factory=VolatilityConfig)
    pairs_trading: PairsTradingConfig = field(default_factory=PairsTradingConfig)
    dca: DcaConfig = field(default_factory=DcaConfig)
    rebalance: RebalanceConfig = field(default_factory=RebalanceConfig)
    ml: MlConfig = field(default_factory=MlConfig)
    aggregator: AggregatorConfig = field(default_factory=AggregatorConfig)
    sentiment: SentimentConfig = field(default_factory=SentimentConfig)
    crypto: CryptoStrategiesConfig = field(default_factory=CryptoStrategiesConfig)


@dataclass
class RiskConfig:
    max_risk_per_trade: float = 0.01
    max_positions: int = 15
    # Separate cap for open crypto positions; 0 = unlimited.
    max_crypto_positions: int = 0
    max_daily_drawdown: float = 0.05
    atr_stop_multiplier: float = 2.0
    max_hold_days: int = 5
    flatten_eod: bool = True
    # When False (default), the EOD flatten leaves crypto positions untouched
    # so 24/7 crypto trades are not force-closed at the equity market close.
    flatten_crypto_eod: bool = False
    # ── Fee-aware edge gate (protects thin-margin crypto/scalp trades) ──
    # Per-side maker/taker fees (fractions). These are kept for clarity and
    # reference; the fee gate uses `taker_fee_pct * 2` when `taker_fee_pct` is
    # present. Set 0 to disable the fee gate entirely (no fee guard).
    # Default values reflect Alpaca Level 1 (maker 0.15%, taker 0.25%).
    maker_fee_pct: float = 0.0015
    taker_fee_pct: float = 0.0025
    # A trade is only taken if its expected favourable move (entry→stop distance,
    # used as a proxy for expected reward) exceeds round-trip fees by this factor.
    # 2.0 means the expected edge must be at least twice the fee drag.
    min_edge_multiple: float = 2.0
    # Optional take-profit target as a fraction above entry (0 = disabled).
    # Applies to crypto software exits (the "7" in the 3-5-7 rule ≈ 0.07).
    take_profit_pct: float = 0.0


@dataclass
class LoggingConfig:
    log_dir: str = "logs"
    level: str = "INFO"


@dataclass
class WebConfig:
    """Local web dashboard (FastAPI + Uvicorn)."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8787


@dataclass
class Config:
    alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)
    ibkr: IBKRConfig = field(default_factory=IBKRConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    strategies: StrategiesConfig = field(default_factory=StrategiesConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    web: WebConfig = field(default_factory=WebConfig)


def _build_dataclass(cls, data: dict | None):
    """Recursively build a dataclass from a dict, ignoring unknown keys.

    Uses ``typing.get_type_hints`` so nested dataclasses are resolved correctly
    even though ``from __future__ import annotations`` turns annotations into
    strings.
    """
    if data is None:
        return cls()
    import typing

    hints = typing.get_type_hints(cls)
    filtered = {}
    for f in cls.__dataclass_fields__:
        if f in data:
            ftype = hints.get(f, cls.__dataclass_fields__[f].type)
            # Handle nested dataclasses
            if isinstance(data[f], dict) and hasattr(ftype, "__dataclass_fields__"):
                filtered[f] = _build_dataclass(ftype, data[f])
            else:
                filtered[f] = data[f]
    return cls(**filtered)


def load_config(path: str | Path | None = None) -> Config:
    """Load configuration from YAML file, overlay with environment variables."""
    # Load .env (if present + python-dotenv installed) before reading env vars.
    _load_dotenv(path)

    raw: dict = {}
    if path is not None:
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh) or {}

    cfg = _build_dataclass(Config, raw)

    # Environment variables always override file values
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if api_key:
        cfg.alpaca.api_key = api_key
    if secret_key:
        cfg.alpaca.secret_key = secret_key

    ibkr_account = os.environ.get("IBKR_ACCOUNT_ID", "")
    ibkr_base = os.environ.get("IBKR_BASE_URL", "")
    if ibkr_account:
        cfg.ibkr.account_id = ibkr_account
    if ibkr_base:
        cfg.ibkr.base_url = ibkr_base

    provider = os.environ.get("BROKER_PROVIDER", "") or cfg.broker.provider
    cfg.broker.provider = provider.lower()

    # Provider-specific validation.
    if cfg.broker.provider == "alpaca":
        if not cfg.alpaca.api_key or not cfg.alpaca.secret_key:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set as environment "
                "variables or in config.yaml when broker.provider is 'alpaca'"
            )
    elif cfg.broker.provider == "ibkr":
        # account_id is optional (auto-discovered), gateway login is external.
        pass
    else:
        raise ValueError(
            f"Unknown broker.provider '{cfg.broker.provider}'. Use 'alpaca' or 'ibkr'."
        )

    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Serialization (write a Config back to a dict / YAML)
# ─────────────────────────────────────────────────────────────────────────────
# Secrets are never written to disk. These keys are stripped from any serialized
# output so an optimizer / config dump can be committed or shared safely.
_SECRET_PATHS: frozenset[tuple[str, str]] = frozenset(
    {
        ("alpaca", "api_key"),
        ("alpaca", "secret_key"),
    }
)


def config_to_dict(cfg: Config, *, redact_secrets: bool = True) -> dict:
    """Convert a :class:`Config` (nested dataclasses) into a plain dict.

    Suitable for ``yaml.safe_dump``. When ``redact_secrets`` is True (default)
    the Alpaca API/secret keys are omitted so the result can be written to a
    file safely.
    """
    from dataclasses import asdict

    data = asdict(cfg)
    if redact_secrets:
        for section, key in _SECRET_PATHS:
            if section in data and isinstance(data[section], dict):
                data[section].pop(key, None)
    return data


def dump_config(cfg: Config, path: str | Path, *, redact_secrets: bool = True) -> None:
    """Write a :class:`Config` to a YAML file.

    Secrets are redacted by default (see :func:`config_to_dict`). Parent
    directories are created as needed.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = config_to_dict(cfg, redact_secrets=redact_secrets)
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False)
    logger.info("Wrote config to %s", p)