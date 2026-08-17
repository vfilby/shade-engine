"""Shade Engine sensors: the calculator's output and the actuator's intent."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, signal_zone_update
from .entity import zone_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    engine = hass.data[DOMAIN]
    entities: list[SensorEntity] = []
    for zone in engine.zones.values():
        entities.append(GlarePositionSensor(zone))
        entities.append(TargetSensor(zone))
    async_add_entities(entities)


class _ZoneSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, zone) -> None:
        self._zone = zone
        self._attr_device_info = zone_device_info(zone)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_zone_update(self._zone.zone_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


class GlarePositionSensor(_ZoneSensor):
    """Layer 1 output: highest position that still blocks direct sun."""

    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:sun-angle"

    def __init__(self, zone) -> None:
        super().__init__(zone)
        self._attr_unique_id = f"{DOMAIN}_{zone.zone_id}_glare_position"
        self._attr_name = "Glare position"

    @property
    def native_value(self) -> int:
        return self._zone.glare.position

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "sun_in_window": self._zone.glare.sun_in_window,
            "gamma": round(self._zone.glare.gamma, 1),
            "profile_angle": round(self._zone.glare.profile_angle, 1),
            "constraint": self._zone.glare.constraint,
        }


class TargetSensor(_ZoneSensor):
    """Layer 3 intent: what the actuator wants, and why it isn't moving."""

    _attr_native_unit_of_measurement = "%"
    _attr_icon = "mdi:crosshairs-gps"

    def __init__(self, zone) -> None:
        super().__init__(zone)
        self._attr_unique_id = f"{DOMAIN}_{zone.zone_id}_target"
        self._attr_name = "Shade target"

    @property
    def native_value(self) -> int:
        return self._zone.core.target(self._zone.glare.position)

    @property
    def extra_state_attributes(self) -> dict:
        core = self._zone.core
        decision = self._zone.last_decision
        return {
            "mode": core.mode,
            "last_decision": decision.reason if decision else None,
            "hold_until": (
                dt_util.utc_from_timestamp(core.hold_until).isoformat()
                if core.hold_until
                else None
            ),
            "last_command": (
                dt_util.utc_from_timestamp(core.last_command_ts).isoformat()
                if core.last_command_ts
                else None
            ),
        }
