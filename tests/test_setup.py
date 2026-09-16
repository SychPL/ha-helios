import pytest

from custom_components.helios import async_setup
from custom_components.helios.const import DOMAIN


@pytest.mark.xfail(strict=True, reason="A4: async_register_views")
async def test_setup_registers_both_views_once(hass):
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})  # the bare hass fixture has no http app
    assert await async_setup(hass, {})
    assert await async_setup(hass, {})
    routes = [(r.method, r.resource.canonical) for r in hass.http.app.router.routes()]  # HA registers routes without names
    assert routes.count(("GET", "/api/helios/appearance/{entry_id}/{image_id}")) == 1
    assert routes.count(("GET", "/api/helios/pair")) == 1 and routes.count(("POST", "/api/helios/pair")) == 1
    assert hass.data[DOMAIN]["views_registered"] is True
