"""Entity composition: identity floor, contributors and the builder."""

from datetime import UTC, datetime, timedelta

import pytest
from anduril.entitymanager.v1.classification_pub_pb import ClassificationLevels
from anduril.entitymanager.v1.entity_manager_api_pub_pb import PublishEntityRequest
from anduril.entitymanager.v1.health_status_pub_pb import ConnectionStatus, HealthStatus
from anduril.entitymanager.v1.media_pub_pb import MediaType
from anduril.entitymanager.v1.sensors_pub_pb import OperationalState, SensorType
from anduril.entitymanager.v1.types_pub_pb import Template
from anduril.ontology.v1.type_pub_pb import Disposition, Environment

from rpi_cam_lattice_service.camera.source import CameraSource
from rpi_cam_lattice_service.config import Config
from rpi_cam_lattice_service.entity import (
    ENTITY_EXPIRY_SECONDS,
    BuildContext,
    CameraObservation,
    EntityBuilder,
    LocationContributor,
    MediaContributor,
    SensorsContributor,
    StaticHealthContributor,
    TaskCatalogContributor,
    base_entity,
    sidc_for,
)

NOW = datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)
CREATED = NOW - timedelta(hours=1)
URLS = [
    "type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Start",
    "type.googleapis.com/anduril.sample_app_rpi_cam.camera.v1alpha.Stop",
]


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


def _source(cfg: Config) -> CameraSource:
    return CameraSource(cfg.camera_latitude, cfg.camera_longitude, cfg.camera_altitude_hae_meters)


def _builder(
    cfg: Config,
    *,
    desired_on: bool = True,
    ready: bool | None = None,
    video_id: str | None = None,
    urls: list[str] | None = None,
) -> EntityBuilder:
    return EntityBuilder(
        cfg,
        entity_id=cfg.entity_id,
        created_time=CREATED,
        contributors=[
            LocationContributor(_source(cfg)),
            SensorsContributor(lambda: CameraObservation(desired_on=desired_on, ready=ready)),
            MediaContributor(lambda: video_id),
            TaskCatalogContributor(urls or []),
            StaticHealthContributor(),
        ],
    )


# -- base entity -----------------------------------------------------------


def test_base_entity_identity():
    cfg = _config()
    e = base_entity(cfg, entity_id="rpi-cam-01", created_time=CREATED, now=NOW)
    assert e.entity_id == "rpi-cam-01"
    assert e.description == "Raspberry Pi Camera Module"
    assert e.is_live is True
    assert e.created_time.to_datetime() == CREATED
    assert e.expiry_time.to_datetime() == NOW + timedelta(seconds=ENTITY_EXPIRY_SECONDS)
    assert e.aliases.name == "Front Gate Camera"
    assert e.ontology.template == Template.ASSET
    assert e.ontology.platform_type == "Camera"
    assert e.mil_view.disposition == Disposition.FRIENDLY
    assert e.mil_view.environment == Environment.LAND
    assert e.provenance.integration_name == "rpi-cam-lattice-service"
    assert e.provenance.data_type == "camera"
    assert e.provenance.source_update_time.to_datetime() == NOW
    # No observed components on the floor.
    assert e.location is None
    assert e.sensors is None
    assert e.media is None
    assert e.health is None
    assert e.task_catalog is None


def test_base_entity_classification_and_symbology():
    e = base_entity(_config(), entity_id="x", created_time=CREATED, now=NOW)
    assert e.data_classification.default.level == ClassificationLevels.UNCLASSIFIED
    assert e.symbology.standard.field == "mil_std_2525_c"
    assert e.symbology.standard.value.sidc == "SFGP-----------"


@pytest.mark.parametrize(
    ("disposition", "environment", "expected"),
    [
        (Disposition.FRIENDLY, Environment.LAND, "SFGP-----------"),
        (Disposition.HOSTILE, Environment.AIR, "SHAP-----------"),
        (Disposition.NEUTRAL, Environment.SURFACE, "SNSP-----------"),
        (Disposition.UNKNOWN, Environment.UNKNOWN, "SUZP-----------"),
    ],
)
def test_sidc_derivation(disposition, environment, expected):
    sidc = sidc_for(disposition, environment)
    assert sidc == expected
    assert len(sidc) == 15


# -- builder ---------------------------------------------------------------


def test_builder_produces_full_entity():
    cfg = _config()
    req = _builder(cfg, video_id="ingress-abc-123", urls=URLS).build(now=NOW)
    assert isinstance(req, PublishEntityRequest)
    e = req.entity
    assert e.entity_id == "rpi-cam-01"
    assert e.aliases.name == "Front Gate Camera"
    assert e.location.position.latitude_degrees == 37.1234
    assert e.location.position.longitude_degrees == -122.5678
    assert e.location.position.altitude_hae_meters.value == 30.0
    assert e.sensors.sensors[0].sensor_id == "rpi-cam-0"
    assert e.sensors.sensors[0].sensor_type == SensorType.CAMERA
    assert e.sensors.sensors[0].operational_state == OperationalState.OPERATIONAL
    assert e.sensors.sensors[0].sensor_description == "Raspberry Pi Camera Module (EO)"
    assert e.media.media[0].item_identifier == "ingress-abc-123"
    assert e.media.media[0].type == MediaType.VIDEO
    assert [d.task_specification_url for d in e.task_catalog.task_definitions] == URLS
    assert e.health.connection_status == ConnectionStatus.ONLINE
    assert e.health.health_status == HealthStatus.HEALTHY
    assert e.health.update_time.to_datetime() == NOW
    assert len(req.to_binary()) > 0


def test_builder_defaults_now_to_utc_now():
    before = datetime.now(UTC)
    req = _builder(_config()).build()
    expiry = req.entity.expiry_time.to_datetime()
    assert before + timedelta(seconds=ENTITY_EXPIRY_SECONDS) <= expiry
    assert expiry <= datetime.now(UTC) + timedelta(seconds=ENTITY_EXPIRY_SECONDS)


def test_builder_exposes_identity():
    b = _builder(_config())
    assert b.entity_id == "rpi-cam-01"
    assert b.created_time == CREATED


def test_contributors_applied_in_order():
    order: list[str] = []

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def apply(self, entity, ctx) -> None:
            order.append(self.name)
            # Later contributors see (and may override) earlier work.
            entity.description = self.name

    b = EntityBuilder(
        _config(),
        entity_id="x",
        created_time=CREATED,
        contributors=[Recorder("first"), Recorder("second"), Recorder("third")],
    )
    req = b.build(now=NOW)
    assert order == ["first", "second", "third"]
    assert req.entity.description == "third"


def test_contributors_receive_context():
    seen: list[BuildContext] = []

    class Spy:
        def apply(self, entity, ctx) -> None:
            seen.append(ctx)

    b = EntityBuilder(_config(), entity_id="x", created_time=CREATED, contributors=[Spy()])
    b.build(now=NOW)
    b.build(now=NOW, offline=True)
    assert seen == [BuildContext(now=NOW), BuildContext(now=NOW, offline=True)]


# -- sensors ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("desired_on", "ready", "offline", "expected"),
    [
        (True, True, False, OperationalState.OPERATIONAL),
        (True, None, False, OperationalState.OPERATIONAL),
        (True, False, False, OperationalState.NON_OPERATIONAL),
        (False, True, False, OperationalState.OFF),
        (False, None, False, OperationalState.OFF),
        (False, False, False, OperationalState.OFF),
        (True, True, True, OperationalState.OFF),
        (True, None, True, OperationalState.OFF),
        (True, False, True, OperationalState.OFF),
        (False, True, True, OperationalState.OFF),
    ],
)
def test_sensor_operational_state(desired_on, ready, offline, expected):
    req = _builder(_config(), desired_on=desired_on, ready=ready).build(now=NOW, offline=offline)
    assert req.entity.sensors.sensors[0].operational_state == expected


def test_sensor_id_and_description_are_configurable():
    e = base_entity(_config(), entity_id="x", created_time=CREATED, now=NOW)
    SensorsContributor(
        lambda: CameraObservation(desired_on=True, ready=True),
        sensor_id="thermal-1",
        description="IR",
    ).apply(e, BuildContext(now=NOW))
    assert e.sensors.sensors[0].sensor_id == "thermal-1"
    assert e.sensors.sensors[0].sensor_description == "IR"


# -- media -----------------------------------------------------------------


def test_video_id_advertised_as_media_item():
    req = _builder(_config(), video_id="ingress-abc-123").build(now=NOW)
    item = req.entity.media.media[0]
    assert item.item_identifier == "ingress-abc-123"
    assert item.type == MediaType.VIDEO


@pytest.mark.parametrize("video_id", [None, ""])
def test_no_video_id_means_empty_media_list(video_id):
    req = _builder(_config(), video_id=video_id).build(now=NOW)
    # Explicitly empty (not absent) so a previously advertised item is cleared.
    assert req.entity.media is not None
    assert list(req.entity.media.media) == []
    assert len(req.to_binary()) > 0


def test_media_reflects_current_value_each_build():
    current: dict[str, str | None] = {"id": "vid-1"}
    b = EntityBuilder(
        _config(),
        entity_id="x",
        created_time=CREATED,
        contributors=[MediaContributor(lambda: current["id"])],
    )
    assert b.build(now=NOW).entity.media.media[0].item_identifier == "vid-1"
    current["id"] = None
    assert list(b.build(now=NOW).entity.media.media) == []


# -- task catalog ----------------------------------------------------------


def test_task_catalog_present_when_urls_given():
    req = _builder(_config(), urls=URLS).build(now=NOW)
    assert [d.task_specification_url for d in req.entity.task_catalog.task_definitions] == URLS


def test_task_catalog_absent_when_no_urls():
    req = _builder(_config(), urls=[]).build(now=NOW)
    assert req.entity.task_catalog is None


# -- health ----------------------------------------------------------------


def test_health_online_by_default_and_offline_on_shutdown():
    b = _builder(_config())
    online = b.build(now=NOW).entity.health
    assert online.connection_status == ConnectionStatus.ONLINE
    assert online.health_status == HealthStatus.HEALTHY
    offline = b.build(now=NOW, offline=True).entity.health
    assert offline.connection_status == ConnectionStatus.OFFLINE
    assert offline.update_time.to_datetime() == NOW


# -- location --------------------------------------------------------------


def test_location_reads_source_each_build():
    calls = []

    class CountingSource(CameraSource):
        def next_state(self):
            calls.append(1)
            return super().next_state()

    b = EntityBuilder(
        _config(),
        entity_id="x",
        created_time=CREATED,
        contributors=[LocationContributor(CountingSource(1.0, 2.0, 3.0))],
    )
    b.build(now=NOW)
    pos = b.build(now=NOW).entity.location.position
    assert len(calls) == 2
    assert (pos.latitude_degrees, pos.longitude_degrees, pos.altitude_hae_meters.value) == (
        1.0,
        2.0,
        3.0,
    )
