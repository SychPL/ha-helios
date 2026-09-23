"""The helios/* WebSocket channel: connect (subscription), state and result."""

from __future__ import annotations

import asyncio

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_device_registry_updated_event

from . import identity
from .const import DOMAIN, MUSIC_SECTION_REVISION, PROTOCOL, PROTOCOLS, dashboard_path_for, valid_dashboard_path
from .coordinator import HeliosCoordinator


def _entry_for(hass: HomeAssistant, installation_id: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.unique_id == installation_id:
            return entry
    return None


def _coordinator_for_connection(hass: HomeAssistant, connection) -> HeliosCoordinator | None:
    for coordinator in hass.data[DOMAIN]["entries"].values():
        if coordinator.is_owner(connection):
            return coordinator
    return None


@websocket_api.websocket_command(
    {
        vol.Required("type"): "helios/connect",
        vol.Required("protocol"): int,
        vol.Required("installation_id"): cv.string,
        vol.Required("app_version"): cv.string,
        vol.Required("version_code"): int,
        vol.Optional("capabilities"): [cv.string],
    }
)
@websocket_api.async_response
async def ws_connect(hass: HomeAssistant, connection, msg: dict) -> None:
    """Subscription owned by one clock (SPEC 0.10 pkt 4.1, 6.2): the entry must exist and belong to this user; pairing happens over HTTP."""
    data = hass.data[DOMAIN]
    installation_id = msg["installation_id"]
    if msg["protocol"] not in PROTOCOLS:
        connection.send_error(msg["id"], "unsupported_protocol", f"Helios speaks protocol {PROTOCOL}")
        return

    def owned():
        """The entry as of now, or None when it is gone or belongs to someone else (re-paired meanwhile)."""
        current = _entry_for(hass, installation_id)
        return current if current is not None and current.data.get("user_id") == connection.user.id else None

    entry = owned()
    if entry is None:
        connection.send_error(msg["id"], "unauthorized", "Unknown device, or paired with a different user - pair it again with a code")
        return
    section = entry.data.get("music_assistant")
    manual = (entry.options.get("music_token") or "").strip()
    source = identity.music_source(hass) if not manual else identity.quiet_music_source(hass)
    # MA gone, a different MA server, a pasted token that changed, or a section from an older version (address or token unusable)
    stale = section is not None and (
        (source is None and not manual)
        or (source is not None and section.get("source_url") != source[0])
        or section.get("minted") != MUSIC_SECTION_REVISION
        or (manual and section.get("token") != manual)
        or (not manual and section.get("manual"))
    )
    missing = section is None and (source is not None or manual)
    if stale or missing:
        lock = data["locks"].setdefault(installation_id, asyncio.Lock())
        try:
            async with lock:  # the same lock the pairing transaction holds through its steps 3-5
                entry = owned()
                if entry is None:
                    connection.send_error(msg["id"], "unauthorized", "This device has been paired again")
                    return
                if entry.data.get("music_assistant") != section:
                    section = entry.data.get("music_assistant")  # another connect already did the work
                else:
                    old = section
                    fresh = await identity.async_create_music_section(hass, installation_id, dict(entry.options)) if (source is not None or manual) else None
                    try:
                        hass.config_entries.async_update_entry(entry, data={**entry.data, "music_assistant": fresh})
                    except Exception:  # noqa: BLE001 - the stored (old) section stays valid: only the token of this attempt goes
                        await identity.async_revoke_music_section(hass, fresh)
                    else:
                        section = fresh
                        await identity.async_revoke_music_section(hass, old)  # only after the new section is persisted (best effort when the server is gone)
        finally:
            if not lock.locked() and not lock._waiters:  # noqa: SLF001 - every exit path drops an idle lock
                data["locks"].pop(installation_id, None)
    coordinator: HeliosCoordinator | None = None
    for _ in range(50):  # a freshly created or reloaded entry finishes its setup shortly after the flow completes
        entry = owned()
        if entry is None:
            break
        coordinator = data["entries"].get(entry.entry_id)
        if coordinator is not None:
            break
        await asyncio.sleep(0.2)
    # directly before attach, after the last await: a re-pair or reload meanwhile replaces both the entry data and the coordinator
    entry = owned()
    if entry is None:
        connection.send_error(msg["id"], "unauthorized", "This device has been paired again")
        return
    if coordinator is None or data["entries"].get(entry.entry_id) is not coordinator:
        connection.send_error(msg["id"], "not_ready", "The Helios integration is still loading")
        return
    sub_id = msg["id"]
    coordinator.attach(connection, sub_id)

    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, installation_id)})
    _music_issue(hass, entry, section, device)

    @callback
    def device_updated(event) -> None:
        """Name or area edited in HA: tell the clock so its player name and voice context follow the HA device."""
        if not coordinator.is_owner(connection, sub_id):
            return
        current = dr.async_get(hass).async_get(event.data["device_id"])
        if current is not None:
            connection.send_event(sub_id, {"type": "device", "device_id": current.id, "area_id": current.area_id, "name": current.name_by_user or current.name})

    unsubscribe_registry = async_track_device_registry_updated_event(hass, device.id, device_updated) if device else None

    @callback
    def cleanup() -> None:
        if unsubscribe_registry is not None:
            unsubscribe_registry()
        coordinator.detach(connection, sub_id, "disconnected")

    connection.subscriptions[sub_id] = cleanup
    connection.send_result(sub_id)
    connection.send_event(
        sub_id,
        {"type": "connected", "device_id": device.id if device else None, "area_id": device.area_id if device else None, "name": (device.name_by_user or device.name) if device else None},
    )
    coordinator.send_appearance()
    coordinator.send_connection(identity.connection_payload(hass, dict(entry.data), dict(entry.options)))


@callback
def _music_issue(hass: HomeAssistant, entry, section: dict | None, device) -> None:
    """A clock without music while Music Assistant runs is a Repairs item, not just a log line (the add-on mints no token)."""
    issue_id = f"no_music_{entry.entry_id}"
    if section is None and identity.quiet_music_source(hass) is not None:
        name = (device.name_by_user or device.name) if device else entry.title
        ir.async_create_issue(hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="no_music", translation_placeholders={"name": name})
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)


@websocket_api.websocket_command({vol.Required("type"): "helios/state", vol.Required("state"): dict})
@callback
def ws_state(hass: HomeAssistant, connection, msg: dict) -> None:
    coordinator = _coordinator_for_connection(hass, connection)
    if coordinator is None:
        connection.send_error(msg["id"], "unauthorized", "No active helios/connect subscription")
        return
    coordinator.set_state(msg["state"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "helios/result", vol.Required("request_id"): cv.string, vol.Required("status"): cv.string, vol.Optional("code"): cv.string}
)
@callback
def ws_result(hass: HomeAssistant, connection, msg: dict) -> None:
    coordinator = _coordinator_for_connection(hass, connection)
    if coordinator is None:
        connection.send_error(msg["id"], "unauthorized", "No active helios/connect subscription")
        return
    coordinator.resolve(msg["request_id"], msg["status"], msg.get("code"))
    connection.send_result(msg["id"])


@websocket_api.websocket_command({vol.Required("type"): "helios/clocks"})
@websocket_api.require_admin
@callback
def ws_clocks(hass: HomeAssistant, connection, msg: dict) -> None:
    """SPEC 0.16: one row per paired clock for the panel's tabs - its HA device name, area and the dashboard it shows."""
    devices, areas = dr.async_get(hass), ar.async_get(hass)
    entries = hass.data.get(DOMAIN, {}).get("entries", {})
    clocks = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        device = devices.async_get_device(identifiers={(DOMAIN, entry.data.get("installation_id"))})
        area = areas.async_get_area(device.area_id) if device and device.area_id else None
        coordinator = entries.get(entry.entry_id)
        clocks.append({
            "entry_id": entry.entry_id,
            "name": (device.name_by_user or device.name) if device else entry.title,
            "area": area.name if area else None,
            "dashboard_path": dashboard_path_for(dict(entry.data)),
            "online": coordinator is not None and coordinator.connection is not None,
        })
    clocks.sort(key=lambda c: (c["name"] or "").casefold())
    connection.send_result(msg["id"], {"clocks": clocks})


@websocket_api.websocket_command({vol.Required("type"): "helios/clock/set_dashboard", vol.Required("entry_id"): cv.string, vol.Required("dashboard_path"): cv.string})
@websocket_api.require_admin
@callback
def ws_set_dashboard(hass: HomeAssistant, connection, msg: dict) -> None:
    """SPEC 0.16: point one clock at another dashboard; the options listener pushes the new path to the clock."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "Unknown clock")
        return
    path = msg["dashboard_path"]
    if not valid_dashboard_path(path):
        connection.send_error(msg["id"], "invalid_format", "Dashboard path must be a lowercase slug with a hyphen, at most 64 characters")
        return
    # entry.data, not options: the options form writes its whole dict back after its step returns, outside any lock;
    # read-modify-write here has no await in between, so it cannot interleave with ws_connect's data update either
    hass.config_entries.async_update_entry(entry, data={**entry.data, "dashboard_path": path})
    connection.send_result(msg["id"], {"dashboard_path": path})


@callback
def async_register(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_connect)
    websocket_api.async_register_command(hass, ws_state)
    websocket_api.async_register_command(hass, ws_result)
    websocket_api.async_register_command(hass, ws_clocks)
    websocket_api.async_register_command(hass, ws_set_dashboard)
