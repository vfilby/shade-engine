"""Shade mode select entity: the entire policy interface, one per zone."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
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
        ShadeModeSelect(engine, zone) for zone in engine.zones.values()
    )


class ShadeModeSelect(SelectEntity, RestoreEntity):
    """Current mode for one zone. Automations write this; nothing else."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:blinds-horizontal"

    def __init__(self, engine, zone) -> None:
        self._engine = engine
        self._zone = zone
        self._attr_unique_id = f"{DOMAIN}_{zone.zone_id}_mode"
        self._attr_name = "Shade mode"
        self._attr_options = list(zone.core.modes)
        self._attr_device_info = zone_device_info(zone)

    @property
    def current_option(self) -> str:
        return self._zone.core.mode

    async def async_select_option(self, option: str) -> None:
        await self._engine.async_set_mode(self._zone.zone_id, option)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._engine.restore_mode(self._zone.zone_id, last.state)
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
