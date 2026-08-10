"""Shade Engine binary sensors: sun-in-window and the manual hold."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.util import dt as dt_util

from .const import DOMAIN, signal_zone_update


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    engine = hass.data[DOMAIN]
    entities: list[BinarySensorEntity] = []
    for zone in engine.zones.values():
        entities.append(SunInWindowSensor(zone))
        entities.append(HoldActiveSensor(engine, zone))
    async_add_entities(entities)


class _ZoneBinarySensor(BinarySensorEntity):
    _attr_should_poll = False

    def __init__(self, zone) -> None:
        self._zone = zone

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


class SunInWindowSensor(_ZoneBinarySensor):
    """Is direct sun geometrically possible through this window right now."""

    _attr_icon = "mdi:sun-compass"

    def __init__(self, zone) -> None:
        super().__init__(zone)
        self._attr_unique_id = f"{DOMAIN}_{zone.zone_id}_sun_in_window"
        self._attr_name = f"{zone.name} sun in window"

    @property
    def is_on(self) -> bool:
        return self._zone.glare.sun_in_window


class HoldActiveSensor(_ZoneBinarySensor):
    """A human moved a cover; the engine is standing down for this zone."""

    _attr_icon = "mdi:hand-back-right"

    def __init__(self, engine, zone) -> None:
        super().__init__(zone)
        self._engine = engine
        self._attr_unique_id = f"{DOMAIN}_{zone.zone_id}_hold"
        self._attr_name = f"{zone.name} shade hold"

    @property
    def is_on(self) -> bool:
        return self._zone.core.hold_active(dt_util.utcnow().timestamp())

    @property
    def extra_state_attributes(self) -> dict:
        hold_until = self._zone.core.hold_until
        return {
            "hold_until": (
                dt_util.utc_from_timestamp(hold_until).isoformat()
                if hold_until
                else None
            )
        }
