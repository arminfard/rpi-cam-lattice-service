"""CLI entry point: configuration, wiring, and the run/shutdown sequence.

This module is deliberately *only* wiring. Every feature lives in its own
package and is plugged in here:

* ``lattice``  — the client (transport, auth, one thin client per API)
* ``camera``   — the media pipeline, the Start/Stop control, the video ingress
* ``entity``   — the composition seam: a base entity plus one *contributor*
                 per component (location, sensors, media, task catalog, health)
* ``tasking``  — the agent stream that executes Start/Stop
* ``health``   — probes sampled on their own cadence, published as ``Health``

Adding a feature means adding a package and a few lines in ``_build_*`` below,
never changing the publish loop or the other packages.

Run sequence: recover any ingress a previous process left behind, bring the
stream up (best-effort), run until a signal, then stop the stream, archive the
ingress, and publish one final entity marked offline.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from . import config as config_module
from .camera.control import CameraControl
from .camera.ingress import VideoIngress
from .camera.pipeline import CommandPipeline, MediaMtxStatusProbe
from .camera.source import CameraSource
from .config import Config, ConfigError
from .entity import (
    CameraObservation,
    EntityBuilder,
    EntityContributor,
    LocationContributor,
    MediaContributor,
    SensorsContributor,
    StaticHealthContributor,
    TaskCatalogContributor,
)
from .lattice import LatticeClient
from .logging_setup import configure, get_logger
from .service import Service
from .state import StateStore
from .tasking.handler import TaskHandler

CREATED_TIME_KEY = "created_time"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rpi-cam-lattice-service",
        description="Publish a Raspberry Pi camera as a Lattice entity via gRPC (Connect).",
    )
    parser.add_argument(
        "--config", default=".env", help="path to the .env config file (default: .env)"
    )
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    return parser.parse_args(argv)


def _created_time(state: StateStore) -> datetime:
    """A stable-id asset keeps one creation time across restarts."""
    raw = state.get(CREATED_TIME_KEY)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    created = datetime.now(UTC)
    state.set(CREATED_TIME_KEY, created.isoformat())
    return created


def _build_pipeline(config: Config) -> CommandPipeline:
    """The media pipeline: shell commands to start/stop MediaMTX, observed via its API."""
    probe = MediaMtxStatusProbe(api_url=config.mediamtx_api_url, path=config.mediamtx_path)
    return CommandPipeline(
        start_command=config.task_start_command,
        stop_command=config.task_stop_command,
        probe=probe,
    )


def _build_control(
    client: LatticeClient, config: Config, state: StateStore, pipeline: CommandPipeline, logger
) -> CameraControl:
    """Wire the task-driven camera control, with the video ingress when enabled."""
    video = None
    if config.video_enabled:
        video = VideoIngress(
            client,
            title=config.video_title or f"{config.entity_name} (SRT)",
            passphrase=config.srt_passphrase,
            srt_target_file=config.srt_target_file,
            state=state,
        )
    else:
        logger.info("video ingress disabled (VIDEO_ENABLED=false)")
    return CameraControl(pipeline=pipeline, video=video)


def _build_entity_builder(
    config: Config,
    *,
    created_time: datetime,
    source: CameraSource,
    control: CameraControl,
    pipeline: CommandPipeline,
    task_handler: TaskHandler | None,
) -> EntityBuilder:
    """Compose the entity: identity floor plus one contributor per component."""

    def observe() -> CameraObservation:
        snapshot = control.snapshot()
        ready = pipeline.status().ready if snapshot.desired_on else None
        return CameraObservation(desired_on=snapshot.desired_on, ready=ready)

    contributors: list[EntityContributor] = [
        LocationContributor(source),
        SensorsContributor(observe),
        MediaContributor(lambda: control.snapshot().video_id),
        StaticHealthContributor(),
    ]
    if task_handler is not None:
        contributors.append(TaskCatalogContributor(task_handler.task_specification_urls))
    return EntityBuilder(
        config,
        entity_id=config.entity_id,
        created_time=created_time,
        contributors=contributors,
    )


def _initial_start(control: CameraControl, logger) -> None:
    """Bring the stream up at boot.

    Best-effort: on failure (for example an unreachable endpoint) the service
    still publishes the camera entity, reporting the sensor OFF and no video,
    and an operator's Start task is the retry.
    """
    try:
        control.start()
    except Exception as exc:
        logger.warning(
            "could not start the camera stream at boot; publishing with the "
            "sensor OFF and no video reference (send a Start task to retry)",
            error=str(exc),
        )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    configure(debug=args.debug)
    logger = get_logger()

    try:
        config = config_module.load(args.config)
        config.validate()
    except ConfigError as exc:
        logger.error("invalid configuration", error=str(exc))
        return 2

    try:
        client = LatticeClient(config)
    except Exception as exc:
        logger.error("failed to initialize Lattice client", error=str(exc))
        return 1

    state = StateStore(config.state_file)
    pipeline = _build_pipeline(config)
    control = _build_control(client, config, state, pipeline, logger)
    service: Service | None = None
    try:
        source = CameraSource(
            latitude_degrees=config.camera_latitude,
            longitude_degrees=config.camera_longitude,
            altitude_hae_meters=config.camera_altitude_hae_meters,
        )

        task_handler = None
        if config.tasking_enabled:
            task_handler = TaskHandler(
                client,
                agent_entity_id=config.entity_id,
                task_package=config.task_package,
                control=control,
                heartbeat_interval_ms=config.task_heartbeat_interval_ms,
            )
        else:
            logger.info("tasking disabled (TASKING_ENABLED=false)")

        builder = _build_entity_builder(
            config,
            created_time=_created_time(state),
            source=source,
            control=control,
            pipeline=pipeline,
            task_handler=task_handler,
        )
        workers = [("tasking", task_handler.run)] if task_handler is not None else []
        service = Service(config, client, builder=builder, workers=workers)
        # Start/Stop push their outcome to the asset immediately (Media item
        # added or cleared) instead of waiting for the next 1 Hz tick.
        control.on_change = service.publish_now

        # Archive an ingress a crashed predecessor never cleaned up, then
        # register a fresh one.
        control.recover()
        _initial_start(control, logger)
        return service.run()
    finally:
        # Leave nothing pushing at, or advertised as, a stream that no longer
        # has a producer, and tell operators the asset went offline on purpose.
        control.shutdown()
        if service is not None:
            service.publish_offline()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
