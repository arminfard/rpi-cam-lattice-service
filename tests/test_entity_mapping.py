"""Tests for the State -> Entity protobuf mapping.

These stand in for the (deferred) live round-trip: they prove the mapping uses
the correct enum members, wrapper types, and field values, so a wrong literal
can't slip through unnoticed. With these strongly-typed generated stubs an
invalid enum fails at construct time, unlike the REST SDK's open string enums.
"""

from datetime import datetime, timezone

from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest
from anduril.entitymanager.v1.health_status_pub_pb import ConnectionStatus, HealthStatus
from anduril.entitymanager.v1.ontology_pub_pb import Ontology  # noqa: F401
from anduril.entitymanager.v1.types_pub_pb import Template
from anduril.ontology.v1.type_pub_pb import Disposition, Environment

from rpi_cam_lattice_service.sources import State
from rpi_cam_lattice_service.worker import INTEGRATION_NAME, build_publish_request


def _sample_state() -> State:
    return State(
        latitude_degrees=49.6,
        longitude_degrees=11.7,
        altitude_hae_meters=1048.0,
        altitude_agl_meters=600.0,
        altitude_asf_meters=1000.0,
        velocity_e_mps=3.0,
        velocity_n_mps=4.0,
        velocity_u_mps=0.0,
        speed_mps=5.0,
        acceleration_e_mps2=0.1,
        acceleration_n_mps2=0.2,
        acceleration_u_mps2=0.3,
    )


def test_maps_identity_and_enums():
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    req = build_publish_request("uav-1", created, _sample_state())
    assert isinstance(req, PublishEntityRequest)

    e = req.entity
    assert e.entity_id == "uav-1"
    assert e.is_live is True
    assert e.aliases.name == "Drone 1"
    assert e.ontology.template == Template.ASSET
    assert e.ontology.platform_type == "UAV"
    assert e.mil_view.disposition == Disposition.FRIENDLY
    assert e.mil_view.environment == Environment.AIR
    assert e.health.connection_status == ConnectionStatus.ONLINE
    assert e.health.health_status == HealthStatus.HEALTHY
    assert e.provenance.integration_name == INTEGRATION_NAME
    assert e.provenance.data_type == "anduril"


def test_maps_location_wrappers_and_floats():
    req = build_publish_request("uav-1", datetime.now(timezone.utc), _sample_state())
    loc = req.entity.location
    # Plain doubles.
    assert loc.position.latitude_degrees == 49.6
    assert loc.velocity_enu.e == 3.0 and loc.velocity_enu.n == 4.0
    # DoubleValue wrappers.
    assert loc.speed_mps.value == 5.0
    assert loc.position.altitude_hae_meters.value == 1048.0
    assert loc.position.altitude_agl_meters.value == 600.0


def test_attitude_quaternion_from_velocity():
    # velocity E=3, N=4 -> yaw = atan2(4,3); quaternion is a unit rotation about Up.
    req = build_publish_request("uav-1", datetime.now(timezone.utc), _sample_state())
    q = req.entity.location.attitude_enu
    assert q.x == 0.0 and q.y == 0.0
    norm = (q.w**2 + q.z**2) ** 0.5
    assert abs(norm - 1.0) < 1e-9


def test_expiry_is_after_creation():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    req = build_publish_request("uav-1", now, _sample_state(), now=now)
    expiry = req.entity.expiry_time.to_datetime()
    assert expiry > now


def test_serializes_to_binary():
    req = build_publish_request("uav-1", datetime.now(timezone.utc), _sample_state())
    assert len(req.to_binary()) > 0
