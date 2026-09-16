"""Builds Lattice entities from sensor state and publishes them.

Ports ``internal/service/worker.go``: the attitude math (yaw from ENU velocity,
quaternion from yaw) and the mapping of a ``State`` snapshot onto a Lattice
``Entity`` protobuf message published via ``PublishEntity``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from protobuf.wkt import timestamp_pb, wrappers_pb

from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest
from anduril.entitymanager.v1.entity_pub_pb import Aliases, Entity, Provenance
from anduril.entitymanager.v1.health_status_pub_pb import (
    ConnectionStatus,
    Health,
    HealthStatus,
)
from anduril.entitymanager.v1.location_pub_pb import Location, Position
from anduril.entitymanager.v1.media_pub_pb import Media, MediaItem, MediaType
from anduril.entitymanager.v1.ontology_pub_pb import MilView, Ontology
from anduril.entitymanager.v1.sensors_pub_pb import (
    OperationalState,
    Sensor,
    Sensors,
    SensorType,
)
from anduril.entitymanager.v1.types_pub_pb import Template
from anduril.ontology.v1.type_pub_pb import Disposition, Environment
from anduril.tasks.v2.catalog_pub_pb import TaskCatalog, TaskDefinition
from anduril.type.coords_pub_pb import ENU, Quaternion

from .config import Config
from .sources import State

INTEGRATION_NAME = "rpi-cam-lattice-service"
# Keep the entity live: expiry is short and we re-publish on every tick.
ENTITY_EXPIRY_SECONDS = 10


def velocity_to_yaw_enu(velocity_east: float, velocity_north: float) -> float:
    """Compute yaw (degrees) from ENU velocity. 0 deg = East, 90 deg = North."""
    return math.atan2(velocity_north, velocity_east) * 180.0 / math.pi


def yaw_to_quaternion_enu(yaw_degrees: float) -> Quaternion:
    """Convert a yaw angle to a body-frame-to-ENU quaternion (rotation about Up).

    For a rotation of angle theta about the Z (Up) axis:
        q = [w, x, y, z] = [cos(theta/2), 0, 0, sin(theta/2)]
    """
    yaw_rad = yaw_degrees * math.pi / 180.0
    return Quaternion(
        w=math.cos(yaw_rad * 0.5),
        x=0.0,
        y=0.0,
        z=math.sin(yaw_rad * 0.5),
    )


def _double(value: float) -> wrappers_pb.DoubleValue:
    return wrappers_pb.DoubleValue(value=value)


def _timestamp(dt: datetime) -> timestamp_pb.Timestamp:
    return timestamp_pb.Timestamp.from_datetime(dt)


def build_publish_request(
    entity_id: str,
    created_time: datetime,
    state: State,
    *,
    now: datetime | None = None,
) -> PublishEntityRequest:
    """Map a sensor ``State`` onto a Lattice ``Entity`` publish request."""
    now = now or datetime.now(timezone.utc)

    yaw_degrees = velocity_to_yaw_enu(state.velocity_e_mps, state.velocity_n_mps)
    attitude = yaw_to_quaternion_enu(yaw_degrees)

    entity = Entity(
        entity_id=entity_id,
        description="UAV asset",
        is_live=True,
        created_time=_timestamp(created_time),
        expiry_time=_timestamp(now + timedelta(seconds=ENTITY_EXPIRY_SECONDS)),
        aliases=Aliases(name="Drone 1"),
        ontology=Ontology(template=Template.ASSET, platform_type="UAV"),
        mil_view=MilView(
            disposition=Disposition.FRIENDLY,
            environment=Environment.AIR,
        ),
        location=Location(
            position=Position(
                latitude_degrees=state.latitude_degrees,
                longitude_degrees=state.longitude_degrees,
                altitude_hae_meters=_double(state.altitude_hae_meters),
                altitude_agl_meters=_double(state.altitude_agl_meters),
                altitude_asf_meters=_double(state.altitude_asf_meters),
            ),
            velocity_enu=ENU(
                e=state.velocity_e_mps,
                n=state.velocity_n_mps,
                u=state.velocity_u_mps,
            ),
            speed_mps=_double(state.speed_mps),
            acceleration=ENU(
                e=state.acceleration_e_mps2,
                n=state.acceleration_n_mps2,
                u=state.acceleration_u_mps2,
            ),
            attitude_enu=attitude,
        ),
        provenance=Provenance(
            integration_name=INTEGRATION_NAME,
            data_type="anduril",
            source_update_time=_timestamp(now),
        ),
        health=Health(
            connection_status=ConnectionStatus.ONLINE,
            health_status=HealthStatus.HEALTHY,
            update_time=_timestamp(now),
        ),
    )
    return PublishEntityRequest(entity=entity)


def build_camera_publish_request(
    config: Config,
    entity_id: str,
    created_time: datetime,
    state: State,
    *,
    video_id: str | None = None,
    task_specification_urls: list[str] | None = None,
    streaming: bool = True,
    now: datetime | None = None,
) -> PublishEntityRequest:
    """Map a stationary camera ``State`` onto a Lattice ``Entity`` publish request.

    The camera is published as a friendly, land-based asset carrying an EO camera
    sensor and its health. When ``video_id`` is provided (the id returned by
    Lattice's VideoManager ``CreateIngressStream``), it is advertised on the
    entity's ``Media`` component so operators can locate the live feed.

    ``task_specification_urls`` are advertised in the entity's ``task_catalog``
    (an operator can only assign a task the catalog lists). ``streaming`` is the
    task-driven camera state: a stopped stream reports the sensor as ``OFF``.
    """
    now = now or datetime.now(timezone.utc)

    description = "Raspberry Pi Camera Module"
    media = None
    if video_id:
        media = Media(
            media=[MediaItem(item_identifier=video_id, type=MediaType.VIDEO)]
        )
    task_catalog = None
    if task_specification_urls:
        task_catalog = TaskCatalog(
            task_definitions=[
                TaskDefinition(task_specification_url=url)
                for url in task_specification_urls
            ]
        )
    operational_state = (
        OperationalState.OPERATIONAL if streaming else OperationalState.OFF
    )

    entity = Entity(
        entity_id=entity_id,
        description=description,
        is_live=True,
        created_time=_timestamp(created_time),
        expiry_time=_timestamp(now + timedelta(seconds=ENTITY_EXPIRY_SECONDS)),
        aliases=Aliases(name=config.entity_name),
        ontology=Ontology(template=Template.ASSET, platform_type=config.platform_type),
        mil_view=MilView(
            disposition=Disposition.FRIENDLY,
            environment=Environment.LAND,
        ),
        location=Location(
            position=Position(
                latitude_degrees=state.latitude_degrees,
                longitude_degrees=state.longitude_degrees,
                altitude_hae_meters=_double(state.altitude_hae_meters),
            ),
        ),
        provenance=Provenance(
            integration_name=config.integration_name,
            data_type="camera",
            source_update_time=_timestamp(now),
        ),
        health=Health(
            connection_status=ConnectionStatus.ONLINE,
            health_status=HealthStatus.HEALTHY,
            update_time=_timestamp(now),
        ),
        sensors=Sensors(
            sensors=[
                Sensor(
                    sensor_id="rpi-cam-0",
                    sensor_type=SensorType.CAMERA,
                    operational_state=operational_state,
                    sensor_description="Raspberry Pi Camera Module (EO)",
                )
            ]
        ),
        media=media,
        task_catalog=task_catalog,
    )
    return PublishEntityRequest(entity=entity)
