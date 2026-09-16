"""Raspberry Pi camera source.

The camera is a *stationary* sensor: it reports a fixed geodetic location (from
config) with zero motion. It reuses the shared ``State`` snapshot so it plugs
into the same publishing loop as the drone simulator — this is the seam that
replaces the simulator for the real Raspberry Pi deployment.

The camera's video is not carried in ``State``; it is registered separately with
Lattice's VideoManager (see ``video.py``) and the resulting video id is attached
to the published entity.
"""

from __future__ import annotations

from .base import Source, State


class CameraSource(Source):
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

    def next_state(self) -> State:
        return State(
            latitude_degrees=self._latitude,
            longitude_degrees=self._longitude,
            altitude_hae_meters=self._altitude_hae,
            altitude_agl_meters=0.0,
            altitude_asf_meters=0.0,
            # Stationary: no motion.
            velocity_e_mps=0.0,
            velocity_n_mps=0.0,
            velocity_u_mps=0.0,
            speed_mps=0.0,
            acceleration_e_mps2=0.0,
            acceleration_n_mps2=0.0,
            acceleration_u_mps2=0.0,
        )
