"""Service.publish_now publishes the entity from live state, on demand."""

from rpi_cam_lattice_service.config import Config
from rpi_cam_lattice_service.service import Service
from rpi_cam_lattice_service.camera.source import CameraSource


class FakeClient:
    def __init__(self) -> None:
        self.requests = []

    def publish_entity(self, request, *, timeout_ms=None):
        self.requests.append(request)


def test_publish_now_uses_current_builder_state():
    cfg = Config(lattice_endpoint="example.test", environment_token="tok", entity_id="rpi-cam-01")
    client = FakeClient()
    calls = []

    def builder(entity_id, created_time, state):
        calls.append((entity_id, created_time))
        from rpi_cam_lattice_service.camera.entity import build_camera_publish_request
        return build_camera_publish_request(cfg, entity_id, created_time, state, video_id="vid-1")

    svc = Service(cfg, client, source=CameraSource(1.0, 2.0, 3.0), request_builder=builder, entity_id="rpi-cam-01")
    svc.publish_now()
    svc.publish_now()

    assert len(client.requests) == 2
    assert client.requests[0].entity.entity_id == "rpi-cam-01"
    assert client.requests[0].entity.media.media[0].item_identifier == "vid-1"
    # Same identity and creation time every call (also shared with the 1 Hz loop).
    assert calls[0] == calls[1] == ("rpi-cam-01", svc._created_time)
