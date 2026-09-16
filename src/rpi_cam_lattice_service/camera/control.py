"""Camera control: the task-driven stream state and how to reach it.

``CameraControl`` is the single, thread-safe place where the integration
records whether the camera's video stream is running. The task handler drives
it (``Start`` / ``Stop``), and the 1 Hz publish loop reads it so the entity
reflects the change: the sensor's operational state and the advertised video.

Each transition sequences two things in the order that keeps Lattice and the
Pi consistent:

* ``start()``: register a **new** ingress with Lattice (and write its SRT push
  URL for MediaMTX) *before* running the start command, so MediaMTX comes up
  pointing at a live ingress. If the command then fails, the fresh ingress is
  archived again so nothing leaks.
* ``stop()``: run the stop command *before* archiving the ingress, so MediaMTX
  is not left pushing frames at an archived stream. If archiving fails, the
  stream is still reported as stopped, the ingress id is kept, and the error is
  raised so the task ends ``DONE_NOT_OK`` and a retry can finish the cleanup.

The commands (``TASK_START_COMMAND`` / ``TASK_STOP_COMMAND``) are typically
``systemctl restart|stop mediamtx-srt``; ``restart`` rather than ``start`` so
MediaMTX re-reads the SRT target on every Start. When a command is empty the
transition is state-only (development off the Pi).
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

from ..logging_setup import get_logger
from .ingress import VideoIngress

logger = get_logger(__name__)

COMMAND_TIMEOUT_SECONDS = 30.0


class CameraControlError(Exception):
    """Raised when a start/stop transition fails."""


class CameraControl:
    """Thread-safe desired-state of the camera stream plus the actions to reach it."""

    def __init__(
        self,
        *,
        start_command: str = "",
        stop_command: str = "",
        streaming: bool = False,
        video: VideoIngress | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._start_command = start_command.strip()
        self._stop_command = stop_command.strip()
        self._streaming = streaming
        self._video = video
        # Called (under the lock) once a transition has taken effect, to push
        # the new state to the camera asset entity immediately: the new
        # ingress id into its Media items after Start, an empty Media list
        # after Stop. Wired to ``Service.publish_now``.
        self.on_change = on_change
        # Reentrant: the on_change publish reads ``streaming`` / ``video_id``
        # while a transition still holds the lock.
        self._lock = threading.RLock()

    @property
    def streaming(self) -> bool:
        with self._lock:
            return self._streaming

    @property
    def video_id(self) -> str | None:
        """The ingress id currently advertised on the entity, if any."""
        return self._video.video_id if self._video else None

    def start(self) -> None:
        """Register a fresh ingress, then bring the stream up."""
        with self._lock:
            if self._video is not None:
                try:
                    self._video.create()
                except Exception as exc:
                    raise CameraControlError(f"could not register video ingress: {exc}") from exc
            try:
                self._run(self._start_command, "start")
            except CameraControlError:
                # Do not leak an ingress nobody pushes to.
                self._archive_best_effort("start command failed")
                raise
            self._streaming = True
            # Advertise the new ingress id on the asset now. If this fails the
            # stream is up but the task ends DONE_NOT_OK; a repeated Start is
            # idempotent (same ingress, MediaMTX restarted, publish retried).
            self._publish("started")
        logger.info("camera stream started", video_id=self.video_id)

    def stop(self) -> None:
        """Bring the stream down, then archive its ingress in Lattice."""
        with self._lock:
            self._run(self._stop_command, "stop")
            self._streaming = False
            if self._video is not None:
                try:
                    self._video.delete()
                except Exception as exc:
                    raise CameraControlError(
                        f"stream stopped but the ingress could not be archived: {exc}"
                    ) from exc
            # Clear the archived ingress from the asset's Media items now.
            self._publish("stopped")
        logger.info("camera stream stopped")

    def shutdown(self) -> None:
        """Best-effort cleanup at service exit: stop the stream and archive the ingress."""
        with self._lock:
            if self._streaming:
                try:
                    self._run(self._stop_command, "stop")
                except CameraControlError as exc:
                    logger.warning("stop command failed during shutdown", error=str(exc))
                self._streaming = False
            self._archive_best_effort("shutdown")
            try:
                self._publish("shutdown")
            except CameraControlError as exc:
                logger.warning("final entity publish failed during shutdown", error=str(exc))

    def _publish(self, transition: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception as exc:
            raise CameraControlError(
                f"stream {transition} but the entity could not be updated: {exc}"
            ) from exc
        logger.info("entity updated", transition=transition, video_id=self.video_id)

    def _archive_best_effort(self, reason: str) -> None:
        if self._video is None:
            return
        try:
            self._video.delete()
        except Exception as exc:
            logger.warning(
                "could not archive video ingress",
                reason=reason,
                video_id=self._video.video_id,
                error=str(exc),
            )

    @staticmethod
    def _run(command: str, action: str) -> None:
        if not command:
            logger.info("no command configured; state-only transition", action=action)
            return
        logger.info("running command", action=action, command=command)
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise CameraControlError(
                f"{action} command timed out after {COMMAND_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        except OSError as exc:
            raise CameraControlError(f"{action} command could not run: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            raise CameraControlError(
                f"{action} command exited with {result.returncode}: {detail}"
            )
