"""CLI entry point.

Loads config, builds the Lattice client, registers an SRT video ingress with
Lattice's VideoManager (so MediaMTX has somewhere to push and the entity can
advertise the video id), then runs the publishing service for the camera entity.
"""

from __future__ import annotations

import argparse
import os

from . import config as config_module
from .config import Config, ConfigError
from .control import CameraControl
from .lattice_client import LatticeClient, SrtIngressInfo
from .logging_setup import configure, get_logger
from .service import Service
from .sources import CameraSource
from .tasking import TaskHandler
from .worker import build_camera_publish_request


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


def _register_video_ingress(
    client: LatticeClient, config: Config, logger
) -> SrtIngressInfo | None:
    """Register the SRT ingress with Lattice and persist the push URL.

    Best-effort: on failure the service still publishes the entity (without a
    video reference), which matters when the endpoint isn't reachable yet.
    """
    if not config.video_enabled:
        logger.info("video ingress disabled (VIDEO_ENABLED=false)")
        return None
    title = config.video_title or f"{config.entity_name} (SRT)"
    try:
        info = client.create_srt_ingress(
            title=title,
            passphrase=config.srt_passphrase,
            ingress_id=config.entity_id,
        )
    except Exception as exc:
        logger.warning(
            "could not register SRT ingress with VideoManager; publishing "
            "without a video reference",
            error=str(exc),
        )
        return None

    logger.info(
        "registered SRT ingress",
        video_id=info.video_id,
        push_url=info.push_url,
    )
    _write_srt_target(config.srt_target_file, info.push_url, logger)
    return info


def _write_srt_target(path: str, push_url: str, logger) -> None:
    """Write the SRT push URL as an EnvironmentFile for the MediaMTX unit."""
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w") as handle:
            handle.write(f"SRT_TARGET={push_url}\n")
        logger.info("wrote SRT target for MediaMTX", srt_target_file=path)
    except OSError as exc:
        logger.warning("could not write SRT target file", path=path, error=str(exc))


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

    try:
        info = _register_video_ingress(client, config, logger)
        video_id = info.video_id if info else None

        source = CameraSource(
            latitude_degrees=config.camera_latitude,
            longitude_degrees=config.camera_longitude,
            altitude_hae_meters=config.camera_altitude_hae_meters,
        )

        # Task-driven camera state. The publish loop reads it every tick so the
        # entity reflects Start/Stop; the task handler writes it.
        control = CameraControl(
            start_command=config.task_start_command,
            stop_command=config.task_stop_command,
            streaming=True,
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
            return build_camera_publish_request(
                config,
                entity_id,
                created_time,
                state,
                video_id=video_id,
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
        return service.run()
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
