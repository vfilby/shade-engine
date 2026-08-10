"""Shade Engine: sun-tracking shade control with externalized policy.

Three layers:
  1. Calculator (calculator.py) - pure geometry, published as sensors.
  2. Policy - the user's automations, which only ever set a zone's mode.
  3. Actuator (core.py + this module) - resolves mode to a target position
     and commands covers, with a movement deadband, rate limiting that
     defers rather than drops, and a visible hold when a human moves a cover.
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    Platform,
)
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util
from datetime import timedelta

from .calculator import GlareResult, WindowGeometry, glare
from .const import (
    ATTR_DURATION,
    ATTR_ZONE,
    CONF_AZIMUTH,
    CONF_COVERS,
    CONF_DEADBAND,
    CONF_DEFAULT_MODE,
    CONF_FOV_LEFT,
    CONF_FOV_RIGHT,
    CONF_HEIGHT,
    CONF_HOLD_DURATION,
    CONF_MAX,
    CONF_MIN,
    CONF_MIN_ELEVATION,
    CONF_MIN_INTERVAL,
    CONF_MODES,
    CONF_MOTION,
    CONF_NAME,
    CONF_PROTECT_DEPTH,
    CONF_SETTLE,
    CONF_WINDOW,
    CONF_ZONES,
    DOMAIN,
    GLARE,
    SERVICE_HOLD,
    SERVICE_RECONCILE,
    SERVICE_RELEASE,
    STARTUP_DELAY,
    signal_zone_update,
)
from .core import (
    REASON_COMMAND,
    Decision,
    ModeTarget,
    MotionConfig,
    ZoneCore,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]

SUN_ENTITY = "sun.sun"
TICK_INTERVAL = timedelta(seconds=60)


def _mode_target(value):
    """Validate one mode's target: an int, "glare", or a clamp mapping."""
    if isinstance(value, bool):
        raise vol.Invalid("mode target must be a position, 'glare', or a mapping")
    if isinstance(value, int):
        if not 0 <= value <= 100:
            raise vol.Invalid("fixed position must be 0-100")
        return ModeTarget(fixed=value)
    if value == GLARE:
        return ModeTarget(dynamic=True)
    if isinstance(value, dict):
        schema = vol.Schema(
            {
                vol.Optional(CONF_MIN, default=0): vol.All(int, vol.Range(0, 100)),
                vol.Optional(CONF_MAX, default=100): vol.All(int, vol.Range(0, 100)),
            }
        )
        clamp = schema(value)
        return ModeTarget(dynamic=True, min=clamp[CONF_MIN], max=clamp[CONF_MAX])
    raise vol.Invalid("mode target must be a position, 'glare', or a mapping")


WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AZIMUTH): vol.All(vol.Coerce(float), vol.Range(0, 360)),
        vol.Optional(CONF_FOV_LEFT, default=90.0): vol.All(
            vol.Coerce(float), vol.Range(0, 180)
        ),
        vol.Optional(CONF_FOV_RIGHT, default=90.0): vol.All(
            vol.Coerce(float), vol.Range(0, 180)
        ),
        vol.Required(CONF_HEIGHT): vol.All(vol.Coerce(float), vol.Range(min=0.05)),
        vol.Required(CONF_PROTECT_DEPTH): vol.All(
            vol.Coerce(float), vol.Range(min=0.0)
        ),
        vol.Optional(CONF_MIN_ELEVATION, default=0.0): vol.Coerce(float),
    }
)

MOTION_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_DEADBAND, default=3): cv.positive_int,
        vol.Optional(CONF_MIN_INTERVAL, default=300): cv.positive_int,
        vol.Optional(CONF_HOLD_DURATION, default=3600): cv.positive_int,
        vol.Optional(CONF_SETTLE, default=90): cv.positive_int,
    }
)

ZONE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Required(CONF_COVERS): vol.All(cv.ensure_list, [cv.entity_id]),
        vol.Required(CONF_WINDOW): WINDOW_SCHEMA,
        vol.Required(CONF_MODES): vol.All(
            {cv.slug: _mode_target}, vol.Length(min=1)
        ),
        vol.Optional(CONF_DEFAULT_MODE): cv.slug,
        vol.Optional(CONF_MOTION, default={}): MOTION_SCHEMA,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({vol.Required(CONF_ZONES): {cv.slug: ZONE_SCHEMA}})},
    extra=vol.ALLOW_EXTRA,
)


class Zone:
    """One configured zone: geometry, runtime core, and derived state."""

    def __init__(self, zone_id: str, conf: dict) -> None:
        self.zone_id = zone_id
        self.name: str = conf.get(CONF_NAME) or zone_id.replace("_", " ").title()
        win = conf[CONF_WINDOW]
        self.geometry = WindowGeometry(
            azimuth=win[CONF_AZIMUTH],
            fov_left=win[CONF_FOV_LEFT],
            fov_right=win[CONF_FOV_RIGHT],
            height=win[CONF_HEIGHT],
            protect_depth=win[CONF_PROTECT_DEPTH],
            min_elevation=win[CONF_MIN_ELEVATION],
        )
        modes: dict[str, ModeTarget] = conf[CONF_MODES]
        default_mode = conf.get(CONF_DEFAULT_MODE) or next(iter(modes))
        if default_mode not in modes:
            raise vol.Invalid(
                f"zone {zone_id}: default_mode '{default_mode}' is not a mode"
            )
        motion = conf[CONF_MOTION]
        self.core = ZoneCore(
            covers=list(conf[CONF_COVERS]),
            modes=modes,
            motion=MotionConfig(
                deadband=motion[CONF_DEADBAND],
                min_interval=motion[CONF_MIN_INTERVAL],
                hold_duration=motion[CONF_HOLD_DURATION],
                settle=motion[CONF_SETTLE],
            ),
            mode=default_mode,
        )
        self.glare: GlareResult = GlareResult(100, False, 0.0, 0.0)
        self.last_decision: Decision | None = None


class ShadeEngine:
    """Owns all zones, listens to the world, commands the covers."""

    def __init__(self, hass: HomeAssistant, zones: dict[str, Zone]) -> None:
        self.hass = hass
        self.zones = zones
        self._cover_to_zone: dict[str, Zone] = {}
        for zone in zones.values():
            for cover in zone.core.covers:
                self._cover_to_zone[cover] = zone
        self._hold_timers: dict[str, object] = {}

    # -- lifecycle ----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        """Subscribe to everything and schedule the first reconcile."""
        async_track_state_change_event(self.hass, [SUN_ENTITY], self._sun_changed)
        async_track_state_change_event(
            self.hass, list(self._cover_to_zone), self._cover_changed
        )
        async_track_time_interval(self.hass, self._tick, TICK_INTERVAL)
        self._recompute_glare()

        async def _initial(_now) -> None:
            _LOGGER.info("Initial reconcile of %d zones", len(self.zones))
            await self._evaluate_all(forced=False)

        async_call_later(self.hass, STARTUP_DELAY, _initial)

    # -- inputs -------------------------------------------------------------

    @callback
    def _recompute_glare(self) -> None:
        sun = self.hass.states.get(SUN_ENTITY)
        if sun is None:
            return
        azimuth = sun.attributes.get("azimuth")
        elevation = sun.attributes.get("elevation")
        if azimuth is None or elevation is None:
            return
        for zone in self.zones.values():
            result = glare(azimuth, elevation, zone.geometry)
            if result != zone.glare:
                zone.glare = result
                async_dispatcher_send(self.hass, signal_zone_update(zone.zone_id))

    async def _sun_changed(self, event: Event) -> None:
        self._recompute_glare()
        await self._evaluate_all(forced=False)

    async def _cover_changed(self, event: Event) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("unavailable", "unknown"):
            return
        position = new_state.attributes.get("current_position")
        if position is None:
            return
        cover = event.data["entity_id"]
        zone = self._cover_to_zone[cover]
        if zone.core.report_position(cover, round(position), self._now()):
            _LOGGER.info(
                "[%s] Manual move of %s to %s: holding for %.0f min",
                zone.zone_id,
                cover,
                round(position),
                zone.core.motion.hold_duration / 60,
            )
            self._schedule_hold_expiry(zone)
            async_dispatcher_send(self.hass, signal_zone_update(zone.zone_id))

    async def _tick(self, _now) -> None:
        await self._evaluate_all(forced=False)

    # -- actuation ----------------------------------------------------------

    def _now(self) -> float:
        return dt_util.utcnow().timestamp()

    def _current_positions(self, zone: Zone) -> dict[str, int | None]:
        positions: dict[str, int | None] = {}
        for cover in zone.core.covers:
            state = self.hass.states.get(cover)
            if state is None or state.state in ("unavailable", "unknown"):
                positions[cover] = None
                continue
            raw = state.attributes.get("current_position")
            positions[cover] = None if raw is None else round(raw)
        return positions

    async def _evaluate_all(self, forced: bool) -> None:
        for zone in self.zones.values():
            await self._evaluate(zone, forced)

    async def _evaluate(self, zone: Zone, forced: bool) -> None:
        decision = zone.core.evaluate(
            self._now(),
            zone.glare.position,
            self._current_positions(zone),
            forced=forced,
        )
        previous = zone.last_decision
        zone.last_decision = decision
        if decision.reason == REASON_COMMAND:
            _LOGGER.info(
                "[%s] mode=%s -> position %s for %s",
                zone.zone_id,
                zone.core.mode,
                decision.target,
                ", ".join(decision.covers),
            )
            await self.hass.services.async_call(
                "cover",
                "set_cover_position",
                {ATTR_ENTITY_ID: decision.covers, "position": decision.target},
                blocking=False,
            )
        else:
            _LOGGER.debug(
                "[%s] no command: %s (target %s)",
                zone.zone_id,
                decision.reason,
                decision.target,
            )
        if previous is None or previous.reason != decision.reason:
            async_dispatcher_send(self.hass, signal_zone_update(zone.zone_id))

    # -- mode + services ----------------------------------------------------

    async def async_set_mode(self, zone_id: str, mode: str) -> None:
        zone = self.zones[zone_id]
        if zone.core.mode == mode:
            return
        zone.core.set_mode(mode)
        _LOGGER.info("[%s] mode -> %s", zone_id, mode)
        async_dispatcher_send(self.hass, signal_zone_update(zone_id))
        await self._evaluate(zone, forced=True)

    @callback
    def restore_mode(self, zone_id: str, mode: str) -> None:
        """Adopt a restored mode at startup without commanding anything."""
        zone = self.zones[zone_id]
        if mode in zone.core.modes:
            zone.core.mode = mode

    def _schedule_hold_expiry(self, zone: Zone) -> None:
        if (timer := self._hold_timers.pop(zone.zone_id, None)) is not None:
            timer()
        delay = max(1.0, (zone.core.hold_until or 0) - self._now() + 1)

        async def _expired(_now) -> None:
            self._hold_timers.pop(zone.zone_id, None)
            _LOGGER.info("[%s] hold expired, reconciling", zone.zone_id)
            async_dispatcher_send(self.hass, signal_zone_update(zone.zone_id))
            await self._evaluate(zone, forced=True)

        self._hold_timers[zone.zone_id] = async_call_later(self.hass, delay, _expired)

    async def async_hold(self, zone_id: str, duration: float | None) -> None:
        zone = self.zones[zone_id]
        zone.core.start_hold(self._now(), duration)
        _LOGGER.info("[%s] hold started via service", zone_id)
        self._schedule_hold_expiry(zone)
        async_dispatcher_send(self.hass, signal_zone_update(zone_id))

    async def async_release(self, zone_id: str) -> None:
        zone = self.zones[zone_id]
        if (timer := self._hold_timers.pop(zone_id, None)) is not None:
            timer()
        zone.core.release_hold()
        _LOGGER.info("[%s] hold released via service", zone_id)
        async_dispatcher_send(self.hass, signal_zone_update(zone_id))
        await self._evaluate(zone, forced=True)

    async def async_reconcile(self, zone_id: str | None) -> None:
        if zone_id is None:
            await self._evaluate_all(forced=True)
        else:
            await self._evaluate(self.zones[zone_id], forced=True)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Shade Engine from YAML configuration."""
    conf = config[DOMAIN]
    zones = {
        zone_id: Zone(zone_id, zone_conf)
        for zone_id, zone_conf in conf[CONF_ZONES].items()
    }
    engine = ShadeEngine(hass, zones)
    hass.data[DOMAIN] = engine

    def _zone_id(call: ServiceCall) -> str:
        zone_id = call.data[ATTR_ZONE]
        if zone_id not in zones:
            raise vol.Invalid(f"unknown zone: {zone_id}")
        return zone_id

    async def _service_hold(call: ServiceCall) -> None:
        await engine.async_hold(_zone_id(call), call.data.get(ATTR_DURATION))

    async def _service_release(call: ServiceCall) -> None:
        await engine.async_release(_zone_id(call))

    async def _service_reconcile(call: ServiceCall) -> None:
        zone_id = call.data.get(ATTR_ZONE)
        if zone_id is not None and zone_id not in zones:
            raise vol.Invalid(f"unknown zone: {zone_id}")
        await engine.async_reconcile(zone_id)

    hass.services.async_register(
        DOMAIN,
        SERVICE_HOLD,
        _service_hold,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ZONE): cv.slug,
                vol.Optional(ATTR_DURATION): cv.positive_int,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RELEASE,
        _service_release,
        schema=vol.Schema({vol.Required(ATTR_ZONE): cv.slug}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECONCILE,
        _service_reconcile,
        schema=vol.Schema({vol.Optional(ATTR_ZONE): cv.slug}),
    )

    for platform in PLATFORMS:
        hass.async_create_task(
            discovery.async_load_platform(hass, platform, DOMAIN, {}, config)
        )

    if hass.state is CoreState.running:
        engine.async_start()
    else:
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, lambda _event: engine.async_start()
        )
    return True
