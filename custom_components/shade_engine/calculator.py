"""Pure sun-geometry math for Shade Engine.

No Home Assistant imports — this module is importable and testable anywhere.

Model: a vertical shade drops from the top of the glass. The uncovered
opening at the bottom (height ``h``) admits direct sun that penetrates the
room a horizontal distance ``x = h / tan(profile_angle)``, where the profile
angle is the sun's elevation projected onto the window's normal plane.
Solving for the largest opening that keeps penetration at or under the
protected depth ``d``:

    h = d * tan(profile_angle)
    position% = clamp(h / glass_height, 0, 1) * 100

Position 100 is fully open, 0 fully closed (Home Assistant convention).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowGeometry:
    """Static geometry for one window (or a bank of identical windows)."""

    azimuth: float
    fov_left: float = 90.0
    fov_right: float = 90.0
    height: float = 1.0
    protect_depth: float = 1.0
    min_elevation: float = 0.0


@dataclass(frozen=True)
class GlareResult:
    """Output of the glare calculation."""

    position: int
    sun_in_window: bool
    gamma: float
    profile_angle: float


def relative_azimuth(sun_azimuth: float, window_azimuth: float) -> float:
    """Signed sun-to-window azimuth difference, wrapped to [-180, 180)."""
    return ((sun_azimuth - window_azimuth + 180.0) % 360.0) - 180.0


def glare(sun_azimuth: float, sun_elevation: float, geo: WindowGeometry) -> GlareResult:
    """Highest shade position that keeps direct sun off the protected depth.

    Returns position 100 (fully open) whenever direct sun cannot reach the
    window: below the elevation floor, outside the field of view, or grazing
    the glass at nearly 90 degrees.
    """
    gamma = relative_azimuth(sun_azimuth, geo.azimuth)
    in_fov = -geo.fov_left <= gamma <= geo.fov_right

    if sun_elevation <= geo.min_elevation or not in_fov:
        return GlareResult(100, False, gamma, 0.0)

    cos_gamma = math.cos(math.radians(gamma))
    if cos_gamma <= 1e-6:
        return GlareResult(100, False, gamma, 90.0)

    profile = math.degrees(
        math.atan2(math.tan(math.radians(sun_elevation)), cos_gamma)
    )
    open_height = geo.protect_depth * math.tan(math.radians(profile))
    fraction = max(0.0, min(1.0, open_height / geo.height))
    return GlareResult(round(fraction * 100), True, gamma, profile)
