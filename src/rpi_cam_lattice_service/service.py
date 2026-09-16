"""Service lifecycle: publish loop, task thread, signal handling, graceful shutdown.

Signals are handled on the main thread (a Python requirement); the 1 Hz
publish loop and the task stream each run on a background thread.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone

from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest

from .camera.source import CameraSource, CameraState
from .config import Config
from .lattice import LatticeClient
from .logging_setup import get_logger
from .tasking.handler import TaskHandler

logger = get_logger(__name__)

PUBLISH_INTERVAL_SECONDS = 1.0
PUBLISH_TIMEOUT_MS = 1000
SHUTDOWN_TIMEOUT_SECONDS = 30.0

# (entity_id, created_time, state) -> PublishEntityRequest
RequestBuilder = Callable[[str, datetime, CameraState], PublishEntityRequest]


class Service:
    """The long-running service."""

    def __init__(
        self,
        config: Config,
        client: LatticeClient,
        *,
        source: CameraSource,
        request_builder: RequestBuilder,
        entity_id: str,
        task_handler: TaskHandler | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._source = source
        self._request_builder = request_builder
        # Optional Lattice task handler; runs the agent stream on its own thread.
        self._task_handler = task_handler
        # The entity's identity is fixed for the life of the service so that
        # both the periodic loop and on-demand publishes describe the same
        # entity (a stable id keeps the camera's identity across restarts).
        self._entity_id = entity_id
        self._created_time = datetime.now(timezone.utc)

        self._stop = threading.Event()
        self._reload_requested = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._task_thread: threading.Thread | None = None
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

        if self._task_handler is not None:
            # Daemon: the blocking stream read cannot be interrupted, so the
            # thread is abandoned at exit rather than joined.
            self._task_thread = threading.Thread(
                target=self._task_handler.run,
                args=(self._stop,),
                name="tasking",
                daemon=True,
            )
            self._task_thread.start()
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

    # -- publishing ----------------------------------------------------------

    @property
    def entity_id(self) -> str:
        return self._entity_id

    def publish_now(self, *, timeout_ms: int = PUBLISH_TIMEOUT_MS) -> None:
        """Publish the entity immediately from the current source/control state.

        Used by Start/Stop transitions so the asset's Media item and sensor
        state change as part of the task rather than on the next tick. Raises
        on failure; the periodic loop keeps republishing regardless.
        """
        state = self._source.next_state()
        request = self._request_builder(self._entity_id, self._created_time, state)
        self._client.publish_entity(request, timeout_ms=timeout_ms)

    # -- worker --------------------------------------------------------------

    def _worker(self) -> None:
        entity_id = self._entity_id
        logger.info(
            "worker initialized",
            entity_id=entity_id,
            created_at=self._created_time.isoformat(),
        )

        # Tick every PUBLISH_INTERVAL_SECONDS; wait() returns True when stopped.
        while not self._stop.wait(PUBLISH_INTERVAL_SECONDS):
            try:
                self.publish_now()
                logger.debug("operation completed successfully", entity_id=entity_id)
            except Exception as exc:
                logger.error("operation failed", error=str(exc))

        logger.info("worker stopping")
