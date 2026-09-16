"""Tests for the drone simulator invariants."""

import math

from rpi_cam_lattice_service.sources import DroneSimulator, State


def test_next_state_returns_state():
    sim = DroneSimulator()
    state = sim.next_state()
    assert isinstance(state, State)


def test_speed_matches_velocity_magnitude():
    sim = DroneSimulator()
    for _ in range(5):
        s = sim.next_state()
        expected = math.sqrt(
            s.velocity_e_mps**2 + s.velocity_n_mps**2 + s.velocity_u_mps**2
        )
        assert math.isclose(s.speed_mps, expected, rel_tol=1e-9)


def test_position_stays_near_center():
    sim = DroneSimulator()
    center_lat, center_lon, radius = 49.65108, 11.79045, 0.5
    for _ in range(50):
        s = sim.next_state()
        assert abs(s.latitude_degrees - center_lat) <= radius + 1e-6
        assert abs(s.longitude_degrees - center_lon) <= radius + 1e-6


def test_first_sample_has_zero_acceleration():
    sim = DroneSimulator()
    s = sim.next_state()
    assert s.acceleration_e_mps2 == 0.0
    assert s.acceleration_n_mps2 == 0.0
    assert s.acceleration_u_mps2 == 0.0


def test_altitude_within_expected_band():
    sim = DroneSimulator()
    # base HAE = 1000 MSL + 48 geoid = 1048, +/- 50 m sinusoidal variation.
    for _ in range(50):
        s = sim.next_state()
        assert 1048.0 - 50.0 - 1e-6 <= s.altitude_hae_meters <= 1048.0 + 50.0 + 1e-6
