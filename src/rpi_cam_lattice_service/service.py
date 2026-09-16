"""Service lifecycle: worker loop, signal handling, graceful shutdown.

Ports ``internal/service/service.go`` and the ``worker`` loop from
``internal/service/worker.go``. Signals are handled on the main thread (a
Python requirement); the 1 Hz publish loop runs on a background thread.
"""

from __future__ import annotations

import signal
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest

from .config import Config
from .lattice_client import LatticeClient
from .logging_setup import get_logger
from .sources import DroneSimulator, Source, State
from .worker import build_publish_request

logger = get_logger(__name__)

PUBLISH_INTERVAL_SECONDS = 1.0
PUBLISH_TIMEOUT_MS = 1000
SHUTDOWN_TIMEOUT_SECONDS = 30.0

# (entity_id, created_time, state) -> PublishEntityRequest
RequestBuilder = Callable[[str, datetime, State], PublishEntityRequest]


class Service:
    """The long-running service."""

    def __init__(
        self,
        config: Config,
        client: LatticeClient,
        *,
        source: Source | None = None,
        request_builder: RequestBuilder | None = None,
        entity_id: str | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._source = source or DroneSimulator()
        self._request_builder = request_builder or build_publish_request
        # A stable id keeps a fixed sensor's identity across restarts; fall back
        # to a random id when none is supplied (the drone-sim behavior).
        self._entity_id = entity_id

        self._stop = threading.Event()
        self._reload_requested = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._start_time = 0.0

    # -- lifecycle -----------------------------------------------------------

    def run(self) -> int:
        """Start the worker and block until a shutdown signal is received."""
        self._start_time = time.monotonic()
        logger.info(
            "service starting",
            endpoint=self._config.lattice_endpoint,
            start_time=datetime.now(timezone.utc).isoformat(),
        )

        self._install_signal_handlers()

        self._worker_thread = threading.Thread(
            target=self._worker, name="publisher", daemon=True
        )
        self._worker_thread.start()
        logger.info("service started, waiting for signals")

        # Main loop: wait for stop, servicing reload requests as they arrive.
        while not self._stop.is_set():
            if self._reload_requested.is_set():
                self._reload_requested.clear()
                self._reload()
            time.sleep(0.2)

        return self._shutdown()

    def _shutdown(self) -> int:
        logger.info("initiating shutdown")
        self._stop.set()

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
            if self._worker_thread.is_alive():
                logger.error("shutdown timeout exceeded")
                return 1

        uptime = time.monotonic() - self._start_time
        logger.info("shutdown completed", uptime_seconds=round(uptime, 1))
        return 0

    def _reload(self) -> None:
        """Handle SIGHUP: reload configuration in place and keep running."""
        logger.info("reloading configuration")
        try:
            from . import config as config_module

            new_cfg = config_module.load(self._config._config_path or ".env")
            new_cfg.validate()
            self._config = new_cfg
            logger.info(
                "configuration reloaded", endpoint=new_cfg.lattice_endpoint
            )
        except Exception as exc:
            logger.error("config reload failed", error=str(exc))

    # -- signals -------------------------------------------------------------

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self._on_terminate)
        signal.signal(signal.SIGTERM, self._on_terminate)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self._on_reload)

    def _on_terminate(self, signum, _frame) -> None:
        logger.info("received signal", signal=signal.Signals(signum).name)
        self._stop.set()

    def _on_reload(self, signum, _frame) -> None:
        logger.info("received signal", signal=signal.Signals(signum).name)
        self._reload_requested.set()

    # -- worker --------------------------------------------------------------

    def _worker(self) -> None:
        entity_id = self._entity_id or str(uuid.uuid4())
        created_time = datetime.now(timezone.utc)
        logger.info(
            "worker initialized",
            entity_id=entity_id,
            created_at=created_time.isoformat(),
        )

        # Tick every PUBLISH_INTERVAL_SECONDS; wait() returns True when stopped.
        while not self._stop.wait(PUBLISH_INTERVAL_SECONDS):
            try:
                state = self._source.next_state()
                request = self._request_builder(entity_id, created_time, state)
                self._client.publish_entity(request, timeout_ms=PUBLISH_TIMEOUT_MS)
                logger.debug("operation completed successfully", entity_id=entity_id)
            except Exception as exc:
                logger.error("operation failed", error=str(exc))

        logger.info("worker stopping")
