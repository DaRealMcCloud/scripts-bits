"""Supervisor / watchdog for the trading process.

Runs the trader as a child process and keeps it alive:

- restarts the child if it exits unexpectedly (with exponential backoff),
- restarts the child if the heartbeat file goes stale (a stalled scheduler),
- exposes runtime status the web dashboard reads (pid, uptime, last heartbeat),
- optionally hosts the FastAPI web dashboard in the supervisor process, so the
  dashboard stays reachable even while the trader child is restarting.

Pure-Python (``subprocess`` + a monitor thread); no external process manager
required.  Works on Windows and POSIX.

Run with::

    python -m trader.supervisor
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from trader.config import Config, load_config
from trader.logger import setup_logging
from trader.main import HEARTBEAT_FILE, load_default_config

logger = logging.getLogger(__name__)

STATUS_FILE = Path(os.environ.get("TRADER_STATUS", "data/supervisor_status.json"))


class Supervisor:
    """Parent watchdog that manages a single trader child process."""

    def __init__(
        self,
        cfg: Config,
        heartbeat_stale_seconds: float = 900.0,
        max_backoff: float = 300.0,
        poll_interval: float = 5.0,
    ) -> None:
        self.cfg = cfg
        self.heartbeat_stale = heartbeat_stale_seconds
        self.max_backoff = max_backoff
        self.poll_interval = poll_interval

        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._restarts = 0
        self._child_started_at: float | None = None
        self._started_at = time.time()
        self._backoff = 1.0

    # ── Child lifecycle ──────────────────────────────────────
    def _spawn_child(self) -> None:
        # Fresh heartbeat so a slow startup doesn't trip the stale check.
        self._touch_heartbeat()
        env = dict(os.environ)
        cmd = [sys.executable, "-m", "trader.main"]
        logger.info("Supervisor launching child: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, env=env)  # noqa: S603
        self._child_started_at = time.time()

    def _touch_heartbeat(self) -> None:
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        except Exception:
            logger.debug("Supervisor could not touch heartbeat", exc_info=True)

    def _heartbeat_age(self) -> float:
        try:
            ts = datetime.fromisoformat(HEARTBEAT_FILE.read_text(encoding="utf-8").strip())
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            return float("inf")

    def _kill_child(self) -> None:
        if self._proc and self._proc.poll() is None:
            logger.warning("Supervisor terminating child pid=%s", self._proc.pid)
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            except Exception:
                logger.error("Failed to terminate child", exc_info=True)

    # ── Status (for the web dashboard) ───────────────────────
    def write_status(self, competing_session: bool = False) -> None:
        pid = self._proc.pid if self._proc and self._proc.poll() is None else None
        uptime = time.time() - self._child_started_at if self._child_started_at and pid else 0.0
        status = {
            "running": pid is not None,
            "pid": pid,
            "uptime_seconds": round(uptime, 1),
            # Survives child restarts, which reset uptime_seconds to zero.
            "supervisor_uptime_seconds": round(time.time() - self._started_at, 1),
            "restarts": self._restarts,
            "last_heartbeat_age_seconds": round(self._heartbeat_age(), 1),
            "broker_provider": self.cfg.broker.provider,
            "competing_session": competing_session,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATUS_FILE.write_text(json.dumps(status, indent=2), encoding="utf-8")
        except Exception:
            logger.debug("Failed to write supervisor status", exc_info=True)

    # ── Control (used by web dashboard endpoints) ────────────
    def request_restart(self) -> None:
        logger.info("Restart requested via control channel")
        self._kill_child()

    def request_stop(self) -> None:
        logger.info("Stop requested via control channel")
        self._stop.set()
        self._kill_child()

    # ── Main loop ────────────────────────────────────────────
    def run(self) -> None:
        self._spawn_child()
        while not self._stop.is_set():
            time.sleep(self.poll_interval)
            self.write_status()

            if self._proc is None:
                continue

            exited = self._proc.poll()
            stale = self._heartbeat_age() > self.heartbeat_stale

            if exited is not None:
                logger.warning("Child exited with code %s", exited)
                if exited == 2:
                    # Exit code 2 == broker not authenticated (see main.py).
                    logger.error(
                        "Broker session not ready (e.g. IBKR gateway not logged in). "
                        "Backing off before retry."
                    )
                self._restart_child()
            elif stale:
                logger.error(
                    "Heartbeat stale (%.0fs > %.0fs); scheduler likely stalled. Restarting.",
                    self._heartbeat_age(),
                    self.heartbeat_stale,
                )
                self._kill_child()
                self._restart_child()
            else:
                # Healthy tick — decay backoff.
                self._backoff = max(1.0, self._backoff / 2)

        self._kill_child()
        logger.info("Supervisor stopped")

    def _restart_child(self) -> None:
        if self._stop.is_set():
            return
        self._restarts += 1
        delay = min(self.max_backoff, self._backoff)
        logger.info("Restarting child in %.0fs (restart #%d)", delay, self._restarts)
        time.sleep(delay)
        self._backoff = min(self.max_backoff, self._backoff * 2)
        self._spawn_child()


def _maybe_start_web(cfg: Config, supervisor: Supervisor) -> None:
    """Start the FastAPI dashboard in a daemon thread if enabled + installed."""
    if not getattr(cfg.web, "enabled", False):
        logger.info(
            "Web dashboard is disabled (set web.enabled: true in config.yaml "
            "to serve it at http://%s:%d).",
            getattr(cfg.web, "host", "127.0.0.1"),
            int(getattr(cfg.web, "port", 8787)),
        )
        return
    try:
        from trader.web.app import run_web
    except Exception:
        logger.warning(
            "Web dashboard enabled but FastAPI/uvicorn not installed "
            "(pip install 'alpaca-trader[web]'). Continuing without web UI."
        )
        return

    t = threading.Thread(
        target=run_web,
        kwargs={"cfg": cfg, "supervisor": supervisor},
        daemon=True,
        name="web-dashboard",
    )
    t.start()
    browse_host = "127.0.0.1" if cfg.web.host in ("0.0.0.0", "::", "") else cfg.web.host
    logger.info("Web dashboard starting — open http://%s:%d in your browser", browse_host, cfg.web.port)


def main() -> None:
    cfg = load_default_config()
    setup_logging(cfg)
    supervisor = Supervisor(cfg)
    _maybe_start_web(cfg, supervisor)

    import signal

    def _shutdown(signum, frame):
        supervisor.request_stop()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Supervisor starting (broker=%s)", cfg.broker.provider)
    supervisor.run()


if __name__ == "__main__":
    main()
