"""Pure sun-geometry math for Shade Engine.

No Home Assistant imports — this module is importable and testable anywhere.

Model: a vertical shade drops from the top of the glass. The uncovered
opening at the bottom (height ``h`` above the sill) admits direct sun. All
geometry is worked in the vertical plane along the sun's azimuth, using the
profile angle (the sun's elevation projected onto the window's normal plane),
so a single angle ``t = tan(profile)`` drives everything.

Two protection models, chosen per window:

* ``protect_depth`` (legacy): keep direct sun off the floor past depth ``d``.
  The steepest admitted ray grazes the shade's bottom edge, so
  ``h = d * t``.

* ``eye_zone``: keep sun out of a rectangle of room — heights
  ``[low, high]`` above the floor across distances ``[near, far]`` from the
  window. Direct sun is excluded when the shade-edge ray is already below
  the zone on arrival::

      sill + h - near * t <= low

  Reflections off horizontal surfaces (floor, counters) are handled by
  unfolding the mirror: a ray bouncing off a plane at height ``r`` into the
  zone is a straight ray into the zone's mirror image at heights
  ``2r - [high, low]``. Each reflector contributes one more linear
  constraint, clipped to the strip of the reflector that can actually bounce
  into the zone and to the patch of it the sun actually lights (which gives
  the "escape" case for free: when the sunlit patch lands beyond the hazard
  strip, the bounce rises past the zone and the constraint vanishes).

The published position is the highest one satisfying every constraint:

    position% = clamp(min(h_direct, h_reflector...) / glass_height, 0, 1) * 100

Position 100 is fully open, 0 fully closed (Home Assistant convention).
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class EyeZone:
    """Region to keep sun out of: heights [low, high] over depths [near, far]."""

    low: float
    high: float
    near: float
    far: float = math.inf


@dataclass(frozen=True)
class Reflector:
    """A horizontal reflective surface spanning [start, end] from the window."""

    height: float = 0.0
    start: float = 0.0
    end: float = math.inf


@dataclass(frozen=True)
class WindowGeometry:
    """Static geometry for one window (or a bank of identical windows)."""

    azimuth: float
    fov_left: float = 90.0
    fov_right: float = 90.0
    height: float = 1.0
    protect_depth: float = 1.0
    min_elevation: float = 0.0
    sill_height: float = 0.0
    eye_zone: EyeZone | None = None
    reflectors: tuple[Reflector, ...] = ()


@dataclass(frozen=True)
class GlareResult:
    """Output of the glare calculation."""

    position: int
    sun_in_window: bool
    gamma: float
    profile_angle: float
    constraint: str = "none"


def relative_azimuth(sun_azimuth: float, window_azimuth: float) -> float:
    """Signed sun-to-window azimuth difference, wrapped to [-180, 180)."""
    return ((sun_azimuth - window_azimuth + 180.0) % 360.0) - 180.0


def _open_direct(t: float, geo: WindowGeometry) -> float:
    """Max opening keeping direct sun out of the eye zone (inf if unconstrained)."""
    ez = geo.eye_zone
    if ez is None:
        return geo.protect_depth * t
    # The beam's lowest ray grazes the sill; if it still clears the zone's top
    # at the far edge, every admitted ray passes over the zone entirely.
    if geo.sill_height - ez.far * t >= ez.high:
        return math.inf
    return (ez.low - geo.sill_height) + ez.near * t


def _open_reflected(t: float, geo: WindowGeometry, ref: Reflector) -> float:
    """Max opening keeping bounced sun out of the eye zone (inf if unconstrained)."""
    ez = geo.eye_zone
    r = ref.height
    if r >= ez.low:
        # A reflector at or above the zone can't bounce up into it in this model.
        return math.inf
    # Bounce points on the plane whose reflected ray crosses the zone.
    strip_lo = max(ez.near - (ez.high - r) / t, ref.start, 0.0)
    strip_hi = min(ez.far - (ez.low - r) / t, ref.end)
    if strip_hi <= strip_lo:
        return math.inf
    # Nearest sunlit point on the plane is fixed by the sill, not the shade:
    # if even that lands beyond the hazard strip, no opening can light it.
    patch_start = max(geo.sill_height - r, 0.0) / t
    if patch_start >= strip_hi:
        return math.inf
    # Otherwise the shade-edge ray must land at or before the strip:
    # (sill + h - r) / t <= strip_lo.
    return (r - geo.sill_height) + strip_lo * t


def glare(sun_azimuth: float, sun_elevation: float, geo: WindowGeometry) -> GlareResult:
    """Highest shade position that keeps sun out of the protected region.

    Returns position 100 (fully open) whenever direct sun cannot reach the
    window: below the elevation floor, outside the field of view, or grazing
    the glass at nearly 90 degrees. The ``constraint`` field names what bound
    the result: ``direct``, ``reflected``, or ``none``.
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
    t = math.tan(math.radians(profile))

    if t <= 1e-9:
        open_height, constraint = 0.0, "direct"
    else:
        open_height, constraint = _open_direct(t, geo), "direct"
        if geo.eye_zone is not None:
            for ref in geo.reflectors:
                bounce = _open_reflected(t, geo, ref)
                if bounce < open_height:
                    open_height, constraint = bounce, "reflected"

    fraction = max(0.0, min(1.0, open_height / geo.height))
    if fraction >= 1.0:
        constraint = "none"
    return GlareResult(round(fraction * 100), True, gamma, profile, constraint)
