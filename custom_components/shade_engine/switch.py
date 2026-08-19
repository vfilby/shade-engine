"""Per-zone control switch: is the engine allowed to move this zone at all."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, signal_zone_update
from .entity import zone_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    engine = hass.data[DOMAIN]
    async_add_entities(
        ShadeControlSwitch(engine, zone) for zone in engine.zones.values()
    )


class ShadeControlSwitch(SwitchEntity, RestoreEntity):
    """Off means the engine never commands this zone's covers.

    Unlike a hold this has no expiry; it survives restarts. Turning it back
    on reconciles immediately (bypassing the rate limit, never a hold).
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:robot"

    def __init__(self, engine, zone) -> None:
        self._engine = engine
        self._zone = zone
        self._attr_unique_id = f"{DOMAIN}_{zone.zone_id}_control"
        self._attr_name = "Shade control"
        self._attr_device_info = zone_device_info(zone)

    @property
    def is_on(self) -> bool:
        return self._zone.core.enabled

    async def async_turn_on(self, **kwargs) -> None:
        await self._engine.async_set_enabled(self._zone.zone_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._engine.async_set_enabled(self._zone.zone_id, False)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._engine.restore_enabled(
                self._zone.zone_id, last.state != STATE_OFF
            )
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
