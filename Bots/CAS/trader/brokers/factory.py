"""Broker factory — builds a :class:`BrokerClient` from configuration.

Adapters are imported lazily so that using one broker does not require the
other's dependencies to be installed (e.g. Alpaca users need not have the IBKR
gateway/``requests`` stack exercised, and vice-versa).
"""

from __future__ import annotations

import logging

from trader.brokers.base import BrokerClient

logger = logging.getLogger(__name__)


def make_broker(cfg) -> BrokerClient:
    """Instantiate the broker selected by ``cfg.broker.provider``.

    The returned client is *not* yet connected; call ``.connect()`` before use.
    """
    provider = cfg.broker.provider.lower()

    if provider == "alpaca":
        from trader.brokers.alpaca import AlpacaBroker

        logger.info("Selected broker provider: alpaca")
        return AlpacaBroker(cfg)

    if provider == "ibkr":
        from trader.brokers.ibkr import IBKRBroker

        logger.info("Selected broker provider: ibkr")
        return IBKRBroker(cfg)

    raise ValueError(f"Unknown broker provider '{provider}'. Use 'alpaca' or 'ibkr'.")
