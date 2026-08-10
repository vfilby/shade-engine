"""Tests for the pure glare calculator."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "shade_engine"))

from calculator import WindowGeometry, glare, relative_azimuth  # noqa: E402

WEST = WindowGeometry(azimuth=268, height=0.74, protect_depth=1.7)


def test_relative_azimuth_wraps():
    assert relative_azimuth(270, 268) == 2
    assert relative_azimuth(266, 268) == -2
    assert relative_azimuth(10, 350) == 20
    assert relative_azimuth(350, 10) == -20
    assert relative_azimuth(88, 268) == 180 or relative_azimuth(88, 268) == -180


def test_sun_below_horizon_is_open():
    result = glare(268, -5, WEST)
    assert result.position == 100
    assert not result.sun_in_window


def test_sun_below_min_elevation_is_open():
    geo = WindowGeometry(azimuth=358, fov_left=90, fov_right=45, height=1.9,
                         protect_depth=2.0, min_elevation=10)
    result = glare(358, 8, geo)
    assert result.position == 100
    assert not result.sun_in_window


def test_sun_outside_fov_is_open():
    geo = WindowGeometry(azimuth=358, fov_left=90, fov_right=45, height=1.9,
                         protect_depth=2.0)
    # Sun 60 degrees east of the window normal exceeds fov_right=45.
    result = glare(58, 20, geo)
    assert result.position == 100
    assert not result.sun_in_window


def test_head_on_low_sun_closes_hard():
    # Sun straight at the window, very low: shade must drop nearly closed.
    result = glare(268, 3, WEST)
    assert result.sun_in_window
    assert result.position < 15


def test_high_sun_stays_open():
    # Sun straight at the window but 60 degrees up: penetration is short.
    result = glare(268, 60, WEST)
    assert result.sun_in_window
    assert result.position == 100


def test_evening_sun_realistic():
    # ~19:25 on Aug 9: elevation ~13, azimuth ~285. Adaptive Cover said 41;
    # same order of magnitude is what we expect from the same formula family.
    result = glare(285, 13, WEST)
    assert result.sun_in_window
    assert 30 <= result.position <= 70


def test_grazing_angle_is_open():
    # Sun at nearly 90 degrees to the glass: no meaningful penetration.
    result = glare(268 + 89.9999, 20, WindowGeometry(azimuth=268, height=1.0,
                                                     protect_depth=1.0))
    assert result.position == 100


def test_position_monotonic_in_elevation():
    positions = [glare(268, el, WEST).position for el in range(1, 60)]
    assert positions == sorted(positions)
