"""Tests for the Start/Stop transitions: ingress lifecycle ordering and failures.

A fake Lattice client records every VideoManager call so the tests can assert
that Start registers a fresh ingress (new id each time) and writes the SRT
target *before* MediaMTX is started, and that Stop stops MediaMTX *before*
archiving the ingress.
"""

from __future__ import annotations

import pytest

from rpi_cam_lattice_service.camera.control import CameraControl, CameraControlError
from rpi_cam_lattice_service.camera.ingress import VideoIngress
from rpi_cam_lattice_service.lattice import SrtIngressInfo


class FakeClient:
    def __init__(self, *, fail_create=False, fail_delete=False) -> None:
        self.calls: list[tuple] = []
        self.fail_create = fail_create
        self.fail_delete = fail_delete

    def create_srt_ingress(self, *, title, passphrase="", ingress_id=None, timeout_ms=None):
        self.calls.append(("create", ingress_id))
        if self.fail_create:
            raise RuntimeError("VideoManager unavailable")
        return SrtIngressInfo(
            video_id=ingress_id, push_url=f"srt://lattice:8890?streamid={ingress_id}", session_id="s"
        )

    def delete_srt_ingress(self, ingress_id, *, timeout_ms=None):
        self.calls.append(("delete", ingress_id))
        if self.fail_delete:
            raise RuntimeError("VideoManager unavailable")


def _setup(tmp_path, *, start_command="", stop_command="", **client_kwargs):
    client = FakeClient(**client_kwargs)
    target = tmp_path / "srt_target.env"
    video = VideoIngress(
        client, entity_id="rpi-cam-01", title="cam", srt_target_file=str(target)
    )
    # Commands that record when they ran, relative to the VideoManager calls,
    # by appending to a marker file the fake client cannot see; we inspect the
    # SRT target file's presence at command time instead.
    control = CameraControl(
        start_command=start_command, stop_command=stop_command, video=video
    )
    return client, video, control, target


def test_start_registers_fresh_ingress_and_writes_target_before_command(tmp_path):
    marker = tmp_path / "seen_target"
    # The start command copies the target file: it must already exist.
    client, video, control, target = _setup(
        tmp_path, start_command=f"cp '{tmp_path}/srt_target.env' '{marker}'"
    )
    control.start()

    assert control.streaming is True
    assert client.calls[0][0] == "create"
    ingress_id = client.calls[0][1]
    assert ingress_id.startswith("rpi-cam-01-")
    assert control.video_id == ingress_id
    assert target.read_text() == f"SRT_TARGET=srt://lattice:8890?streamid={ingress_id}\n"
    assert marker.read_text() == target.read_text()


def test_stop_runs_command_then_archives_and_clears_video(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    ingress_id = control.video_id
    control.stop()

    assert control.streaming is False
    assert control.video_id is None
    assert client.calls == [("create", ingress_id), ("delete", ingress_id)]


def test_restart_uses_a_new_ingress_id(tmp_path):
    client, video, control, target = _setup(tmp_path)
    control.start()
    first = control.video_id
    control.stop()
    control.start()
    second = control.video_id
    assert first != second
    assert [c[0] for c in client.calls] == ["create", "delete", "create"]


def test_start_failure_on_ingress_leaves_state_unchanged(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_create=True)
    with pytest.raises(CameraControlError, match="could not register video ingress"):
        control.start()
    assert control.streaming is False
    assert control.video_id is None
    assert not target.exists()


def test_start_command_failure_archives_the_new_ingress(tmp_path):
    client, video, control, target = _setup(tmp_path, start_command="exit 7")
    with pytest.raises(CameraControlError, match="exited with 7"):
        control.start()
    assert control.streaming is False
    assert control.video_id is None
    assert [c[0] for c in client.calls] == ["create", "delete"]


def test_stop_command_failure_keeps_ingress(tmp_path):
    client, video, control, target = _setup(tmp_path, stop_command="false")
    control.start()
    with pytest.raises(CameraControlError):
        control.stop()
    assert control.streaming is True
    assert control.video_id is not None
    assert [c[0] for c in client.calls] == ["create"]


def test_archive_failure_reports_stopped_but_keeps_id_for_retry(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_delete=True)
    control.start()
    ingress_id = control.video_id
    with pytest.raises(CameraControlError, match="could not be archived"):
        control.stop()
    assert control.streaming is False
    assert control.video_id == ingress_id  # retained so a retry can archive it
    client.fail_delete = False
    control.stop()  # retry succeeds
    assert control.video_id is None


def test_shutdown_is_best_effort(tmp_path):
    client, video, control, target = _setup(tmp_path, fail_delete=True)
    control.start()
    control.shutdown()  # must not raise
    assert control.streaming is False
    assert [c[0] for c in client.calls] == ["create", "delete"]


def test_without_video_transitions_are_state_only():
    control = CameraControl()
    control.start()
    assert control.streaming is True and control.video_id is None
    control.stop()
    assert control.streaming is False


# --- entity updates as part of the transition --------------------------------


class RecordingPublisher:
    """Stands in for Service.publish_now: records the control state at call time."""

    def __init__(self, control_ref: list, fail: bool = False) -> None:
        self._control_ref = control_ref
        self.fail = fail
        self.snapshots: list[tuple[bool, str | None]] = []

    def __call__(self) -> None:
        control = self._control_ref[0]
        self.snapshots.append((control.streaming, control.video_id))
        if self.fail:
            raise RuntimeError("publish failed")


def _setup_with_publisher(tmp_path, *, fail_publish=False, **kwargs):
    ref: list = []
    publisher = RecordingPublisher(ref, fail=fail_publish)
    client, video, control, target = _setup(tmp_path, **kwargs)
    control.on_change = publisher
    ref.append(control)
    return client, control, publisher


def test_start_publishes_new_ingress_id_to_entity_after_stream_is_up(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path)
    control.start()
    # Exactly one publish, made after the ingress exists and streaming is on,
    # so the asset's Media items carry the new id.
    assert publisher.snapshots == [(True, control.video_id)]
    assert control.video_id is not None


def test_stop_publishes_cleared_media_after_archiving(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path)
    control.start()
    control.stop()
    # The stop-time publish happens after delete: no video id, not streaming.
    assert publisher.snapshots[-1] == (False, None)
    assert client.calls[-1][0] == "delete"


def test_publish_failure_fails_the_transition_but_keeps_stream_state(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path, fail_publish=True)
    with pytest.raises(CameraControlError, match="entity could not be updated"):
        control.start()
    # The stream really is up; a repeated Start reuses the ingress and retries.
    assert control.streaming is True
    ingress_id = control.video_id
    publisher.fail = False
    control.start()
    assert control.video_id == ingress_id
    assert [c[0] for c in client.calls] == ["create"]


def test_shutdown_publishes_final_state_best_effort(tmp_path):
    client, control, publisher = _setup_with_publisher(tmp_path, fail_publish=True)
    publisher.fail = False
    control.start()
    publisher.fail = True
    control.shutdown()  # must not raise
    assert publisher.snapshots[-1] == (False, None)
