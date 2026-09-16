"""Configuration loading and validation.

Ports ``internal/config/config.go``. Config is read from a ``.env`` file (path
supplied via ``--config``) with any real environment variables taking
precedence, so the same code works for local development (a ``.env`` file) and
for a systemd deployment (``Environment=`` / ``EnvironmentFile=``).

Note: the Go original had a latent bug where ``config.Load()`` ignored the
``--config`` flag and always read ``.env``. This port honors the supplied path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import dotenv_values


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


_TRUTHY = {"true", "1", "yes", "y", "on"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


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
    # Legacy knob carried over from the Go integration. This gRPC/Connect
    # transport cannot disable TLS verification, so it is not honored; if set we
    # warn and point at ca_cert_path instead.
    skip_tls_verify_requested: bool = False

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

    # --- Video (Lattice VideoManager SRT ingress) ---
    # When enabled, the service registers an SRT ingress stream with Lattice's
    # VideoManager at startup, advertises the returned video id on the entity
    # (Media -> MediaItem), and writes the SRT push URL for MediaMTX to consume.
    video_enabled: bool = True
    video_title: str = ""
    srt_passphrase: str = ""
    # Where to write the SRT push URL Lattice returns, as an EnvironmentFile
    # (SRT_TARGET=<url>) that the MediaMTX systemd unit loads.
    srt_target_file: str = "./srt_target.env"

    # Fields not sourced from the environment.
    _config_path: str = field(default="", repr=False)

    @property
    def uses_static_token(self) -> bool:
        """Prefer the long-lived static bearer token when it is present.

        When both a static token and OAuth client credentials are configured we
        default to the static token, matching the Go integration's precedence.
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
            raise ConfigError(
                f"LATTICE_CA_CERT_PATH does not exist: {self.ca_cert_path}"
            )


def load(path: str = ".env") -> Config:
    """Load configuration from ``path`` (a .env file) merged with os.environ.

    Values present in the real environment override values in the file, so a
    systemd unit can inject secrets without a file on disk.
    """
    file_values = dotenv_values(path) if path and os.path.exists(path) else {}

    def get(key: str) -> str:
        # Real environment wins over the file.
        env_val = os.environ.get(key)
        if env_val is not None:
            return env_val
        return (file_values.get(key) or "")

    cfg = Config(
        lattice_endpoint=get("LATTICE_ENDPOINT").strip(),
        client_id=get("CLIENT_ID").strip(),
        client_secret=get("CLIENT_SECRET").strip(),
        environment_token=get("ENVIRONMENT_TOKEN").strip(),
        sandboxes_token=get("SANDBOXES_TOKEN").strip(),
        ca_cert_path=get("LATTICE_CA_CERT_PATH").strip(),
        skip_tls_verify_requested=_truthy(get("SKIP_TLS_VERIFY")),
        entity_id=(get("ENTITY_ID").strip() or "rpi-cam-01"),
        entity_name=(get("ENTITY_NAME").strip() or "RPi Camera"),
        platform_type=(get("PLATFORM_TYPE").strip() or "Camera"),
        integration_name=(get("INTEGRATION_NAME").strip() or "rpi-cam-lattice-service"),
        camera_latitude=_float(get("CAMERA_LATITUDE"), 49.65108),
        camera_longitude=_float(get("CAMERA_LONGITUDE"), 11.79045),
        camera_altitude_hae_meters=_float(get("CAMERA_ALTITUDE_HAE_METERS"), 1048.0),
        video_enabled=_truthy(get("VIDEO_ENABLED") or "true"),
        video_title=get("VIDEO_TITLE").strip(),
        srt_passphrase=get("SRT_PASSPHRASE").strip(),
        srt_target_file=(get("SRT_TARGET_FILE").strip() or "./srt_target.env"),
        _config_path=path,
    )
    return cfg
