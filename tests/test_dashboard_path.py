"""SPEC 0.16: each clock has its own dashboard path, listed and switched by the panel over two admin-only commands."""

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios import identity
from custom_components.helios.const import DOMAIN, dashboard_path_for, valid_dashboard_path

BEDROOM, OFFICE = "0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69", "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"


async def paired(hass, installation, **extra):
    user_id, token = await identity.async_create_identity(hass, installation)
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=installation, title=f"Helios {installation[:8]}",
        data={"installation_id": installation, "user_id": user_id, "app_version": "0.13.0", "version_code": 33, "music_assistant": None, **extra},
        options={"diagnostics_url": "http://diag.local/x"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry, token


async def connect(ws, installation):
    await ws.send_json({"id": 1, "type": "helios/connect", "protocol": 2, "installation_id": installation, "app_version": "0.13.0", "version_code": 33})
    assert (await ws.receive_json())["success"]
    return [(await ws.receive_json())["event"] for _ in range(3)]


def test_path_validation_and_fallback():
    assert valid_dashboard_path("helios-clock") and valid_dashboard_path("helios-zegar-gabinet-2")
    for bad in ("helios", "Helios-clock", "a--b", "-a-b", "a-b-", "a_b-c", "helios-" + "x" * 58, None, 5):
        assert not valid_dashboard_path(bad), bad
    assert dashboard_path_for({}) == "helios-clock"
    assert dashboard_path_for({"dashboard_path": "Bad Path"}) == "helios-clock"
    assert dashboard_path_for({"dashboard_path": "helios-gabinet"}) == "helios-gabinet"


async def test_default_path_is_unchanged(hass, hass_ws_client):
    entry, token = await paired(hass, BEDROOM)
    events = await connect(await hass_ws_client(hass, access_token=token), BEDROOM)
    assert events[2]["type"] == "connection" and events[2]["dashboard_path"] == "helios-clock"


async def test_set_dashboard_switches_the_live_clock_and_keeps_everything_else(hass, hass_ws_client):
    entry, token = await paired(hass, BEDROOM)
    clock = await hass_ws_client(hass, access_token=token)
    await connect(clock, BEDROOM)
    admin = await hass_ws_client(hass)
    options_before = dict(entry.options)
    await admin.send_json({"id": 5, "type": "helios/clock/set_dashboard", "entry_id": entry.entry_id, "dashboard_path": "helios-gabinet"})
    assert (await admin.receive_json())["result"] == {"dashboard_path": "helios-gabinet"}
    await hass.async_block_till_done()
    assert entry.data["dashboard_path"] == "helios-gabinet" and entry.data["installation_id"] == BEDROOM
    assert dict(entry.options) == options_before
    while True:  # the update listener pushes appearance and connection; the path arrives without a reconnect
        event = (await clock.receive_json())["event"]
        if event["type"] == "connection":
            break
    assert event["dashboard_path"] == "helios-gabinet" and event["diagnostics_url"] == "http://diag.local/x"


async def test_set_dashboard_refuses_bad_input_and_non_admins(hass, hass_ws_client):
    entry, token = await paired(hass, BEDROOM)
    admin = await hass_ws_client(hass)
    cases = [({"entry_id": "nope", "dashboard_path": "helios-x"}, "not_found"), ({"entry_id": entry.entry_id, "dashboard_path": "helios"}, "invalid_format"),
             ({"entry_id": entry.entry_id, "dashboard_path": "Helios-X"}, "invalid_format")]
    for n, (body, code) in enumerate(cases, start=10):
        await admin.send_json({"id": n, "type": "helios/clock/set_dashboard", **body})
        assert (await admin.receive_json())["error"]["code"] == code, body
    clock = await hass_ws_client(hass, access_token=token)  # the clock's own user is not an admin
    for n, body in enumerate(({"type": "helios/clocks"}, {"type": "helios/clock/set_dashboard", "entry_id": entry.entry_id, "dashboard_path": "helios-x"}), start=20):
        await clock.send_json({"id": n, **body})
        assert (await clock.receive_json())["error"]["code"] == "unauthorized"
    assert "dashboard_path" not in entry.data


async def test_clocks_lists_device_names_areas_and_paths(hass, hass_ws_client):
    bedroom, _ = await paired(hass, BEDROOM)
    office, token = await paired(hass, OFFICE, dashboard_path="helios-gabinet")
    devices = dr.async_get(hass)
    area = ar.async_get(hass).async_create("Biuro")
    devices.async_update_device(devices.async_get_device(identifiers={(DOMAIN, OFFICE)}).id, name_by_user="zegar gabinet", area_id=area.id)
    await connect(await hass_ws_client(hass, access_token=token), OFFICE)
    admin = await hass_ws_client(hass)
    await admin.send_json({"id": 1, "type": "helios/clocks"})
    clocks = (await admin.receive_json())["result"]["clocks"]
    assert clocks == [
        {"entry_id": bedroom.entry_id, "name": "Helios 0f3c1b2a", "area": None, "dashboard_path": "helios-clock", "online": False},
        {"entry_id": office.entry_id, "name": "zegar gabinet", "area": "Biuro", "dashboard_path": "helios-gabinet", "online": True},
    ]
