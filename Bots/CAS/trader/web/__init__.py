"""Local web dashboard for the trading bot.

Optional feature — requires the ``web`` extra::

    pip install 'alpaca-trader[web]'

The dashboard is hosted inside the supervisor process (see
``trader.supervisor._maybe_start_web``) so it stays reachable even while the
trader child is restarting.  It binds to ``127.0.0.1`` by default; never expose
it to a public interface without an authenticating reverse proxy in front.
"""

from __future__ import annotations

__all__ = ["run_web"]


def run_web(*args, **kwargs):  # pragma: no cover - thin lazy shim
    """Lazy proxy to :func:`trader.web.app.run_web` (defers FastAPI import)."""
    from trader.web.app import run_web as _run_web

    return _run_web(*args, **kwargs)
