"""A self-contained UAV flight simulator.

Simulates a drone flying a circular
pattern over Germany with sinusoidal altitude variation, producing the kind of
position/velocity/acceleration data a real GPS/IMU sensor would emit. In
production this ``Source`` would be replaced by a real sensor implementation.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .base import Source, State


class DroneSimulator(Source):
    """Simulates a drone flying in a circular pattern with altitude variation."""

    def __init__(self) -> None:
        self._counter = 0

        self._center_lat = 49.65108
        self._center_lon = 11.79045

        # Conversion factors from degrees to meters.
        self._meters_per_degree_lat = 111320.0
        self._meters_per_degree_lon = 111320.0 * math.cos(
            self._center_lat * math.pi / 180.0
        )

        # Altitude configuration.
        # MSL (Mean Sea Level): based on the EGM96 geoid model — what pilots use.
        # HAE (Height Above Ellipsoid): based on WGS84 — what GPS measures.
        # Relationship: HAE = MSL + N, where N is the geoid separation.
        # For this location in Germany, geoid separation is approximately +48 m.
        self._geoid_separation = 48.0
        self._base_altitude_msl = 1000.0
        self._base_altitude_hae = self._base_altitude_msl + self._geoid_separation

        # Terrain configuration for AGL calculation (simulated hilly terrain).
        base_ground_elevation_msl = 450.0
        self._base_ground_elevation_hae = (
            base_ground_elevation_msl + self._geoid_separation
        )
        self._terrain_variation = 100.0  # +/- 100 m (hills and valleys)

        self._radius_degrees = -0.5

        now = datetime.now(timezone.utc)
        self._prev_velocity_east = 0.0
        self._prev_velocity_north = 0.0
        self._prev_velocity_up = 0.0
        self._prev_timestamp = now

    def _ground_elevation_hae(self, lat: float, lon: float) -> float:
        """Simulate terrain elevation using sinusoidal hills and valleys.

        In production this would be a real terrain-database lookup.
        """
        lat_offset = (lat - self._center_lat) * self._meters_per_degree_lat
        lon_offset = (lon - self._center_lon) * self._meters_per_degree_lon

        terrain_factor = math.sin(lat_offset / 200.0) * math.cos(lon_offset / 150.0)
        terrain_factor += 0.5 * math.sin(lat_offset / 100.0 + lon_offset / 100.0)

        return self._base_ground_elevation_hae + (
            terrain_factor * self._terrain_variation
        )

    def next_state(self) -> State:
        """Advance the simulation by one step and return the current state."""
        self._counter += 1
        current_time = datetime.now(timezone.utc)

        # Position angle for circular motion (counter interpreted as degrees).
        t = float(self._counter) * math.pi / 180.0

        # Sinusoidal altitude variation (+/- 50 m) and its rate of change.
        altitude_variation = 50.0 * math.sin(t * 0.5)
        current_altitude_hae = self._base_altitude_hae + altitude_variation
        altitude_change_rate = 50.0 * 0.5 * math.cos(t * 0.5)  # d/dt[A*sin(wt)]

        # Velocity components for circular motion (deg/s), perpendicular to radius.
        angular_velocity = 0.5
        velocity_east_deg_s = self._radius_degrees * math.cos(t) * angular_velocity
        velocity_north_deg_s = -self._radius_degrees * math.sin(t) * angular_velocity

        velocity_east_mps = velocity_east_deg_s * self._meters_per_degree_lon
        velocity_north_mps = velocity_north_deg_s * self._meters_per_degree_lat
        velocity_up_mps = altitude_change_rate

        speed_mps = math.sqrt(
            velocity_east_mps**2 + velocity_north_mps**2 + velocity_up_mps**2
        )

        # Acceleration via finite difference (skipped on the first sample).
        accel_east = accel_north = accel_up = 0.0
        if self._counter > 1:
            dt = (current_time - self._prev_timestamp).total_seconds()
            if dt > 0:
                accel_east = (velocity_east_mps - self._prev_velocity_east) / dt
                accel_north = (velocity_north_mps - self._prev_velocity_north) / dt
                accel_up = (velocity_up_mps - self._prev_velocity_up) / dt

        self._prev_velocity_east = velocity_east_mps
        self._prev_velocity_north = velocity_north_mps
        self._prev_velocity_up = velocity_up_mps
        self._prev_timestamp = current_time

        # Position on the circle.
        lat = self._center_lat + (self._radius_degrees * math.cos(t))
        lon = self._center_lon + (self._radius_degrees * math.sin(t))

        ground_elevation_hae = self._ground_elevation_hae(lat, lon)
        altitude_agl = current_altitude_hae - ground_elevation_hae

        return State(
            latitude_degrees=lat,
            longitude_degrees=lon,
            altitude_hae_meters=current_altitude_hae,
            altitude_agl_meters=altitude_agl,
            altitude_asf_meters=1000.0,  # Above Sea Floor (surface/underwater ops)
            velocity_e_mps=velocity_east_mps,
            velocity_n_mps=velocity_north_mps,
            velocity_u_mps=velocity_up_mps,
            speed_mps=speed_mps,
            acceleration_e_mps2=accel_east,
            acceleration_n_mps2=accel_north,
            acceleration_u_mps2=accel_up,
        )
