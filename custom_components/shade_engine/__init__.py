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
import math
from pathlib import Path

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    EVENT_HOMEASSISTANT_STARTED,
    Platform,
)
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util
from datetime import timedelta

from .calculator import EyeZone, GlareResult, Reflector, WindowGeometry, glare
from .const import (
    ATTR_DURATION,
    ATTR_ZONE,
    CONF_AZIMUTH,
    CONF_COVERS,
    CONF_DEADBAND,
    CONF_DEFAULT_MODE,
    CONF_DEPTH,
    CONF_EYE_ZONE,
    CONF_FOV_LEFT,
    CONF_FOV_RIGHT,
    CONF_FROM,
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
    CONF_REFLECTORS,
    CONF_SETTLE,
    CONF_SILL_HEIGHT,
    CONF_TO,
    CONF_WINDOW,
    CONF_ZONES,
    DOMAIN,
    GLARE,
    SERVICE_HOLD,
    SERVICE_RECONCILE,
    SERVICE_RELEASE,
    SERVICE_RELOAD,
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

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.SWITCH,
]

SUN_ENTITY = "sun.sun"
TICK_INTERVAL = timedelta(seconds=60)

CARD_FILENAME = "shade-engine-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"


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


def _span(value):
    """Validate a [low, high] pair of non-negative meters."""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise vol.Invalid("expected a two-element list: [low, high]")
    low, high = (vol.Coerce(float)(v) for v in value)
    if low < 0:
        raise vol.Invalid("values must be non-negative")
    if low >= high:
        raise vol.Invalid("first value must be less than the second")
    return (low, high)


EYE_ZONE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HEIGHT): _span,
        vol.Required(CONF_DEPTH): _span,
    }
)

REFLECTOR_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HEIGHT, default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0.0)
        ),
        vol.Optional(CONF_FROM, default=0.0): vol.All(
            vol.Coerce(float), vol.Range(min=0.0)
        ),
        vol.Optional(CONF_TO): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
    }
)


def _validate_window(win: dict) -> dict:
    """Cross-field rules the per-key schemas can't express."""
    if CONF_PROTECT_DEPTH not in win and CONF_EYE_ZONE not in win:
        raise vol.Invalid("window needs protect_depth or eye_zone")
    if win[CONF_REFLECTORS] and CONF_EYE_ZONE not in win:
        raise vol.Invalid("reflectors require an eye_zone to protect")
    for ref in win[CONF_REFLECTORS]:
        if CONF_TO in ref and ref[CONF_TO] <= ref[CONF_FROM]:
            raise vol.Invalid("reflector 'to' must be greater than 'from'")
        if ref[CONF_HEIGHT] >= win[CONF_EYE_ZONE][CONF_HEIGHT][0]:
            raise vol.Invalid("reflector must sit below the eye_zone")
    return win


WINDOW_SCHEMA = vol.All(
    vol.Schema(
        {
            vol.Required(CONF_AZIMUTH): vol.All(vol.Coerce(float), vol.Range(0, 360)),
            vol.Optional(CONF_FOV_LEFT, default=90.0): vol.All(
                vol.Coerce(float), vol.Range(0, 180)
            ),
            vol.Optional(CONF_FOV_RIGHT, default=90.0): vol.All(
                vol.Coerce(float), vol.Range(0, 180)
            ),
            vol.Required(CONF_HEIGHT): vol.All(vol.Coerce(float), vol.Range(min=0.05)),
            vol.Optional(CONF_PROTECT_DEPTH): vol.All(
                vol.Coerce(float), vol.Range(min=0.0)
            ),
            vol.Optional(CONF_MIN_ELEVATION, default=0.0): vol.Coerce(float),
            vol.Optional(CONF_SILL_HEIGHT, default=0.0): vol.All(
                vol.Coerce(float), vol.Range(min=0.0)
            ),
            vol.Optional(CONF_EYE_ZONE): EYE_ZONE_SCHEMA,
            vol.Optional(CONF_REFLECTORS, default=[]): vol.All(
                cv.ensure_list, [REFLECTOR_SCHEMA]
            ),
        }
    ),
    _validate_window,
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
        eye = win.get(CONF_EYE_ZONE)
        self.geometry = WindowGeometry(
            azimuth=win[CONF_AZIMUTH],
            fov_left=win[CONF_FOV_LEFT],
            fov_right=win[CONF_FOV_RIGHT],
            height=win[CONF_HEIGHT],
            protect_depth=win.get(CONF_PROTECT_DEPTH, 0.0),
            min_elevation=win[CONF_MIN_ELEVATION],
            sill_height=win[CONF_SILL_HEIGHT],
            eye_zone=(
                EyeZone(
                    low=eye[CONF_HEIGHT][0],
                    high=eye[CONF_HEIGHT][1],
                    near=eye[CONF_DEPTH][0],
                    far=eye[CONF_DEPTH][1],
                )
                if eye
                else None
            ),
            reflectors=tuple(
                Reflector(
                    height=ref[CONF_HEIGHT],
                    start=ref[CONF_FROM],
                    end=ref.get(CONF_TO, math.inf),
                )
                for ref in win[CONF_REFLECTORS]
            ),
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
        self._unsubs: list = []

    # -- lifecycle ----------------------------------------------------------

    @callback
    def async_start(self, delay: float = STARTUP_DELAY) -> None:
        """Subscribe to everything and schedule the first reconcile."""
        self._unsubs.append(
            async_track_state_change_event(self.hass, [SUN_ENTITY], self._sun_changed)
        )
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, list(self._cover_to_zone), self._cover_changed
            )
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._tick, TICK_INTERVAL)
        )
        self._recompute_glare()

        async def _initial(_now) -> None:
            _LOGGER.info("Initial reconcile of %d zones", len(self.zones))
            await self._evaluate_all(forced=False)

        self._unsubs.append(async_call_later(self.hass, delay, _initial))

    @callback
    def async_stop(self) -> None:
        """Unsubscribe from everything; the engine issues no further commands."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for timer in self._hold_timers.values():
            timer()
        self._hold_timers.clear()

    @callback
    def adopt_runtime(self, previous: ShadeEngine) -> None:
        """Carry per-zone runtime state over from the engine being replaced.

        Geometry, modes and motion tuning come from the new YAML; what a
        human or automation did at runtime (chosen mode, control on/off,
        an active hold, the last commanded positions) survives the reload
        for every zone that still exists. A mode the new config no longer
        defines falls back to the zone's default.
        """
        for zone_id, zone in self.zones.items():
            old = previous.zones.get(zone_id)
            if old is None:
                continue
            if old.core.mode in zone.core.modes:
                zone.core.mode = old.core.mode
            zone.core.enabled = old.core.enabled
            zone.core.last_commanded = dict(old.core.last_commanded)
            zone.core.last_command_ts = old.core.last_command_ts
            zone.core.hold_until = old.core.hold_until
            zone.last_decision = old.last_decision
            if zone.core.hold_active(self._now()):
                self._schedule_hold_expiry(zone)

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

    async def async_set_enabled(self, zone_id: str, enabled: bool) -> None:
        """Turn the engine on or off for one zone.

        Re-enabling reconciles immediately (bypassing the rate limit, but
        never a hold) so the zone converges without waiting for a tick.
        """
        zone = self.zones[zone_id]
        if zone.core.enabled == enabled:
            return
        zone.core.enabled = enabled
        _LOGGER.info("[%s] control %s", zone_id, "enabled" if enabled else "disabled")
        async_dispatcher_send(self.hass, signal_zone_update(zone_id))
        await self._evaluate(zone, forced=enabled)

    @callback
    def restore_enabled(self, zone_id: str, enabled: bool) -> None:
        """Adopt a restored on/off state at startup without commanding."""
        self.zones[zone_id].core.enabled = enabled

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


def _build_engine(hass: HomeAssistant, conf: dict) -> ShadeEngine:
    """Construct zones and an engine from validated YAML config."""
    zones = {
        zone_id: Zone(zone_id, zone_conf)
        for zone_id, zone_conf in conf[CONF_ZONES].items()
    }
    return ShadeEngine(hass, zones)


async def _async_reload(hass: HomeAssistant) -> None:
    """Re-read the YAML and swap in a new engine without restarting HA.

    Invalid or missing YAML leaves the running engine untouched and raises,
    so a typo can't silently take the shades offline. Runtime state (mode,
    control on/off, holds) carries over per zone; the config entry is
    reloaded so entities reflect any new zones or mode lists.
    """
    config = await async_integration_yaml_config(hass, DOMAIN)
    if config is None or DOMAIN not in config:
        raise HomeAssistantError(
            f"{DOMAIN}: reload aborted, configuration is missing or invalid "
            "(see log); the running configuration is unchanged"
        )
    try:
        engine = _build_engine(hass, config[DOMAIN])
    except vol.Invalid as err:
        raise HomeAssistantError(
            f"{DOMAIN}: reload aborted, {err}; the running configuration is "
            "unchanged"
        ) from err

    previous: ShadeEngine | None = hass.data.get(DOMAIN)
    if previous is not None:
        previous.async_stop()
        engine.adopt_runtime(previous)
    hass.data[DOMAIN] = engine

    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_reload(entry.entry_id)

    # A reload is a deliberate act: reconcile promptly instead of waiting
    # out the boot-time settling delay (unless HA is itself still booting).
    engine.async_start(delay=1 if hass.state is CoreState.running else STARTUP_DELAY)
    _LOGGER.info("Reloaded configuration: %d zones", len(engine.zones))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Shade Engine from YAML configuration."""
    # Serve the bundled Lovelace card and register it as a frontend resource
    # so `custom:shade-engine-card` works with zero manual resource setup.
    # Registered even when the YAML is gone, so existing dashboards degrade
    # to the card's own "entity not found" message rather than a red box.
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                CARD_URL,
                str(Path(__file__).parent / "www" / CARD_FILENAME),
                cache_headers=True,
            )
        ]
    )
    integration = await async_get_integration(hass, DOMAIN)
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")

    conf = config.get(DOMAIN)
    if conf is None:
        # YAML was removed; drop the imported entry so entities don't linger.
        for entry in hass.config_entries.async_entries(DOMAIN):
            hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
        return True
    engine = _build_engine(hass, conf)
    hass.data[DOMAIN] = engine

    # Services look the engine up per call: a reload replaces hass.data[DOMAIN].
    def _zone_id(call: ServiceCall) -> str:
        zone_id = call.data[ATTR_ZONE]
        if zone_id not in hass.data[DOMAIN].zones:
            raise vol.Invalid(f"unknown zone: {zone_id}")
        return zone_id

    async def _service_hold(call: ServiceCall) -> None:
        await hass.data[DOMAIN].async_hold(
            _zone_id(call), call.data.get(ATTR_DURATION)
        )

    async def _service_release(call: ServiceCall) -> None:
        await hass.data[DOMAIN].async_release(_zone_id(call))

    async def _service_reconcile(call: ServiceCall) -> None:
        zone_id = call.data.get(ATTR_ZONE)
        if zone_id is not None and zone_id not in hass.data[DOMAIN].zones:
            raise vol.Invalid(f"unknown zone: {zone_id}")
        await hass.data[DOMAIN].async_reconcile(zone_id)

    async def _service_reload(call: ServiceCall) -> None:
        await _async_reload(hass)

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
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _service_reload)

    # A single config entry imported from YAML is what lets each zone appear
    # as a device on the integration page; the entry itself stores nothing.
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data={}
        )
    )

    # The wrapper must itself be a @callback: a bare lambda would be run in
    # the executor, and async_start touches loop-only APIs.
    @callback
    def _start(_event: Event) -> None:
        engine.async_start()

    if hass.state is CoreState.running:
        engine.async_start()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up entities from the YAML-imported config entry."""
    engine: ShadeEngine | None = hass.data.get(DOMAIN)
    if engine is None:
        # Entry exists but YAML is gone; async_setup is already removing it.
        return False

    # Drop devices for zones that were removed from the YAML.
    device_registry = dr.async_get(hass)
    zone_ids = set(engine.zones)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if not any(
            domain == DOMAIN and zone_id in zone_ids
            for domain, zone_id in device.identifiers
        ):
            device_registry.async_update_device(
                device.id, remove_config_entry_id=entry.entry_id
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry's platforms."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
