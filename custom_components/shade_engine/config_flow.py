"""Config flow for Shade Engine.

Shade Engine is configured exclusively via YAML; this flow exists only so a
config entry can be created from that YAML (async_step_import). The entry is
what lets Home Assistant show the integration in Settings > Devices &
Services and group each zone's entities under a device. Adding the
integration from the UI is deliberately not supported.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class ShadeEngineConfigFlow(ConfigFlow, domain=DOMAIN):
    """Import-only flow: one entry, created from YAML."""

    VERSION = 1

    async def async_step_import(self, import_data: dict) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Shade Engine", data={})

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_abort(reason="yaml_only")
