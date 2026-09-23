"""HTTP views: the authenticated background image (SPEC 0.8b pkt 5) and the unauthenticated pairing endpoint (SPEC 0.10 pkt 4.1)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_HASS

from . import identity
from .appearance import IMAGE_ID_RE
from .const import (
    DASHBOARD_PATH,
    DOMAIN,
    FLOW_TIMEOUT_SECONDS,
    PAIR_BODY_LIMIT,
    PAIRING_TIMEOUT_SECONDS,
    PANEL_STATIC_URL,
    PROTOCOL,
    SETUP_TIMEOUT_SECONDS,
    new_domain_data,
    parse_pair_request,
)
from .websocket import _entry_for

_LOGGER = logging.getLogger(__name__)
RETIRE_TIMEOUT_SECONDS = 10
ROLLBACK_TIMEOUT_SECONDS = 10  # worst case: PAIRING_TIMEOUT (30) + restore reload (10) + rollback (10) = 50 s < the clock's 60 s read timeout
CANCELLED_USERS_TTL_SECONDS = 3600


def async_register_views(hass: HomeAssistant) -> None:
    """All views, once; called from async_setup and from the config flow (the first pairing runs before async_setup)."""
    data = hass.data.setdefault(DOMAIN, new_domain_data())
    if data.get("views_registered"):
        return
    hass.http.register_view(HeliosAppearanceView())
    hass.http.register_view(HeliosPairView())
    hass.http.register_view(HeliosPanelFileView())
    data["views_registered"] = True


class HeliosPanelFileView(HomeAssistantView):
    """The editor panel's two modules, served like any HA frontend asset (no auth: the browser loads them before it has a token).

    A view rather than a static path: static paths are refused once aiohttp has frozen the router (the test harness on Linux
    does that after the first client), and a view is what the pairing endpoint already relies on. No cache headers, because
    helios-panel.js imports helios-schema.js by a relative URL that cannot carry the version query.
    """

    url = f"{PANEL_STATIC_URL}/{{name}}"
    name = "api:helios:panel"
    requires_auth = False
    FILES = ("helios-panel.js", "helios-schema.js")

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        if name not in self.FILES:
            return web.Response(status=404)
        path = Path(__file__).parent / "panel" / name  # built only from the allowlisted name, never from the URL text
        return web.FileResponse(path, headers={"Content-Type": "text/javascript; charset=utf-8", "Cache-Control": "no-cache"})


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


class PairingFailed(Exception):
    def __init__(self, status: int, error: str) -> None:
        super().__init__(error)
        self.status, self.error = status, error


class HeliosPairView(HomeAssistantView):
    """Unauthenticated: the clock has no token yet. GET is a capability probe, POST trades a one-time code for the clock's own token."""

    url = "/api/helios/pair"
    name = "api:helios:pair"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        return self.json({"protocol": PROTOCOL})

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app[KEY_HASS]
        data = hass.data.setdefault(DOMAIN, new_domain_data())
        source = request.remote or "?"
        now = time.monotonic()
        for stale in [u for u, at in data["cancelled_users"].items() if now - at > CANCELLED_USERS_TTL_SECONDS]:
            data["cancelled_users"].pop(stale, None)  # bounded growth; async_setup_entry drops any entry whose user is gone anyway
        parsed = parse_pair_request(await _read_json(request))
        if parsed is None:
            return self.json({"error": "invalid_request"}, status_code=400)
        registry = data["pairing"]
        if registry.blocked(source):
            _LOGGER.warning("Helios: refused pairing from %s", source)
            return self.json({"error": "unauthorized"}, status_code=401)
        installation_id = parsed["installation_id"]
        if installation_id in data["pairing_active"]:
            return self.json({"error": "pairing_failed"}, status_code=409)
        data["pairing_active"].add(installation_id)
        try:
            lock = data["locks"].setdefault(installation_id, asyncio.Lock())
            async with lock:
                pending = registry.claim(parsed["code"], source)
                if pending is None:
                    _LOGGER.warning("Helios: refused pairing from %s", source)
                    return self.json({"error": "unauthorized"}, status_code=401)
                pending["existing"] = _entry_for(hass, installation_id)  # read under the lock: a re-pair in flight cannot slip in between
                try:
                    async with asyncio.timeout(PAIRING_TIMEOUT_SECONDS):
                        token, pipeline = await _pair(hass, data, pending, parsed)
                except PairingFailed as err:
                    return self.json({"error": err.error}, status_code=err.status)
                except TimeoutError:
                    return self.json({"error": "not_ready"}, status_code=503)
            _LOGGER.info("Helios %s paired from %s", installation_id[:8], source)
            return self.json({"protocol": PROTOCOL, "token": token, "pipeline": pipeline, "dashboard_path": DASHBOARD_PATH})
        finally:
            data["pairing_active"].discard(installation_id)
            lock = data["locks"].get(installation_id)
            if lock is not None and not lock.locked() and not lock._waiters:  # noqa: SLF001 - drop idle locks so random ids cannot grow the map
                data["locks"].pop(installation_id, None)


async def _read_json(request: web.Request) -> object:
    """Bounded read (also for chunked bodies), then JSON; anything over PAIR_BODY_LIMIT, slow or malformed is None."""
    chunks, size = [], 0
    try:
        async with asyncio.timeout(5):
            while True:
                chunk = await request.content.read(PAIR_BODY_LIMIT)
                if not chunk:
                    break
                size += len(chunk)
                if size > PAIR_BODY_LIMIT:
                    return None
                chunks.append(chunk)
        return json.loads(b"".join(chunks))
    except (TimeoutError, ValueError, UnicodeDecodeError):
        return None


async def _pair(hass: HomeAssistant, data: dict, pending: dict, parsed: dict) -> tuple[str, str]:
    """SPEC 0.10 pkt 4.1 steps 3-5: new identity first, the entry, then (past the commit point) the old identity goes."""
    installation_id = parsed["installation_id"]
    pipeline = identity.preferred_pipeline(hass)
    if pipeline is None:
        await _rollback(hass, data, pending, None, None, pending["existing"] is None)  # the code is spent: the flow ends now
        raise PairingFailed(503, "not_ready")
    user_id = music = None
    existing = pending["existing"]
    previous_user = previous_music = None
    try:
        user_id, token = await identity.async_create_identity(hass, installation_id)
        music = await identity.async_create_music_section(hass, installation_id, dict(existing.options) if existing is not None else {})
        entry_data = {"installation_id": installation_id, "user_id": user_id, "app_version": parsed["app_version"], "version_code": parsed["version_code"], "music_assistant": music}
        if existing is not None:
            previous = dict(existing.data)
            previous_user, previous_music = previous.get("user_id"), previous.get("music_assistant")
            hass.config_entries.async_update_entry(existing, data={**previous, **entry_data})
            ok = False
            try:
                ok = await hass.config_entries.async_reload(existing.entry_id)
            finally:
                if not ok:
                    # the restoring reload is part of the transaction (criterion 15: a working old entry before the answer), with its own bound
                    # only the fields this pairing wrote go back: a dashboard assigned meanwhile (SPEC 0.16) must survive the rollback
                    hass.config_entries.async_update_entry(existing, data={**existing.data, **{key: previous.get(key) for key in entry_data}})
                    try:
                        async with asyncio.timeout(ROLLBACK_TIMEOUT_SECONDS):  # its own bound, also after the transaction timeout already fired
                            restored = await hass.config_entries.async_reload(existing.entry_id)
                    except TimeoutError:
                        restored = False
                    if not restored:
                        _LOGGER.error("Helios %s: restoring the previous entry failed", installation_id[:8])
            if not ok:
                raise PairingFailed(503, "not_ready")
            if not pending["future"].done():
                pending["future"].set_result({"reconfigured": True})
        else:
            if not pending["future"].done():
                pending["future"].set_result(entry_data)
            try:
                async with asyncio.timeout(FLOW_TIMEOUT_SECONDS):
                    await pending["done"].wait()
                    while any(f["flow_id"] == pending["flow_id"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)):
                        await asyncio.sleep(0.1)  # the flow manager finishes async_create_entry after the step returns
            except TimeoutError as err:
                raise PairingFailed(409, "pairing_failed") from err
            for _ in range(int(SETUP_TIMEOUT_SECONDS / 0.2)):
                entry = _entry_by_user(hass, user_id)
                if entry is not None and entry.state is ConfigEntryState.LOADED:
                    break
                await asyncio.sleep(0.2)
            else:
                raise PairingFailed(503, "not_ready")
    except PairingFailed:
        await _rollback(hass, data, pending, user_id, music, existing is None)
        raise
    except BaseException as err:
        await _rollback(hass, data, pending, user_id, music, existing is None)
        if isinstance(err, TimeoutError | asyncio.CancelledError | SystemExit | KeyboardInterrupt):
            raise  # a shutdown or a cancel is not a pairing error and must not be swallowed
        _LOGGER.warning("Helios %s: pairing failed: %s", installation_id[:8], type(err).__name__)
        raise PairingFailed(503, "not_ready") from err
    # commit point: the new entry is loaded, nothing below may fail the request
    hass.async_create_task(_retire(hass, previous_user if previous_user != user_id else None, previous_music))
    return token, pipeline


def _entry_by_user(hass: HomeAssistant, user_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get("user_id") == user_id:
            return entry
    return None


async def _rollback(hass: HomeAssistant, data: dict, pending: dict, user_id: str | None, music: dict | None, new_entry: bool) -> None:
    """Own budget: runs after the transaction timeout may already have fired, so nothing here may hang."""
    pending["cancelled"] = True
    if new_entry and user_id:
        # async_abort() only forgets the flow; a finish that is already running may still create the entry later.
        # The marker stays until async_setup_entry consumes it or it ages out (purged in the view, 1 h); async_setup_entry
        # also drops any entry whose user no longer exists, so a finish later than the TTL cannot keep an entry either.
        data["cancelled_users"][user_id] = time.monotonic()
    try:
        hass.config_entries.flow.async_abort(pending["flow_id"])
    except Exception:  # noqa: BLE001 - already finished
        pass
    names, steps = [], []
    if user_id:
        entry = _entry_by_user(hass, user_id)
        if entry is not None:
            names.append("wpis")
            steps.append(hass.config_entries.async_remove(entry.entry_id))
        names.append("user")
        steps.append(identity.async_remove_identity(hass, user_id))
    names.append("token MA")
    steps.append(identity.async_revoke_music_section(hass, music))
    try:
        async with asyncio.timeout(ROLLBACK_TIMEOUT_SECONDS):
            # all at once and independently (like _retire): a hung entry removal never starves the user or the MA token;
            # the removals are idempotent, so async_remove_entry's own cleanup running alongside is harmless
            results = await asyncio.gather(*steps, return_exceptions=True)
    except TimeoutError:
        _LOGGER.error("Helios: cleaning up a failed pairing took longer than %s s", ROLLBACK_TIMEOUT_SECONDS)
        return
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            _LOGGER.warning("Helios: cleaning up a failed pairing (%s): %s", name, type(result).__name__)


async def _retire(hass: HomeAssistant, user_id: str | None, music: dict | None) -> None:
    """Past the commit point: best effort, both steps independently under one 10 s budget, warnings only."""
    names = ("user", "MA token")
    steps = (identity.async_remove_identity(hass, user_id), identity.async_revoke_music_section(hass, music))
    try:
        async with asyncio.timeout(RETIRE_TIMEOUT_SECONDS):
            results = await asyncio.gather(*steps, return_exceptions=True)  # a hung step never blocks the other one
    except TimeoutError:
        _LOGGER.warning("The clock's previous identity was not fully removed within %s s", RETIRE_TIMEOUT_SECONDS)
        return
    for name, result in zip(names, results, strict=True):
        if isinstance(result, BaseException):
            _LOGGER.warning("The clock's previous identity was not fully removed (%s): %s", name, type(result).__name__)
