"""Tests for the pure glare calculator."""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "shade_engine"))

from calculator import (  # noqa: E402
    EyeZone,
    Reflector,
    WindowGeometry,
    glare,
    relative_azimuth,
)

WEST = WindowGeometry(azimuth=268, height=0.74, protect_depth=1.7)

# A tall patio-door style window: glass from floor to 2 m, eyes protected
# between 0.8 and 1.4 m high, 2-4 m into the room, shiny floor.
PATIO = WindowGeometry(
    azimuth=268,
    height=2.0,
    eye_zone=EyeZone(low=0.8, high=1.4, near=2.0, far=4.0),
    reflectors=(Reflector(height=0.0),),
)


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


# -- eye zone + reflections --------------------------------------------------


def test_eye_zone_floor_matches_protect_depth():
    # eye_zone with low=0, near=protect_depth, far=inf is the legacy model.
    eye = WindowGeometry(
        azimuth=268,
        height=0.74,
        eye_zone=EyeZone(low=0.0, high=1.4, near=1.7),
    )
    for elevation in range(1, 80, 3):
        assert glare(268, elevation, eye).position == glare(268, elevation, WEST).position


def test_high_sun_reflection_closes():
    # At 45 degrees direct sun stops well short of the eye zone, but the
    # floor bounce climbs back into it: mirror formula near*t - high = 0.6 m.
    result = glare(268, 45, PATIO)
    assert result.position == 30
    assert result.constraint == "reflected"

    no_reflector = WindowGeometry(
        azimuth=268,
        height=2.0,
        eye_zone=EyeZone(low=0.8, high=1.4, near=2.0, far=4.0),
    )
    assert glare(268, 45, no_reflector).position == 100


def test_daily_pattern_is_non_monotonic():
    # Glare when high (reflection), better mid-descent (bounce falls short of
    # the zone), tightening again as the sun drops toward eye level.
    high = glare(268, 45, PATIO)
    mid = glare(268, 10, PATIO)
    assert high.position == 30 and high.constraint == "reflected"
    assert mid.position == 58 and mid.constraint == "direct"
    assert mid.position > high.position


def test_counter_reflector_extent():
    counter_zone = EyeZone(low=1.0, high=1.6, near=2.0, far=4.0)
    under_window = WindowGeometry(
        azimuth=268,
        height=2.0,
        eye_zone=counter_zone,
        reflectors=(Reflector(height=0.9, start=0.0, end=0.6),),
    )
    elevation = math.degrees(math.atan(0.3))
    result = glare(268, elevation, under_window)
    assert result.position == 45
    assert result.constraint == "reflected"

    # Same counter moved deep into the room: the lit patch that matters is
    # farther out, the bounce constraint loosens past direct, direct binds.
    deep = WindowGeometry(
        azimuth=268,
        height=2.0,
        eye_zone=counter_zone,
        reflectors=(Reflector(height=0.9, start=3.0, end=3.5),),
    )
    result = glare(268, elevation, deep)
    assert result.position == 80
    assert result.constraint == "direct"


def test_sill_escape_branch():
    # Counter-height sill: at 20 degrees the nearest sunlit floor point is
    # already beyond the hazard strip, so the reflection constraint vanishes;
    # at 30 degrees the strip is lit and even a crack of opening bounces in.
    kitchen = WindowGeometry(
        azimuth=268,
        height=0.74,
        sill_height=0.9,
        eye_zone=EyeZone(low=0.8, high=1.4, near=2.0, far=4.0),
        reflectors=(Reflector(height=0.0),),
    )
    escaped = glare(268, 20, kitchen)
    assert escaped.position == 85
    assert escaped.constraint == "direct"

    lit = glare(268, 30, kitchen)
    assert lit.position == 0
    assert lit.constraint == "reflected"


def test_reflector_above_eye_zone_is_ignored():
    zone = EyeZone(low=0.8, high=1.4, near=2.0, far=4.0)
    base = WindowGeometry(azimuth=268, height=2.0, eye_zone=zone)
    shelved = WindowGeometry(
        azimuth=268,
        height=2.0,
        eye_zone=zone,
        reflectors=(Reflector(height=1.0),),
    )
    for elevation in range(1, 80, 3):
        assert glare(268, elevation, shelved) == glare(268, elevation, base)


def test_constraint_attribute_when_unbound():
    assert glare(268, -5, WEST).constraint == "none"
    assert glare(268, 60, WEST).constraint == "none"  # clamps fully open
