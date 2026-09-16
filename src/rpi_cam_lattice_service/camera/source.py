"""The camera as a sensor source.

``CameraState`` is a plain-Python snapshot of what the camera reports about
itself, deliberately decoupled from any Lattice/protobuf type. The Raspberry Pi
camera is a *stationary* sensor: it reports a fixed geodetic location from
config. Its video is not carried here; that is the ``VideoIngress`` lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CameraState:
    """A single observation of the camera: its geodetic position."""

    latitude_degrees: float
    longitude_degrees: float
    altitude_hae_meters: float


class CameraSource:
    """A stationary Raspberry Pi camera at a fixed, configured location."""

    def __init__(
        self,
        latitude_degrees: float,
        longitude_degrees: float,
        altitude_hae_meters: float,
    ) -> None:
        self._latitude = latitude_degrees
        self._longitude = longitude_degrees
        self._altitude_hae = altitude_hae_meters

    def next_state(self) -> CameraState:
        return CameraState(
            latitude_degrees=self._latitude,
            longitude_degrees=self._longitude,
            altitude_hae_meters=self._altitude_hae,
        )
