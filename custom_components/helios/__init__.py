"""Helios: a Lenovo Smart Clock 2 running the Helios app, connected over HA's own WebSocket API."""

from __future__ import annotations

import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from . import appearance as ap
from . import websocket
from .const import DOMAIN, PairingRegistry
from .coordinator import HeliosCoordinator
from .http import HeliosAppearanceView

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.LIGHT, Platform.NUMBER, Platform.MEDIA_PLAYER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    data = hass.data.setdefault(DOMAIN, {"pairing": PairingRegistry(), "entries": {}})
    if not data.get("ws_registered"):
        websocket.async_register(hass)
        data["ws_registered"] = True
    if not data.get("http_registered"):
        hass.http.register_view(HeliosAppearanceView())
        data["http_registered"] = True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {"pairing": PairingRegistry(), "entries": {}})
    repaired = await hass.async_add_executor_job(ap.repair_storage, hass.config.path("helios", entry.entry_id), dict(entry.options))
    if repaired != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=repaired)
    coordinator = HeliosCoordinator(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.installation_id)},
        manufacturer="Lenovo",
        model="Smart Clock 2",
        name=entry.title,
        sw_version=entry.data.get("app_version"),
    )
    hass.data[DOMAIN]["entries"][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Appearance saved: push the full snapshot over the live subscription; no reload, nothing else restarts."""
    coordinator: HeliosCoordinator | None = hass.data[DOMAIN]["entries"].get(entry.entry_id)
    if coordinator is not None:
        coordinator.send_appearance()


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.async_add_executor_job(shutil.rmtree, hass.config.path("helios", entry.entry_id), True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: HeliosCoordinator | None = hass.data[DOMAIN]["entries"].pop(entry.entry_id, None)
    if coordinator is not None:
        coordinator.send_removed()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
