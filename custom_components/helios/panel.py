"""The dashboard editor: an admin-only custom panel in the sidebar; its modules are served by HeliosPanelFileView (http.py).

The page edits the `helios` section of a storage-mode Lovelace dashboard through the frontend's own `lovelace/config`
and `lovelace/config/save` commands; its clock tabs come from `helios/clocks` and `helios/clock/set_dashboard` (SPEC 0.16).
"""

from __future__ import annotations

from homeassistant.components import frontend
from homeassistant.core import HomeAssistant, callback
from homeassistant.loader import async_get_integration

from .const import DASHBOARD_PATH, DOMAIN, PANEL_STATIC_URL, PANEL_URL_PATH


async def async_register_panel(hass: HomeAssistant) -> None:
    """Idempotent: the second clock finds the panel already there. Admin-only, like the dashboard it writes to."""
    if frontend.async_panel_exists(hass, PANEL_URL_PATH):
        return
    version = (await async_get_integration(hass, DOMAIN)).version
    # what panel_custom.async_register_panel builds (panel_custom/__init__.py), without its hard dependency on the frontend being set up in tests
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title="Helios",
        sidebar_icon="mdi:clock-digital",
        frontend_url_path=PANEL_URL_PATH,
        require_admin=True,
        config_panel_domain=DOMAIN,
        config={
            "dashboard_path": DASHBOARD_PATH,
            "version": str(version),
            "_panel_custom": {"name": "helios-panel", "embed_iframe": False, "trust_external": False, "handle_safe_area": False, "module_url": f"{PANEL_STATIC_URL}/helios-panel.js?v={version}"},
        },
    )


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
