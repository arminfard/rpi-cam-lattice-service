"""Tests for the task handler: dispatch by type URL and the status lifecycle.

A fake LatticeClient records every status update so the tests can assert the
exact sequence of statuses and that status_version strictly increases from the
version the task arrived with.
"""

from __future__ import annotations

import threading

import pytest
from protobuf import Oneof
from protobuf.wkt import any_pb

from anduril.taskmanager.v1.task_api_pub_pb import CancelRequest, ExecuteRequest
from anduril.taskmanager.v1.task_manager_api_pub_pb import Heartbeat, ListenAsAgentResponse
from anduril.taskmanager.v1.task_pub_pb import Status, Task, TaskVersion

from rpi_cam_lattice_service.control import CameraControl, CameraControlError
from rpi_cam_lattice_service.tasking import (
    TaskHandler,
    task_name_from_type_url,
    task_type_url,
)

PACKAGE = "anduril.sample_app_rpi_cam.camera.v1alpha"
AGENT = "rpi-cam-01"


class FakeClient:
    def __init__(self, fail_update: bool = False) -> None:
        self.updates: list[dict] = []
        self.fail_update = fail_update

    def update_task_status(self, **kwargs):
        if self.fail_update:
            raise RuntimeError("update_status unavailable")
        self.updates.append(kwargs)
        return TaskVersion(
            task_id=kwargs["task_id"], status_version=kwargs["status_version"]
        )

    def listen_as_agent(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("not used in unit tests")


def _execute_response(task_id: str, type_url: str | None, status_version: int = 3):
    spec = any_pb.Any(type_url=type_url, value=b"") if type_url else None
    task = Task(
        version=TaskVersion(task_id=task_id, definition_version=1, status_version=status_version),
        display_name=f"test {task_id}",
        specification=spec,
    )
    return ListenAsAgentResponse(request=Oneof("execute_request", ExecuteRequest(task=task)))


def _handler(client, control=None):
    control = control or CameraControl(streaming=True)
    return TaskHandler(client, agent_entity_id=AGENT, task_package=PACKAGE, control=control), control


def _drain_threads():
    for t in threading.enumerate():
        if t.name.startswith("task-"):
            t.join(timeout=5)


def test_type_url_helpers():
    url = task_type_url(PACKAGE, "Start")
    assert url == "type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Start"
    assert task_name_from_type_url(url) == "Start"


def test_catalog_advertises_start_and_stop():
    handler, _ = _handler(FakeClient())
    assert handler.task_specification_urls == [
        task_type_url(PACKAGE, "Start"),
        task_type_url(PACKAGE, "Stop"),
    ]


def test_stop_task_runs_to_done_ok_and_flips_state():
    client = FakeClient()
    handler, control = _handler(client)
    assert control.streaming is True

    handler.handle_response(_execute_response("t-1", task_type_url(PACKAGE, "Stop"), status_version=3))
    _drain_threads()

    assert control.streaming is False
    statuses = [u["status"] for u in client.updates]
    assert statuses == [Status.EXECUTING, Status.DONE_OK]
    versions = [u["status_version"] for u in client.updates]
    assert versions == [4, 5]  # strictly increasing from the delivered version
    assert all(u["task_id"] == "t-1" for u in client.updates)
    assert all(u["definition_version"] == 1 for u in client.updates)
    assert all(u["agent_entity_id"] == AGENT for u in client.updates)
    assert client.updates[-1]["error_message"] is None


def test_start_task_after_stop_restores_state():
    client = FakeClient()
    handler, control = _handler(client, CameraControl(streaming=False))
    handler.handle_response(_execute_response("t-2", task_type_url(PACKAGE, "Start")))
    _drain_threads()
    assert control.streaming is True
    assert [u["status"] for u in client.updates] == [Status.EXECUTING, Status.DONE_OK]


def test_unsupported_task_type_is_done_not_ok():
    client = FakeClient()
    handler, control = _handler(client)
    handler.handle_response(_execute_response("t-3", "type.googleapis.com/anduril.tasks.v2.Investigate"))
    _drain_threads()
    assert control.streaming is True
    assert [u["status"] for u in client.updates] == [Status.DONE_NOT_OK]
    assert "unsupported task type" in client.updates[0]["error_message"]


def test_missing_specification_is_done_not_ok():
    client = FakeClient()
    handler, _ = _handler(client)
    handler.handle_response(_execute_response("t-4", None))
    _drain_threads()
    assert [u["status"] for u in client.updates] == [Status.DONE_NOT_OK]


def test_failing_command_reports_done_not_ok():
    client = FakeClient()
    control = CameraControl(stop_command="exit 3", streaming=True)
    handler, _ = _handler(client, control)
    handler.handle_response(_execute_response("t-5", task_type_url(PACKAGE, "Stop")))
    _drain_threads()
    assert control.streaming is True  # state unchanged on failure
    assert [u["status"] for u in client.updates] == [Status.EXECUTING, Status.DONE_NOT_OK]
    assert "exited with 3" in client.updates[-1]["error_message"]


def test_heartbeat_and_cancel_for_unknown_task_are_harmless():
    client = FakeClient()
    handler, _ = _handler(client)
    assert handler.last_heartbeat is None
    handler.handle_response(ListenAsAgentResponse(heartbeat=Heartbeat()))
    assert handler.last_heartbeat is not None
    handler.handle_response(
        ListenAsAgentResponse(request=Oneof("cancel_request", CancelRequest(task_id="nope")))
    )
    assert client.updates == []


def test_update_failure_does_not_crash_worker():
    client = FakeClient(fail_update=True)
    handler, control = _handler(client)
    handler.handle_response(_execute_response("t-6", task_type_url(PACKAGE, "Stop")))
    _drain_threads()
    # The action never ran because EXECUTING could not be reported first.
    assert control.streaming is True


def test_camera_control_commands():
    control = CameraControl(start_command="true", stop_command="false", streaming=False)
    control.start()
    assert control.streaming is True
    with pytest.raises(CameraControlError):
        control.stop()
    assert control.streaming is True
