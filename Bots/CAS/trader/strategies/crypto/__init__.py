"""Crypto-specific, long-only trading strategies (24/7).

Crypto on Alpaca is spot/cash only:
- No shorting, no margin, no broker-side stop or bracket orders.
- These strategies therefore emit LONG signals ONLY and set
  ``Signal.asset_class = AssetClass.CRYPTO``.
- Protective exits are enforced in software by the crypto scan loop.
"""

from trader.strategies.crypto.crypto_breakout import CryptoBreakoutStrategy
from trader.strategies.crypto.crypto_dca import CryptoDcaStrategy
from trader.strategies.crypto.crypto_mean_reversion import CryptoMeanReversionStrategy
from trader.strategies.crypto.crypto_momentum import CryptoMomentumStrategy
from trader.strategies.crypto.crypto_volatility import CryptoVolatilityStrategy

__all__ = [
    "CryptoMomentumStrategy",
    "CryptoMeanReversionStrategy",
    "CryptoBreakoutStrategy",
    "CryptoDcaStrategy",
    "CryptoVolatilityStrategy",
]
