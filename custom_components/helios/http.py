"""Authenticated GET for a clock's prepared background image (SPEC 0.8b pkt 5)."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.http import KEY_HASS

from .appearance import IMAGE_ID_RE
from .const import DOMAIN


class HeliosAppearanceView(HomeAssistantView):
    """Serves only the entry's current image to its paired user or an admin; stale ids and foreign pairs are 404."""

    url = "/api/helios/appearance/{entry_id}/{image_id}"
    name = "api:helios:appearance"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, image_id: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        if not IMAGE_ID_RE.match(image_id):
            return web.Response(status=404)
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            return web.Response(status=404)
        user = request["hass_user"]
        if not (user.is_admin or user.id == entry.data.get("user_id")):
            return web.Response(status=403)
        if entry.options.get("image_id") != image_id:
            return web.Response(status=404)
        path = Path(hass.config.path("helios", entry.entry_id, f"{image_id}.jpg"))  # built only from the validated id, never from the URL text
        if not path.is_file():
            return web.Response(status=404)
        return web.FileResponse(path, headers={"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=31536000"})
