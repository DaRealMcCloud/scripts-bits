"""Low-level IBKR Client Portal Gateway session.

The IBKR Web API is exposed by the *Client Portal Gateway* (a local Java
process, typically at ``https://localhost:5000/v1/api``).  It uses a
self-signed certificate and requires:

- an authenticated brokerage session (user logs in via the gateway web page),
- a periodic ``POST /tickle`` keepalive (session expires after ~60s idle),
- confirmation replies for order "questions" (POST /iserver/reply/{id}).

This module wraps those mechanics: TLS-relaxed session, keepalive thread,
rate limiting (IBKR caps at ~10 req/s), and retry/backoff.  It exposes generic
``get``/``post``/``delete`` helpers used by :class:`trader.brokers.ibkr.IBKRBroker`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from trader.brokers._http import RateLimiter, RateLimitError, TransientError, with_retry

logger = logging.getLogger(__name__)


class IBKRSession:
    """Manages the authenticated HTTP session to the CP Gateway."""

    def __init__(
        self,
        base_url: str = "https://localhost:5000/v1/api",
        verify_ssl: bool = False,
        keepalive_interval: float = 45.0,
        rate_per_sec: float = 9.0,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        self.keepalive_interval = keepalive_interval
        self.timeout = timeout
        self._rate = RateLimiter(rate_per_sec)

        self._session: Any = None  # requests.Session, created on connect
        self._keepalive_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._authenticated = False

    # ── Connection / auth ────────────────────────────────────
    def connect(self) -> None:
        import requests

        if not self.verify_ssl:
            # CP Gateway ships a self-signed cert; silence the warning noise.
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._session = requests.Session()
        self._session.verify = self.verify_ssl
        self._session.headers.update({"User-Agent": "alpaca-trader/ibkr", "Accept": "application/json"})

        self._ensure_authenticated()
        self._start_keepalive()

    def _ensure_authenticated(self) -> None:
        status = self.auth_status()
        authed = bool(status.get("authenticated"))
        connected = bool(status.get("connected", True))
        if not authed:
            raise RuntimeError(
                "IBKR gateway is not authenticated. Log in via the Client Portal "
                "Gateway web page (default https://localhost:5000) before starting."
            )
        if not connected:
            logger.warning("IBKR session authenticated but not connected to competing session")
        self._authenticated = True
        logger.info("IBKR session authenticated")

    def auth_status(self) -> dict:
        """POST /iserver/auth/status — reports session auth state."""
        try:
            return self.post("/iserver/auth/status", json={}) or {}
        except Exception as exc:  # pragma: no cover - network dependent
            logger.error("IBKR auth status check failed: %s", exc)
            return {"authenticated": False, "connected": False}

    def reauthenticate(self) -> None:
        """Attempt to re-validate a brokerage session."""
        try:
            self.post("/iserver/reauthenticate", json={})
        except Exception:
            logger.warning("IBKR reauthenticate request failed", exc_info=True)

    # ── Keepalive ────────────────────────────────────────────
    def _start_keepalive(self) -> None:
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            return
        self._stop.clear()

        def _loop():
            while not self._stop.wait(self.keepalive_interval):
                try:
                    self.tickle()
                except Exception:
                    logger.warning("IBKR tickle failed; attempting reauth", exc_info=True)
                    self.reauthenticate()

        self._keepalive_thread = threading.Thread(target=_loop, daemon=True, name="ibkr-keepalive")
        self._keepalive_thread.start()

    def tickle(self) -> dict:
        """POST /tickle — keeps the session alive (must run < 60s intervals)."""
        return self.post("/tickle", json={}) or {}

    def is_authenticated(self) -> bool:
        return self._authenticated

    def close(self) -> None:
        self._stop.set()
        if self._session is not None:
            try:
                self.post("/logout", json={})
            except Exception:
                pass
            self._session.close()

    # ── Generic HTTP helpers (rate-limited + retried) ────────
    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @with_retry(max_attempts=4, base_delay=0.5)
    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    @with_retry(max_attempts=4, base_delay=0.5)
    def post(self, path: str, json: dict | list | None = None) -> Any:
        return self._request("POST", path, json=json)

    @with_retry(max_attempts=4, base_delay=0.5)
    def delete(self, path: str, params: dict | None = None) -> Any:
        return self._request("DELETE", path, params=params)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        import requests

        if self._session is None:
            raise RuntimeError("IBKR session not connected; call connect() first")

        self._rate.acquire()
        try:
            resp = self._session.request(
                method, self._url(path), timeout=self.timeout, **kwargs
            )
        except requests.exceptions.RequestException as exc:
            raise TransientError(f"{method} {path} network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"429 rate limited on {path}")
        if resp.status_code >= 500:
            raise TransientError(f"{resp.status_code} server error on {path}")
        if resp.status_code == 401:
            self._authenticated = False
            raise RuntimeError(f"401 unauthorized on {path}; session expired")
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} error on {path}: {resp.text[:300]}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text
