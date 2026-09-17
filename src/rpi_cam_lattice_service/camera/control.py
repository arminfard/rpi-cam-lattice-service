"""Camera control: the task-driven desired state of the stream and how to reach it.

``CameraControl`` is the single, thread-safe place where the integration
records whether the camera's video stream *should* be running. The task
handler drives it (``Start`` / ``Stop``), the 1 Hz publish loop reads it so the
entity reflects the change (the sensor's operational state and the advertised
video), and ``main.py`` calls ``recover()`` at boot and ``shutdown()`` at exit.

Each transition sequences two things in the order that keeps Lattice and the
Pi consistent:

* ``start()``: register a **new** ingress with Lattice (and write its SRT push
  URL for MediaMTX) *before* starting the pipeline, so MediaMTX comes up
  pointing at a live ingress. If the pipeline then fails to start, the fresh
  ingress is archived again so nothing leaks.
* ``stop()``: stop the pipeline *before* archiving the ingress, so MediaMTX is
  not left pushing frames at an archived stream. If archiving fails, the
  stream is still reported as stopped, the ingress id is kept, and the error is
  raised so the task ends ``DONE_NOT_OK`` and a retry can finish the cleanup.

The last step of ``start`` / ``stop`` is the ``on_change`` callback (wired to
``Service.publish_now``) so the entity carries the new state immediately. Its
failure fails the transition but not the stream state: a repeated ``Start`` is
idempotent (same ingress, pipeline restarted, publish retried).

**Two locks, one rule.** ``_transition_lock`` serialises ``start`` / ``stop`` /
``recover`` / ``shutdown``, which do slow things: a VideoManager RPC with a
30 s timeout, a ``systemctl`` command with a 30 s timeout, an entity publish.
``_state_lock`` guards the two fields ``desired_on`` and ``in_transition`` and
is **never held while calling the pipeline, the ingress, or ``on_change``**.
``snapshot()`` takes only the state lock, and ``VideoIngress.video_id`` is
likewise non-blocking, so the 1 Hz heartbeat can always read the state in
microseconds while a transition is stuck in a 30 s RPC. The entity expires
10 s after its last publish; one blocked heartbeat would take it off the map.

The pipeline is optional: with ``pipeline=None`` transitions are state-only
(development off the Pi), exactly like a ``CommandPipeline`` with empty
commands.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ..logging_setup import get_logger
from .ingress import VideoIngress
from .pipeline import MediaPipeline, PipelineError

logger = get_logger(__name__)


class CameraControlError(Exception):
    """Raised when a start/stop transition fails."""


@dataclass(frozen=True)
class ControlSnapshot:
    """The control state at one instant, read without blocking."""

    desired_on: bool  # the operator's last successful Start/Stop intent
    video_id: str | None  # the ingress id currently advertised, if any
    in_transition: bool  # a Start/Stop/shutdown is in progress right now


class CameraControl:
    """Thread-safe desired-state of the camera stream plus the actions to reach it."""

    def __init__(
        self,
        *,
        pipeline: MediaPipeline | None = None,
        video: VideoIngress | None = None,
        on_change: Callable[[], None] | None = None,
        desired_on: bool = False,
    ) -> None:
        self._pipeline = pipeline
        self._video = video
        # Called (outside every lock) once a transition has taken effect, to
        # push the new state to the camera asset entity immediately: the new
        # ingress id into its Media items after Start, an empty Media list
        # after Stop. Wired to ``Service.publish_now``.
        self.on_change = on_change
        self._desired_on = desired_on
        self._in_transition = False
        self._transition_lock = threading.Lock()
        self._state_lock = threading.Lock()

    # -- reads ---------------------------------------------------------------

    def snapshot(self) -> ControlSnapshot:
        """Current state; takes only the state lock, never waits on I/O."""
        with self._state_lock:
            desired_on = self._desired_on
            in_transition = self._in_transition
        return ControlSnapshot(
            desired_on=desired_on, video_id=self._video_id(), in_transition=in_transition
        )

    # -- transitions ---------------------------------------------------------

    def start(self) -> None:
        """Register a fresh ingress, then bring the pipeline up."""
        with self._transition_lock, self._transitioning():
            push_url = ""
            if self._video is not None:
                try:
                    push_url = self._video.create().push_url
                except Exception as exc:
                    raise CameraControlError(f"could not register video ingress: {exc}") from exc
            try:
                self._pipeline_start(push_url)
            except CameraControlError:
                # Do not leak an ingress nobody pushes to.
                self._archive_best_effort("pipeline start failed")
                raise
            self._set_desired_on(True)
            # Advertise the new ingress id on the asset now. If this fails the
            # stream is up but the task ends DONE_NOT_OK; a repeated Start is
            # idempotent (same ingress, pipeline restarted, publish retried).
            self._publish("started")
        logger.info("camera stream started", video_id=self._video_id())

    def stop(self) -> None:
        """Bring the pipeline down, then archive its ingress in Lattice."""
        with self._transition_lock, self._transitioning():
            self._pipeline_stop()
            self._set_desired_on(False)
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

    def recover(self) -> None:
        """Boot-time cleanup: archive an ingress a previous process left behind.

        Best-effort and never raises; the ingress keeps its record on failure
        so the next boot retries.
        """
        if self._video is None:
            return
        with self._transition_lock:
            try:
                self._video.recover()
            except Exception as exc:
                logger.warning("ingress recovery failed", error=str(exc))

    def shutdown(self) -> None:
        """Best-effort cleanup at service exit: stop the pipeline and archive the ingress.

        Does not publish; the caller publishes the final offline entity once
        everything else is down.
        """
        with self._transition_lock, self._transitioning():
            if self.snapshot().desired_on:
                try:
                    self._pipeline_stop()
                except CameraControlError as exc:
                    logger.warning("pipeline stop failed during shutdown", error=str(exc))
                self._set_desired_on(False)
            self._archive_best_effort("shutdown")

    # -- internals -----------------------------------------------------------

    def _video_id(self) -> str | None:
        return self._video.video_id if self._video is not None else None

    def _set_desired_on(self, value: bool) -> None:
        with self._state_lock:
            self._desired_on = value

    @contextmanager
    def _transitioning(self) -> Iterator[None]:
        """Marks ``in_transition`` for the duration of a transition body."""
        with self._state_lock:
            self._in_transition = True
        try:
            yield
        finally:
            with self._state_lock:
                self._in_transition = False

    def _pipeline_start(self, push_url: str) -> None:
        if self._pipeline is None:
            logger.info("no pipeline configured; state-only transition", action="start")
            return
        try:
            self._pipeline.start(push_url)
        except PipelineError as exc:
            raise CameraControlError(str(exc)) from exc

    def _pipeline_stop(self) -> None:
        if self._pipeline is None:
            logger.info("no pipeline configured; state-only transition", action="stop")
            return
        try:
            self._pipeline.stop()
        except PipelineError as exc:
            raise CameraControlError(str(exc)) from exc

    def _publish(self, transition: str) -> None:
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception as exc:
            raise CameraControlError(
                f"stream {transition} but the entity could not be updated: {exc}"
            ) from exc
        logger.info("entity updated", transition=transition, video_id=self._video_id())

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
