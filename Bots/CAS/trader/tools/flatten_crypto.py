"""One-off maintenance script: flatten ALL crypto positions for a clean restart.

Closes every open crypto position and cancels every open crypto order at the
broker, then clears the crypto software-stops from the local state DB. Equity
positions and orders are left untouched.

Use this to reset the crypto book after changing trading rules (fee gate,
3-5-7 limits, stablecoin filter, take-profit) so new entries start fresh.

SAFETY
------
* Runs as a DRY RUN by default — it only prints what it would do.
* Pass ``--yes`` to actually close positions and cancel orders.
* STOP the bot first (``trader.supervisor`` / ``trader.main``). If the bot is
  running it may immediately re-buy after you flatten.

Usage (from the CAS directory)::

    python -m trader.tools.flatten_crypto            # dry run (shows plan)
    python -m trader.tools.flatten_crypto --yes      # actually flatten
"""

from __future__ import annotations

import argparse
import logging
import sys

from trader.brokers.factory import make_broker
from trader.execution.state_store import StateStore
from trader.main import load_default_config
from trader.strategies.base import is_crypto_symbol

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)-7s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("flatten_crypto")


def main() -> int:
    parser = argparse.ArgumentParser(description="Flatten all crypto positions.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually close positions/cancel orders (otherwise dry run).",
    )
    args = parser.parse_args()
    execute = args.yes

    cfg = load_default_config()
    broker = make_broker(cfg)
    broker.connect()

    try:
        positions = broker.get_positions()
        crypto_positions = [p for p in positions if is_crypto_symbol(p.symbol)]

        try:
            open_orders = broker.get_open_orders()
        except Exception:
            logger.warning("Could not read open orders", exc_info=True)
            open_orders = []
        crypto_orders = [o for o in open_orders if is_crypto_symbol(o.symbol)]

        mode = "EXECUTE" if execute else "DRY RUN"
        logger.info("=== Flatten crypto (%s) ===", mode)
        logger.info(
            "Found %d crypto position(s) and %d open crypto order(s)",
            len(crypto_positions),
            len(crypto_orders),
        )

        if not crypto_positions and not crypto_orders:
            logger.info("Nothing to do — no open crypto positions or orders.")
            return 0

        # 1. Cancel resting crypto orders first so they don't re-fill.
        for o in crypto_orders:
            if execute:
                try:
                    broker.cancel_order(o.order_id)
                    logger.info("Cancelled order %s (%s)", o.order_id, o.symbol)
                except Exception:
                    logger.error("Failed to cancel order %s", o.order_id, exc_info=True)
            else:
                logger.info("[dry run] Would cancel order %s (%s)", o.order_id, o.symbol)

        # 2. Close each crypto position.
        for p in crypto_positions:
            if execute:
                try:
                    if broker.close_position(p.symbol):
                        logger.info("Closed position %s (qty=%s)", p.symbol, p.qty)
                    else:
                        logger.error("Failed to close %s (broker rejected)", p.symbol)
                except Exception:
                    logger.error("Failed to close %s", p.symbol, exc_info=True)
            else:
                logger.info("[dry run] Would close %s (qty=%s)", p.symbol, p.qty)

        # 3. Clear crypto software-stops from the local state DB.
        state = StateStore()
        try:
            stops = state.get_crypto_stops()
            for sym in list(stops):
                if execute:
                    state.delete_crypto_stop(sym)
                    logger.info("Cleared software stop for %s", sym)
                else:
                    logger.info("[dry run] Would clear software stop for %s", sym)
        finally:
            state.close()

        if not execute:
            logger.info("Dry run complete. Re-run with --yes to apply.")
        else:
            logger.info("Crypto book flattened. Safe to restart the bot.")
        return 0
    finally:
        try:
            broker.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
