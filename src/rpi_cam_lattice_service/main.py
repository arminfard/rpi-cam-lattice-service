"""CLI entry point.

Loads config, builds the Lattice client, brings the camera stream up (registers
an SRT video ingress with Lattice's VideoManager so MediaMTX has somewhere to
push and the entity can advertise the video id), then runs the publishing
service and the task handler for the camera entity. On exit the stream is
stopped and its ingress archived.
"""

from __future__ import annotations

import argparse

from . import config as config_module
from .camera.control import CameraControl
from .camera.entity import build_camera_publish_request
from .camera.ingress import VideoIngress
from .camera.source import CameraSource
from .config import Config, ConfigError
from .lattice import LatticeClient
from .logging_setup import configure, get_logger
from .service import Service
from .tasking.handler import TaskHandler


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


def _build_control(client: LatticeClient, config: Config, logger) -> CameraControl:
    """Wire the task-driven camera control, with the video ingress when enabled."""
    video = None
    if config.video_enabled:
        video = VideoIngress(
            client,
            entity_id=config.entity_id,
            title=config.video_title or f"{config.entity_name} (SRT)",
            passphrase=config.srt_passphrase,
            srt_target_file=config.srt_target_file,
        )
    else:
        logger.info("video ingress disabled (VIDEO_ENABLED=false)")
    return CameraControl(
        start_command=config.task_start_command,
        stop_command=config.task_stop_command,
        streaming=False,
        video=video,
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

    control = _build_control(client, config, logger)
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
        catalog_urls = task_handler.task_specification_urls if task_handler else None

        def request_builder(entity_id, created_time, state):
            # Read the live control state every tick so Start/Stop show up on
            # the entity (sensor state, and the Media item only while an
            # ingress exists).
            return build_camera_publish_request(
                config,
                entity_id,
                created_time,
                state,
                video_id=control.video_id,
                task_specification_urls=catalog_urls,
                streaming=control.streaming,
            )

        service = Service(
            config,
            client,
            source=source,
            request_builder=request_builder,
            entity_id=config.entity_id,
            task_handler=task_handler,
        )
        # Start/Stop push their outcome to the asset immediately (Media item
        # added or cleared) instead of waiting for the next 1 Hz tick.
        control.on_change = service.publish_now

        _initial_start(control, logger)
        return service.run()
    finally:
        # Leave nothing pushing at, or advertised as, a stream that no longer
        # has a producer.
        control.shutdown()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
