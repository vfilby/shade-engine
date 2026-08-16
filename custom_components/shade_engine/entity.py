"""Shared entity helpers: one device per zone on the integration page."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


def zone_device_info(zone) -> DeviceInfo:
    """Device that groups all of a zone's entities in Devices & Services."""
    return DeviceInfo(
        identifiers={(DOMAIN, zone.zone_id)},
        name=zone.name,
        manufacturer="Shade Engine",
        model="Shade zone",
    )
