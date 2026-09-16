"""Builds the Lattice entity for the camera from its state and control.

Maps a ``CameraState`` snapshot, plus the task-driven stream state, onto a
Lattice ``Entity`` protobuf message published via ``PublishEntity``: a
friendly, land-based asset carrying an EO camera sensor, its health, the
advertised video (``Media``), and the task catalog.
"""

from __future__ import annotations

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

from ..config import Config
from .source import CameraState

# Keep the entity live: expiry is short and we re-publish on every tick.
ENTITY_EXPIRY_SECONDS = 10


def _double(value: float) -> wrappers_pb.DoubleValue:
    return wrappers_pb.DoubleValue(value=value)


def _timestamp(dt: datetime) -> timestamp_pb.Timestamp:
    return timestamp_pb.Timestamp.from_datetime(dt)


def build_camera_publish_request(
    config: Config,
    entity_id: str,
    created_time: datetime,
    state: CameraState,
    *,
    video_id: str | None = None,
    task_specification_urls: list[str] | None = None,
    streaming: bool = True,
    now: datetime | None = None,
) -> PublishEntityRequest:
    """Map a stationary camera ``CameraState`` onto a Lattice ``Entity`` publish request.

    When ``video_id`` is provided (the id returned by Lattice's VideoManager
    ``CreateIngressStream``), it is advertised on the entity's ``Media``
    component so operators can locate the live feed; otherwise ``Media`` is
    published explicitly empty so a stale item is cleared.

    ``task_specification_urls`` are advertised in the entity's ``task_catalog``
    (an operator can only assign a task the catalog lists). ``streaming`` is the
    task-driven camera state: a stopped stream reports the sensor as ``OFF``.
    """
    now = now or datetime.now(timezone.utc)

    media = Media(media=[])
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
        description="Raspberry Pi Camera Module",
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
