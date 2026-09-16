"""Tests for the stationary camera source and its entity mapping."""

from datetime import datetime, timezone

from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest
from anduril.entitymanager.v1.health_status_pub_pb import ConnectionStatus, HealthStatus
from anduril.entitymanager.v1.media_pub_pb import MediaType
from anduril.entitymanager.v1.sensors_pub_pb import OperationalState, SensorType
from anduril.entitymanager.v1.types_pub_pb import Template
from anduril.ontology.v1.type_pub_pb import Disposition, Environment

from rpi_cam_lattice_service.config import Config
from rpi_cam_lattice_service.sources import CameraSource
from rpi_cam_lattice_service.worker import build_camera_publish_request


def _config() -> Config:
    return Config(
        lattice_endpoint="example.test",
        environment_token="tok",
        entity_id="rpi-cam-01",
        entity_name="Front Gate Camera",
        platform_type="Camera",
        camera_latitude=37.1234,
        camera_longitude=-122.5678,
        camera_altitude_hae_meters=30.0,
    )


def test_camera_source_is_stationary():
    cfg = _config()
    src = CameraSource(cfg.camera_latitude, cfg.camera_longitude, cfg.camera_altitude_hae_meters)
    s1 = src.next_state()
    s2 = src.next_state()
    assert s1.latitude_degrees == s2.latitude_degrees == 37.1234
    assert s1.longitude_degrees == -122.5678
    assert s1.speed_mps == 0.0
    assert (s1.velocity_e_mps, s1.velocity_n_mps, s1.velocity_u_mps) == (0.0, 0.0, 0.0)


def test_camera_entity_identity_and_sensor():
    cfg = _config()
    st = CameraSource(cfg.camera_latitude, cfg.camera_longitude, cfg.camera_altitude_hae_meters).next_state()
    req = build_camera_publish_request(cfg, cfg.entity_id, datetime.now(timezone.utc), st)
    assert isinstance(req, PublishEntityRequest)
    e = req.entity
    assert e.entity_id == "rpi-cam-01"
    assert e.aliases.name == "Front Gate Camera"
    assert e.ontology.template == Template.ASSET
    assert e.ontology.platform_type == "Camera"
    assert e.mil_view.disposition == Disposition.FRIENDLY
    assert e.mil_view.environment == Environment.LAND
    assert e.health.connection_status == ConnectionStatus.ONLINE
    assert e.health.health_status == HealthStatus.HEALTHY
    assert e.sensors.sensors[0].sensor_type == SensorType.CAMERA
    assert e.sensors.sensors[0].operational_state == OperationalState.OPERATIONAL
    assert e.location.position.latitude_degrees == 37.1234


def test_video_id_advertised_as_media_item():
    cfg = _config()
    st = CameraSource(cfg.camera_latitude, cfg.camera_longitude, cfg.camera_altitude_hae_meters).next_state()
    req = build_camera_publish_request(
        cfg, cfg.entity_id, datetime.now(timezone.utc), st, video_id="ingress-abc-123"
    )
    item = req.entity.media.media[0]
    assert item.item_identifier == "ingress-abc-123"
    assert item.type == MediaType.VIDEO


def test_no_video_id_means_no_media():
    cfg = _config()
    st = CameraSource(cfg.camera_latitude, cfg.camera_longitude, cfg.camera_altitude_hae_meters).next_state()
    req = build_camera_publish_request(cfg, cfg.entity_id, datetime.now(timezone.utc), st, video_id=None)
    assert req.entity.media is None
    assert len(req.to_binary()) > 0
