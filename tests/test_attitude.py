"""Tests for the attitude math ported from the Go integration."""

import math

from rpi_cam_lattice_service.worker import velocity_to_yaw_enu, yaw_to_quaternion_enu


def test_yaw_east_is_zero():
    assert velocity_to_yaw_enu(velocity_east=5.0, velocity_north=0.0) == 0.0


def test_yaw_north_is_ninety():
    assert velocity_to_yaw_enu(velocity_east=0.0, velocity_north=5.0) == 90.0


def test_yaw_west_is_one_eighty():
    assert abs(velocity_to_yaw_enu(-5.0, 0.0)) == 180.0


def test_quaternion_identity_at_zero_yaw():
    q = yaw_to_quaternion_enu(0.0)
    assert q.w == 1.0
    assert q.x == 0.0 and q.y == 0.0 and q.z == 0.0


def test_quaternion_ninety_degrees():
    # 90 deg rotation about Up: w = z = cos/sin(45 deg) = sqrt(2)/2.
    q = yaw_to_quaternion_enu(90.0)
    half = math.sqrt(2) / 2
    assert math.isclose(q.w, half, abs_tol=1e-9)
    assert math.isclose(q.z, half, abs_tol=1e-9)
    assert q.x == 0.0 and q.y == 0.0


def test_quaternion_is_unit_norm():
    for yaw in (0.0, 33.0, 90.0, 180.0, 270.0, 359.0):
        q = yaw_to_quaternion_enu(yaw)
        norm = math.sqrt(q.w**2 + q.x**2 + q.y**2 + q.z**2)
        assert math.isclose(norm, 1.0, abs_tol=1e-9)
