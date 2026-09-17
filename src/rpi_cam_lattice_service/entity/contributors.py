"""Entity contributors: each one fills a single component of the entity.

The entity published to Lattice is composed from independent parts (location,
sensors, media, task catalog, health, ...). Rather than threading every part
through one ever-growing function signature, each part is an
``EntityContributor`` whose ``apply`` sets its component on the ``Entity``
under construction. ``EntityBuilder`` runs them in order after laying down
the identity floor from ``base.py``.

Contributors depend only on plain callables and small dataclasses, never on
the camera control, pipeline or client types, so the composition stays
decoupled from how the observations are produced. ``main.py`` adapts the real
sources (``lambda: CameraObservation(...)``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from anduril.entitymanager.v1.entity_pub_pb import Entity
from anduril.entitymanager.v1.health_status_pub_pb import (
    ConnectionStatus,
    Health,
    HealthStatus,
)
from anduril.entitymanager.v1.location_pub_pb import Location, Position
from anduril.entitymanager.v1.media_pub_pb import Media, MediaItem, MediaType
from anduril.entitymanager.v1.sensors_pub_pb import (
    OperationalState,
    Sensor,
    Sensors,
    SensorType,
)
from anduril.tasks.v2.catalog_pub_pb import TaskCatalog, TaskDefinition
from protobuf.wkt import timestamp_pb, wrappers_pb

from ..camera.source import CameraSource


@dataclass(frozen=True)
class BuildContext:
    """Per-build inputs shared by every contributor.

    ``offline`` marks the final publish at shutdown: the entity is still
    published (so operators see the asset go dark deliberately rather than
    expire), but every component reports the camera as off/offline.
    """

    now: datetime
    offline: bool = False


class EntityContributor(Protocol):
    """One component of the entity. ``apply`` mutates ``entity`` in place."""

    def apply(self, entity: Entity, ctx: BuildContext) -> None: ...


@dataclass(frozen=True)
class CameraObservation:
    """What is currently known about the camera stream.

    ``desired_on`` is the operator's intent (Start/Stop tasks); ``ready`` is
    whether the media pipeline is observed to be producing frames, or ``None``
    when no probe is available.
    """

    desired_on: bool
    ready: bool | None


def _timestamp(dt: datetime) -> timestamp_pb.Timestamp:
    return timestamp_pb.Timestamp.from_datetime(dt)


class LocationContributor:
    """Sets ``Location`` from the camera source's current geodetic position."""

    def __init__(self, source: CameraSource) -> None:
        self._source = source

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        state = self._source.next_state()
        entity.location = Location(
            position=Position(
                latitude_degrees=state.latitude_degrees,
                longitude_degrees=state.longitude_degrees,
                altitude_hae_meters=wrappers_pb.DoubleValue(value=state.altitude_hae_meters),
            ),
        )


class SensorsContributor:
    """Sets the single EO camera ``Sensor`` with its operational state.

    State rule: offline or not desired on -> OFF; desired on and observed
    ready (or readiness unknown) -> OPERATIONAL; desired on but observed not
    ready -> NON_OPERATIONAL. Unknown readiness is optimistic because the
    probe is optional and its absence should not mark a working camera down.
    """

    def __init__(
        self,
        observe: Callable[[], CameraObservation],
        *,
        sensor_id: str = "rpi-cam-0",
        description: str = "Raspberry Pi Camera Module (EO)",
    ) -> None:
        self._observe = observe
        self._sensor_id = sensor_id
        self._description = description

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        obs = self._observe()
        entity.sensors = Sensors(
            sensors=[
                Sensor(
                    sensor_id=self._sensor_id,
                    sensor_type=SensorType.CAMERA,
                    operational_state=self._state(obs, ctx),
                    sensor_description=self._description,
                )
            ]
        )

    @staticmethod
    def _state(obs: CameraObservation, ctx: BuildContext) -> OperationalState:
        if ctx.offline or not obs.desired_on:
            return OperationalState.OFF
        if obs.ready is False:
            return OperationalState.NON_OPERATIONAL
        return OperationalState.OPERATIONAL


class MediaContributor:
    """Advertises the live video id on ``Media``.

    ``video_id`` returns the id Lattice's VideoManager assigned to the current
    SRT ingress, or ``None`` when there is no stream. ``Media`` is then
    published explicitly empty (not absent) so a previously advertised item is
    cleared on the Lattice side.
    """

    def __init__(self, video_id: Callable[[], str | None]) -> None:
        self._video_id = video_id

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        video_id = self._video_id()
        items = [MediaItem(item_identifier=video_id, type=MediaType.VIDEO)] if video_id else []
        entity.media = Media(media=items)


class TaskCatalogContributor:
    """Advertises the task specification URLs the camera accepts.

    An operator can only assign a task the catalog lists. With no URLs the
    component is left unset (tasking disabled).
    """

    def __init__(self, urls: list[str]) -> None:
        self._urls = list(urls)

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        if not self._urls:
            return
        entity.task_catalog = TaskCatalog(
            task_definitions=[TaskDefinition(task_specification_url=url) for url in self._urls]
        )


class StaticHealthContributor:
    """Placeholder health: ONLINE/HEALTHY, or OFFLINE on the shutdown publish.

    This reports nothing observed about the device. It exists so the entity
    carries a ``Health`` component from day one; wave 2 replaces it with a
    ``HealthContributor`` fed by real telemetry (thermal, power, pipeline,
    tasking stream) sampled on a background worker.
    """

    def apply(self, entity: Entity, ctx: BuildContext) -> None:
        entity.health = Health(
            connection_status=(
                ConnectionStatus.OFFLINE if ctx.offline else ConnectionStatus.ONLINE
            ),
            health_status=HealthStatus.HEALTHY,
            update_time=_timestamp(ctx.now),
        )
