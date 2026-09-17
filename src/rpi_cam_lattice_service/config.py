"""Configuration loading and validation.

Config is read from a ``.env`` file (path supplied via ``--config``) with any
real environment variables taking precedence, so the same code works for local
development (a ``.env`` file) and for a systemd deployment (``Environment=`` /
``EnvironmentFile=``).

Parsing is strict on purpose: a value that is *present* but malformed (for
example ``CAMERA_LATITUDE=abc``) raises ``ConfigError`` naming the key rather
than silently falling back to a default. Defaults are demo values, and a
typo that quietly places the camera on the demo site is worse than a refusal
to start. Empty or missing values do fall back to the defaults.

Configuration is read once at startup; changing it requires a restart.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from dotenv import dotenv_values


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}


def _parse_bool(key: str, value: str, default: bool) -> bool:
    text = value.strip().lower()
    if not text:
        return default
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ConfigError(
        f"{key} must be a boolean (true/false, 1/0, yes/no, on/off), got {value.strip()!r}"
    )


def _parse_float(key: str, value: str, default: float) -> float:
    text = value.strip()
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        raise ConfigError(f"{key} must be a number, got {text!r}") from None


def _parse_int(key: str, value: str, default: int) -> int:
    text = value.strip()
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        raise ConfigError(f"{key} must be an integer, got {text!r}") from None


@dataclass
class Config:
    """Runtime configuration for the integration."""

    lattice_endpoint: str = ""
    client_id: str = ""
    client_secret: str = ""
    environment_token: str = ""
    sandboxes_token: str = ""
    # Path to a PEM CA bundle (an Anduril-issued certificate) used to verify the
    # Lattice server's TLS certificate when connecting to an offline / air-gapped
    # environment whose private CA is not in the system trust store. This is the
    # recommended way to connect at the edge: TLS verification stays fully
    # enabled. See https://developer.anduril.com/guides/... "Connect to offline
    # environments".
    ca_cert_path: str = ""

    # --- Camera entity ---
    # Stable entity id (a fixed camera keeps its identity across restarts).
    entity_id: str = "rpi-cam-01"
    entity_name: str = "RPi Camera"
    platform_type: str = "Camera"
    integration_name: str = "rpi-cam-lattice-service"
    # The camera's fixed location (it doesn't move). Defaults are demo coords —
    # set real values for your deployment.
    camera_latitude: float = 49.65108
    camera_longitude: float = 11.79045
    camera_altitude_hae_meters: float = 1048.0

    # --- Persistent state ---
    # Small JSON file holding what must outlive the process (the current video
    # ingress, the entity's created_time). See ``state.py``.
    state_file: str = "./state.json"

    # --- Video (Lattice VideoManager SRT ingress) ---
    # When enabled, every Start (including boot) registers a new SRT ingress
    # stream with Lattice's VideoManager, advertises the returned video id on
    # the entity (Media -> MediaItem), and writes the SRT push URL for MediaMTX
    # to consume; every Stop (including exit) archives the ingress again.
    video_enabled: bool = True
    video_title: str = ""
    srt_passphrase: str = ""
    # Where to write the SRT push URL Lattice returns, as an EnvironmentFile
    # (SRT_TARGET=<url>) that the MediaMTX systemd unit loads.
    srt_target_file: str = "./srt_target.env"
    # MediaMTX control API used to observe whether the camera path is actually
    # producing frames and whether the SRT pusher is attached.
    mediamtx_api_url: str = "http://127.0.0.1:9997"
    mediamtx_path: str = "cam"

    # --- Tasking (Lattice Tasks API) ---
    # When enabled, the entity advertises a task_catalog and the service listens
    # for tasks routed to it (ListenAsAgent), executing Start/Stop.
    tasking_enabled: bool = True
    # Protobuf package of the custom task definitions pushed to the Lattice
    # Schema Registry (see task-def/). Type URLs are
    # type.googleapis.com/<package>.Start and .Stop.
    task_package: str = "anduril.sample_app_rpi_cam.camera.v1alpha"
    # Shell commands run to start/stop the video stream (empty = state only).
    task_start_command: str = ""
    task_stop_command: str = ""
    # Heartbeat interval requested on the agent stream (0 disables heartbeats).
    task_heartbeat_interval_ms: int = 30000

    # Fields not sourced from the environment.
    _config_path: str = field(default="", repr=False)

    @property
    def uses_static_token(self) -> bool:
        """Prefer the long-lived static bearer token when it is present.

        When both a static token and OAuth client credentials are configured we
        default to the static token.
        """
        return bool(self.environment_token)

    def validate(self) -> None:
        if not self.lattice_endpoint:
            raise ConfigError("LATTICE_ENDPOINT is required")
        has_static = bool(self.environment_token)
        has_oauth = bool(self.client_id and self.client_secret)
        if not (has_static or has_oauth):
            raise ConfigError(
                "authentication is required: set ENVIRONMENT_TOKEN, "
                "or both CLIENT_ID and CLIENT_SECRET"
            )
        if self.ca_cert_path and not os.path.exists(self.ca_cert_path):
            raise ConfigError(f"LATTICE_CA_CERT_PATH does not exist: {self.ca_cert_path}")
        if not -90.0 <= self.camera_latitude <= 90.0:
            raise ConfigError(f"CAMERA_LATITUDE must be in [-90, 90], got {self.camera_latitude}")
        if not -180.0 <= self.camera_longitude <= 180.0:
            raise ConfigError(
                f"CAMERA_LONGITUDE must be in [-180, 180], got {self.camera_longitude}"
            )
        if not math.isfinite(self.camera_altitude_hae_meters):
            raise ConfigError(
                f"CAMERA_ALTITUDE_HAE_METERS must be finite, got {self.camera_altitude_hae_meters}"
            )
        if self.tasking_enabled and not self.task_package:
            raise ConfigError("TASK_PACKAGE is required when TASKING_ENABLED=true")
        if self.task_heartbeat_interval_ms < 0:
            raise ConfigError(
                f"TASK_HEARTBEAT_INTERVAL_MS must be >= 0, got {self.task_heartbeat_interval_ms}"
            )


def load(path: str = ".env") -> Config:
    """Load configuration from ``path`` (a .env file) merged with os.environ.

    Values present in the real environment override values in the file, so a
    systemd unit can inject secrets without a file on disk. Malformed numeric
    or boolean values raise ``ConfigError``.
    """
    file_values = dotenv_values(path) if path and os.path.exists(path) else {}

    def get(key: str) -> str:
        # Real environment wins over the file.
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val
        return file_values.get(key) or ""

    defaults = Config()

    def text(key: str, default: str = "") -> str:
        return get(key).strip() or default

    cfg = Config(
        lattice_endpoint=text("LATTICE_ENDPOINT"),
        client_id=text("CLIENT_ID"),
        client_secret=text("CLIENT_SECRET"),
        environment_token=text("ENVIRONMENT_TOKEN"),
        sandboxes_token=text("SANDBOXES_TOKEN"),
        ca_cert_path=text("LATTICE_CA_CERT_PATH"),
        entity_id=text("ENTITY_ID", defaults.entity_id),
        entity_name=text("ENTITY_NAME", defaults.entity_name),
        platform_type=text("PLATFORM_TYPE", defaults.platform_type),
        integration_name=text("INTEGRATION_NAME", defaults.integration_name),
        camera_latitude=_parse_float(
            "CAMERA_LATITUDE", get("CAMERA_LATITUDE"), defaults.camera_latitude
        ),
        camera_longitude=_parse_float(
            "CAMERA_LONGITUDE", get("CAMERA_LONGITUDE"), defaults.camera_longitude
        ),
        camera_altitude_hae_meters=_parse_float(
            "CAMERA_ALTITUDE_HAE_METERS",
            get("CAMERA_ALTITUDE_HAE_METERS"),
            defaults.camera_altitude_hae_meters,
        ),
        state_file=text("STATE_FILE", defaults.state_file),
        video_enabled=_parse_bool("VIDEO_ENABLED", get("VIDEO_ENABLED"), defaults.video_enabled),
        video_title=text("VIDEO_TITLE"),
        srt_passphrase=text("SRT_PASSPHRASE"),
        srt_target_file=text("SRT_TARGET_FILE", defaults.srt_target_file),
        mediamtx_api_url=text("MEDIAMTX_API_URL", defaults.mediamtx_api_url),
        mediamtx_path=text("MEDIAMTX_PATH", defaults.mediamtx_path),
        tasking_enabled=_parse_bool(
            "TASKING_ENABLED", get("TASKING_ENABLED"), defaults.tasking_enabled
        ),
        task_package=text("TASK_PACKAGE", defaults.task_package),
        task_start_command=text("TASK_START_COMMAND"),
        task_stop_command=text("TASK_STOP_COMMAND"),
        task_heartbeat_interval_ms=_parse_int(
            "TASK_HEARTBEAT_INTERVAL_MS",
            get("TASK_HEARTBEAT_INTERVAL_MS"),
            defaults.task_heartbeat_interval_ms,
        ),
        _config_path=path,
    )
    return cfg
