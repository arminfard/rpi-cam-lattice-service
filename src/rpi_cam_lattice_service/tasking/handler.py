"""Lattice task handling for the camera agent.

The camera entity advertises a ``task_catalog`` listing the custom task types
it accepts (see ``task-def/``). This module opens the ``ListenAsAgent`` stream
for that entity, dispatches each ``ExecuteRequest`` on the *name* of the task
message carried in the specification's type URL, runs the matching action on
the :class:`CameraControl`, and drives the task through its status lifecycle:

    SENT -> EXECUTING -> DONE_OK | DONE_NOT_OK

Lifecycle invariants (see the Lattice "Integrate an agent" guide):

* Report ``EXECUTING`` as soon as a task is picked up.
* Every status update carries a strictly increasing ``status_version``. The
  first update uses the version delivered with the task plus one.
* Terminal states are final; a task never stays in ``EXECUTING``.
* The stream is long-lived and reconnected on any error.
* Task execution runs on a worker thread so the stream loop never blocks.

The handler runs in-process on its own daemon thread (started by ``Service``)
but depends only on ``LatticeClient`` and ``CameraControl``, so it can be
hosted by a separate entry point later if isolation becomes worth the cost.
The task names and type-URL helpers live in ``definitions.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone

from anduril.taskmanager.v1.task_manager_api_pub_pb import ListenAsAgentResponse
from anduril.taskmanager.v1.task_pub_pb import ErrorCode, Status, Task

from ..camera.control import CameraControl
from ..lattice import LatticeClient
from ..logging_setup import get_logger
from .definitions import TASK_START, TASK_STOP, task_name_from_type_url, task_type_url

logger = get_logger(__name__)

DEFAULT_HEARTBEAT_INTERVAL_MS = 30_000
DEFAULT_RECONNECT_DELAY_SECONDS = 2.0
UPDATE_STATUS_TIMEOUT_MS = 10_000


class _ActiveTask:
    """Bookkeeping for one in-flight task (owned by its worker thread)."""

    def __init__(self, task: Task) -> None:
        self.task = task
        self.task_id = task.version.task_id
        self.definition_version = task.version.definition_version
        # Lattice ignores updates whose version is not greater than what it
        # already holds, so continue from the version the task arrived with.
        self.status_version = task.version.status_version
        self.cancel = threading.Event()
        self.terminal = False


class TaskHandler:
    """Streams tasks routed to the camera entity and executes them."""

    def __init__(
        self,
        client: LatticeClient,
        *,
        agent_entity_id: str,
        task_package: str,
        control: CameraControl,
        heartbeat_interval_ms: int = DEFAULT_HEARTBEAT_INTERVAL_MS,
        reconnect_delay_seconds: float = DEFAULT_RECONNECT_DELAY_SECONDS,
    ) -> None:
        self._client = client
        self._entity_id = agent_entity_id
        self._package = task_package
        self._control = control
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._reconnect_delay = reconnect_delay_seconds

        self._actions: dict[str, Callable[[], None]] = {
            task_type_url(task_package, TASK_START): control.start,
            task_type_url(task_package, TASK_STOP): control.stop,
        }
        self._active: dict[str, _ActiveTask] = {}
        self._active_lock = threading.Lock()
        self._last_heartbeat: datetime | None = None

    # -- catalog -------------------------------------------------------------

    @property
    def task_specification_urls(self) -> list[str]:
        """Type URLs to advertise in the entity's ``task_catalog``."""
        return list(self._actions)

    @property
    def last_heartbeat(self) -> datetime | None:
        return self._last_heartbeat

    # -- stream loop ---------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Consume the agent stream until ``stop`` is set, reconnecting on errors.

        A blocking read cannot be interrupted, so a shutdown is only noticed
        between messages or after a disconnect; the caller runs this on a
        daemon thread.
        """
        logger.info(
            "task handler starting",
            entity_id=self._entity_id,
            tasks=self.task_specification_urls,
        )
        while not stop.is_set():
            try:
                stream = self._client.listen_as_agent(
                    self._entity_id, heartbeat_interval_ms=self._heartbeat_interval_ms
                )
                logger.info("task stream opened", entity_id=self._entity_id)
                # The request is only sent once iteration starts; the first
                # message (typically a heartbeat) proves the stream is live.
                first = True
                for response in stream:
                    if first:
                        logger.info("task stream connected", entity_id=self._entity_id)
                        first = False
                    if stop.is_set():
                        break
                    self.handle_response(response)
                logger.warning("task stream ended; reconnecting")
            except Exception as exc:
                if stop.is_set():
                    break
                logger.warning(
                    "task stream error; reconnecting",
                    error=str(exc),
                    retry_in_seconds=self._reconnect_delay,
                )
            stop.wait(self._reconnect_delay)
        logger.info("task handler stopping")

    def handle_response(self, response: ListenAsAgentResponse) -> None:
        """Dispatch one stream message (heartbeat, execute, cancel, complete)."""
        if response.has_field("heartbeat"):
            self._last_heartbeat = datetime.now(timezone.utc)
            logger.debug("task stream heartbeat")
            return

        request = response.request
        if request is None:
            return
        if request.field == "execute_request":
            task = request.value.task
            if task is None or task.version is None or not task.version.task_id:
                logger.warning("execute request without a task id; ignoring")
                return
            self._start_worker(task)
        elif request.field == "cancel_request":
            self._on_cancel(request.value.task_id)
        elif request.field == "complete_request":
            logger.info("complete request received", task_id=request.value.task_id)
        else:
            logger.warning("unknown task stream request", field=request.field)

    # -- execution -----------------------------------------------------------

    def _start_worker(self, task: Task) -> None:
        active = _ActiveTask(task)
        with self._active_lock:
            if active.task_id in self._active:
                logger.info("task already in progress; ignoring duplicate", task_id=active.task_id)
                return
            self._active[active.task_id] = active
        threading.Thread(
            target=self.execute, args=(active,), name=f"task-{active.task_id[:8]}", daemon=True
        ).start()

    def execute(self, active: _ActiveTask) -> None:
        """Run one task to a terminal state (called on a worker thread)."""
        task = active.task
        spec = task.specification
        type_url = spec.type_url if spec is not None else ""
        name = task_name_from_type_url(type_url) if type_url else "<none>"
        logger.info(
            "task received",
            task_id=active.task_id,
            task=name,
            type_url=type_url,
            display_name=task.display_name,
            status_version=active.status_version,
        )
        try:
            action = self._actions.get(type_url)
            if action is None:
                self._update(active, Status.DONE_NOT_OK, error=f"unsupported task type: {type_url or '<none>'}")
                return

            self._update(active, Status.EXECUTING)
            if active.cancel.is_set():
                self._update(
                    active,
                    Status.DONE_NOT_OK,
                    error="cancelled before execution",
                    error_code=ErrorCode.CANCELLED,
                )
                return

            action()
            self._update(active, Status.DONE_OK)
        except Exception as exc:
            logger.error("task failed", task_id=active.task_id, task=name, error=str(exc))
            if not active.terminal:
                try:
                    self._update(active, Status.DONE_NOT_OK, error=str(exc))
                except Exception as update_exc:
                    logger.error(
                        "could not report task failure",
                        task_id=active.task_id,
                        error=str(update_exc),
                    )
        finally:
            with self._active_lock:
                self._active.pop(active.task_id, None)

    def _on_cancel(self, task_id: str) -> None:
        with self._active_lock:
            active = self._active.get(task_id)
        if active is None:
            # Start/Stop complete almost instantly, so a cancel usually arrives
            # after the task is already terminal; nothing further to report.
            logger.info("cancel request for a task that is not in progress", task_id=task_id)
            return
        logger.info("cancel requested", task_id=task_id)
        active.cancel.set()

    def _update(
        self,
        active: _ActiveTask,
        status: Status,
        *,
        error: str | None = None,
        error_code: ErrorCode = ErrorCode.FAILED,
    ) -> None:
        if active.terminal:
            return
        active.status_version += 1
        self._client.update_task_status(
            task_id=active.task_id,
            definition_version=active.definition_version,
            status_version=active.status_version,
            status=status,
            agent_entity_id=self._entity_id,
            error_message=error,
            error_code=error_code,
            timeout_ms=UPDATE_STATUS_TIMEOUT_MS,
        )
        if status in (Status.DONE_OK, Status.DONE_NOT_OK):
            active.terminal = True
        logger.info(
            "task status updated",
            task_id=active.task_id,
            status=status.name,
            status_version=active.status_version,
            error=error,
        )
