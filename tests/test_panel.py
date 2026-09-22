"""The dashboard editor panel: registered with the first clock, admin-only, its files served by a view, gone with the last clock."""

import json
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios import async_setup, identity
from custom_components.helios.const import DOMAIN, PANEL_STATIC_URL, PANEL_URL_PATH

INSTALLATIONS = ("0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69", "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d")
VERSION = json.loads((Path(__file__).resolve().parents[1] / "custom_components/helios/manifest.json").read_text(encoding="utf-8"))["version"]


async def paired(hass, installation):
    user_id, token = await identity.async_create_identity(hass, installation)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=installation, data={"installation_id": installation, "user_id": user_id, "app_version": "0.12.0", "version_code": 32, "music_assistant": None})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry


async def test_panel_is_registered_admin_only_with_a_versioned_module(hass):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup(hass, {})
    entry = await paired(hass, INSTALLATIONS[0])
    panel = hass.data[frontend.DATA_PANELS][PANEL_URL_PATH]
    assert panel.require_admin is True and panel.component_name == "custom" and panel.sidebar_title == "Helios"
    custom = panel.config["_panel_custom"]
    assert custom["name"] == "helios-panel" and custom["embed_iframe"] is False
    assert custom["module_url"] == f"{PANEL_STATIC_URL}/helios-panel.js?v={VERSION}"
    assert panel.config["dashboard_path"] == "helios-clock" and panel.config["version"] == VERSION
    # a reload finds the panel already there instead of raising "Overwriting panel"
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]


async def test_panel_survives_one_clock_and_goes_with_the_last(hass):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup(hass, {})
    first, second = [await paired(hass, i) for i in INSTALLATIONS]
    assert await hass.config_entries.async_unload(first.entry_id)
    assert PANEL_URL_PATH in hass.data[frontend.DATA_PANELS]
    assert await hass.config_entries.async_unload(second.entry_id)
    assert PANEL_URL_PATH not in hass.data[frontend.DATA_PANELS]


async def test_panel_files_are_served_without_auth_and_nothing_else_is(hass, hass_client_no_auth):
    assert await async_setup_component(hass, "http", {})
    assert await async_setup(hass, {})
    client = await hass_client_no_auth()
    response = await client.get(f"{PANEL_STATIC_URL}/helios-panel.js")
    assert response.status == 200 and response.headers["Content-Type"].startswith("text/javascript") and "no-cache" in response.headers["Cache-Control"]
    assert "customElements.define('helios-panel'" in await response.text()
    schema = await client.get(f"{PANEL_STATIC_URL}/helios-schema.js")
    assert schema.status == 200 and "export function validate" in await schema.text()
    for name in ("package.json", "../__init__.py", "missing.js", "helios-panel.js.bak"):
        assert (await client.get(f"{PANEL_STATIC_URL}/{name}")).status == 404, name
    assert (await client.get(f"{PANEL_STATIC_URL}/..%2F__init__.py")).status != 200  # an encoded slash is refused before the view sees it
