"""Camera control state driven by Lattice tasks.

``CameraControl`` is the single, thread-safe place where the integration
records whether the camera's video stream is *supposed* to be running. The
task handler flips it when a ``Start`` or ``Stop`` task arrives, and the 1 Hz
publish loop reads it so the entity reflects the change (sensor operational
state), which is the feedback loop an operator sees in Lattice.

Actually starting/stopping the stream is delegated to two optional shell
commands (``TASK_START_COMMAND`` / ``TASK_STOP_COMMAND``); on the Pi these are
typically ``systemctl start|stop mediamtx-srt``. When a command is empty the
transition is state-only (useful for development off the Pi).
"""

from __future__ import annotations

import subprocess
import threading

from .logging_setup import get_logger

logger = get_logger(__name__)

COMMAND_TIMEOUT_SECONDS = 30.0


class CameraControlError(Exception):
    """Raised when a start/stop command fails."""


class CameraControl:
    """Thread-safe desired-state of the camera stream plus the actions to reach it."""

    def __init__(
        self,
        *,
        start_command: str = "",
        stop_command: str = "",
        streaming: bool = True,
    ) -> None:
        self._start_command = start_command.strip()
        self._stop_command = stop_command.strip()
        self._streaming = streaming
        self._lock = threading.Lock()

    @property
    def streaming(self) -> bool:
        with self._lock:
            return self._streaming

    def start(self) -> None:
        """Start the stream (run the start command) and record the state."""
        with self._lock:
            self._run(self._start_command, "start")
            self._streaming = True
        logger.info("camera stream started")

    def stop(self) -> None:
        """Stop the stream (run the stop command) and record the state."""
        with self._lock:
            self._run(self._stop_command, "stop")
            self._streaming = False
        logger.info("camera stream stopped")

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
