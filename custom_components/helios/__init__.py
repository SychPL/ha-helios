"""Helios: a Lenovo Smart Clock 2 running the Helios app, connected over HA's own WebSocket API."""

from __future__ import annotations

import shutil

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from . import appearance as ap
from . import identity, panel, websocket
from .const import DOMAIN, new_domain_data
from .coordinator import HeliosCoordinator
from .http import async_register_views

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.LIGHT, Platform.NUMBER, Platform.MEDIA_PLAYER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    data = hass.data.setdefault(DOMAIN, new_domain_data())
    if not data.get("ws_registered"):
        websocket.async_register(hass)
        data["ws_registered"] = True
    async_register_views(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, new_domain_data())
    user_id = entry.data.get("user_id")
    if user_id in data["cancelled_users"] or await hass.auth.async_get_user(user_id) is None:
        # the pairing that created this entry was rolled back after its flow had already finished: either the marker is
        # still there, or (a finish that took longer than the marker's TTL) the identity is already gone - an entry whose
        # user does not exist can never authenticate, so it removes itself either way
        data["cancelled_users"].pop(user_id, None)
        hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
        return False
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
    data["entries"][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await panel.async_register_panel(hass)
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options saved (appearance, sendspin_url, diagnostics_url): push both snapshots over the live subscription; no reload."""
    coordinator: HeliosCoordinator | None = hass.data[DOMAIN]["entries"].get(entry.entry_id)
    if coordinator is not None:
        coordinator.send_appearance()
        coordinator.send_connection(identity.connection_payload(hass, dict(entry.data), dict(entry.options)))


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data.setdefault(DOMAIN, new_domain_data())
    data["cancelled_users"].pop(entry.data.get("user_id"), None)
    ir.async_delete_issue(hass, DOMAIN, f"no_music_{entry.entry_id}")
    data["locks"].pop(entry.data.get("installation_id"), None)
    await identity.async_remove_identity(hass, entry.data.get("user_id"))
    await identity.async_revoke_music_section(hass, entry.data.get("music_assistant"))
    await hass.async_add_executor_job(shutil.rmtree, hass.config.path("helios", entry.entry_id), True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: HeliosCoordinator | None = hass.data[DOMAIN]["entries"].pop(entry.entry_id, None)
    if coordinator is not None:
        coordinator.send_removed()
    if not hass.data[DOMAIN]["entries"]:
        panel.async_remove_panel(hass)  # the sidebar entry goes with the last clock
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
