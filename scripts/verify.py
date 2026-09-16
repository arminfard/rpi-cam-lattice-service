"""Live verification driver — run this SEPARATELY from the daemon, on a network
where the Lattice endpoint is reachable.

It constructs its own client and reads back the camera entity the daemon
publishes, proving the publish -> read-back round-trip (the definition of done).
The camera uses a stable entity_id (config ENTITY_ID), so --entity-id defaults
to it.

Usage:
    python scripts/verify.py --config .env [--entity-id rpi-cam-01]
"""

from __future__ import annotations

import argparse
import sys

# Make ``src`` importable when run directly from the repo root.
sys.path.insert(0, "src")

from rpi_cam_lattice_service import config as config_module  # noqa: E402
from rpi_cam_lattice_service.lattice import LatticeClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Read a published entity back from Lattice.")
    parser.add_argument("--config", default=".env")
    parser.add_argument(
        "--entity-id",
        default=None,
        help="entity id to read back (defaults to config ENTITY_ID)",
    )
    args = parser.parse_args()

    config = config_module.load(args.config)
    config.validate()
    entity_id = args.entity_id or config.entity_id

    with LatticeClient(config) as client:
        response = client.get_entity(entity_id, timeout_ms=30000)
        entity = response.entity
        print("entity_id:      ", entity.entity_id)
        print("is_live:        ", entity.is_live)
        print("aliases.name:   ", entity.aliases.name)
        pos = entity.location.position
        print(
            "position:       ",
            f"lat={pos.latitude_degrees:.5f} lon={pos.longitude_degrees:.5f}",
        )
        if entity.media is not None and entity.media.media:
            print("video id (media):", entity.media.media[0].item_identifier)
        else:
            print("video id (media): <none advertised>")
        if entity.sensors is not None and entity.sensors.sensors:
            print("sensor state:   ", entity.sensors.sensors[0].operational_state.name)
        if entity.task_catalog is not None and entity.task_catalog.task_definitions:
            print("task catalog:")
            for definition in entity.task_catalog.task_definitions:
                print("   ", definition.task_specification_url)
        else:
            print("task catalog:    <none advertised>")
        print("expiry_time:    ", entity.expiry_time.to_datetime().isoformat())
        if not entity.is_live:
            print("WARNING: entity is not live", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
