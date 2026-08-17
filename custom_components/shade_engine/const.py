"""Constants for Shade Engine."""

DOMAIN = "shade_engine"

CONF_ZONES = "zones"
CONF_NAME = "name"
CONF_COVERS = "covers"
CONF_WINDOW = "window"
CONF_AZIMUTH = "azimuth"
CONF_FOV_LEFT = "fov_left"
CONF_FOV_RIGHT = "fov_right"
CONF_HEIGHT = "height"
CONF_PROTECT_DEPTH = "protect_depth"
CONF_MIN_ELEVATION = "min_elevation"
CONF_SILL_HEIGHT = "sill_height"
CONF_EYE_ZONE = "eye_zone"
CONF_DEPTH = "depth"
CONF_REFLECTORS = "reflectors"
CONF_FROM = "from"
CONF_TO = "to"
CONF_MODES = "modes"
CONF_DEFAULT_MODE = "default_mode"
CONF_MOTION = "motion"
CONF_DEADBAND = "deadband"
CONF_MIN_INTERVAL = "min_interval"
CONF_HOLD_DURATION = "hold_duration"
CONF_SETTLE = "settle"
CONF_MIN = "min"
CONF_MAX = "max"

ATTR_ZONE = "zone"
ATTR_DURATION = "duration"

SERVICE_HOLD = "hold"
SERVICE_RELEASE = "release"
SERVICE_RECONCILE = "reconcile"

GLARE = "glare"

STARTUP_DELAY = 60


def signal_zone_update(zone_id: str) -> str:
    """Dispatcher signal fired whenever a zone's derived state changes."""
    return f"{DOMAIN}_update_{zone_id}"
