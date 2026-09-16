import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios import identity
from custom_components.helios.const import DOMAIN

INSTALLATION = "0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69"


async def paired(hass):
    user_id, token = await identity.async_create_identity(hass, INSTALLATION)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=INSTALLATION, data={"installation_id": INSTALLATION, "user_id": user_id, "app_version": "0.9.0", "version_code": 28, "music_assistant": None})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return entry, token


async def connect(ws, protocol=2, msg_id=1):
    await ws.send_json({"id": msg_id, "type": "helios/connect", "protocol": protocol, "installation_id": INSTALLATION, "app_version": "0.9.0", "version_code": 28})
    result = await ws.receive_json()
    events = []
    if result["success"]:
        for _ in range(3):
            events.append((await ws.receive_json())["event"])
    return result, events


async def test_connect_accepts_protocol_1_and_2_and_sends_connection(hass, hass_ws_client):
    entry, token = await paired(hass)
    for protocol in (1, 2):
        ws = await hass_ws_client(hass, access_token=token)
        result, events = await connect(ws, protocol)
        assert result["success"], protocol
        assert [e["type"] for e in events] == ["connected", "appearance", "connection"]
        assert events[2]["music_assistant"] is None and events[2]["dashboard_path"] == "helios-clock" and events[2]["diagnostics_url"] is None


async def test_pairing_code_is_no_longer_part_of_connect(hass, hass_ws_client):
    entry, token = await paired(hass)
    ws = await hass_ws_client(hass, access_token=token)
    await ws.send_json({"id": 1, "type": "helios/connect", "protocol": 2, "installation_id": INSTALLATION, "app_version": "0.9.0", "version_code": 28, "pairing_code": "123456"})
    assert (await ws.receive_json())["error"]["code"] == "invalid_format"


async def test_protocol_3_is_refused(hass, hass_ws_client):
    entry, token = await paired(hass)
    ws = await hass_ws_client(hass, access_token=token)
    result, _ = await connect(ws, 3)
    assert result["error"]["code"] == "unsupported_protocol"


async def test_music_section_is_minted_on_connect_when_ma_appears(hass, hass_ws_client, monkeypatch):
    entry, token = await paired(hass)
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://ma:8095", "ma-token"))

    async def fake_create(hass, installation_id):
        return {"url": "http://ma:8095", "token": "clock-token"}

    monkeypatch.setattr(identity, "async_create_music_section", fake_create)
    ws = await hass_ws_client(hass, access_token=token)
    _, events = await connect(ws)
    assert events[2]["music_assistant"] == {"url": "http://ma:8095", "token": "clock-token", "sendspin_url": "ws://ma:8927/sendspin"}
    assert entry.data["music_assistant"] == {"url": "http://ma:8095", "token": "clock-token"}


async def until_connection(ws):
    async with __import__("asyncio").timeout(3):
        while True:
            msg = await ws.receive_json()
            if msg.get("event", {}).get("type") == "connection":
                return msg["event"]


async def test_options_change_resends_connection_without_restart(hass, hass_ws_client):
    entry, token = await paired(hass)
    coordinator = hass.data[DOMAIN]["entries"][entry.entry_id]
    ws = await hass_ws_client(hass, access_token=token)
    await connect(ws)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "diagnostics_url": "http://pc:8757/x/events"})
    await hass.async_block_till_done()
    assert (await until_connection(ws))["diagnostics_url"] == "http://pc:8757/x/events"
    hass.config_entries.async_update_entry(entry, data={**entry.data, "music_assistant": {"url": "http://ma:8095", "token": "clock-token"}}, options={**entry.options, "sendspin_url": "ws://other:8927/sendspin"})
    await hass.async_block_till_done()
    assert (await until_connection(ws))["music_assistant"]["sendspin_url"] == "ws://other:8927/sendspin"
    assert hass.data[DOMAIN]["entries"][entry.entry_id] is coordinator and entry.state is ConfigEntryState.LOADED, "options never reload the entry"


async def test_failed_section_save_keeps_the_old_token_and_revokes_only_the_new_one(hass, hass_ws_client, monkeypatch):
    entry, token = await paired(hass)
    old = {"url": "http://old:8095", "token": "old-clock-token"}
    hass.config_entries.async_update_entry(entry, data={**entry.data, "music_assistant": old})
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://new:8095", "ma-token"))
    revoked = []

    async def fake_revoke(hass, section):
        revoked.append(section)

    async def fake_create(hass, installation_id):
        return {"url": "http://new:8095", "token": "new-clock-token"}

    def failing_update(entry_, **kwargs):
        raise RuntimeError("store down")

    monkeypatch.setattr(identity, "async_revoke_music_section", fake_revoke)
    monkeypatch.setattr(identity, "async_create_music_section", fake_create)
    monkeypatch.setattr(hass.config_entries, "async_update_entry", failing_update)
    ws = await hass_ws_client(hass, access_token=token)
    _, events = await connect(ws)
    assert entry.data["music_assistant"] == old and events[2]["music_assistant"]["token"] == "old-clock-token"
    assert revoked == [{"url": "http://new:8095", "token": "new-clock-token"}], "the old token is never revoked before the new section is persisted"


async def test_ma_removed_from_ha_drops_the_section(hass, hass_ws_client, monkeypatch):
    entry, token = await paired(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "music_assistant": {"url": "http://old:8095", "token": "old-clock-token"}})
    revoked = []

    async def fake_revoke(hass, section):
        revoked.append(section)

    monkeypatch.setattr(identity, "async_revoke_music_section", fake_revoke)
    ws = await hass_ws_client(hass, access_token=token)
    _, events = await connect(ws)  # no loaded music_assistant entry in this test
    assert events[2]["music_assistant"] is None and entry.data["music_assistant"] is None
    assert revoked == [{"url": "http://old:8095", "token": "old-clock-token"}]


async def test_ma_server_change_revokes_the_old_token_and_mints_a_new_one(hass, hass_ws_client, monkeypatch):
    entry, token = await paired(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "music_assistant": {"url": "http://old:8095", "token": "old-clock-token"}})
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://new:8095", "ma-token"))
    revoked, created = [], []

    async def fake_revoke(hass, section):
        revoked.append(section)

    async def fake_create(hass, installation_id):
        created.append(installation_id)
        return {"url": "http://new:8095", "token": "new-clock-token"}

    monkeypatch.setattr(identity, "async_revoke_music_section", fake_revoke)
    monkeypatch.setattr(identity, "async_create_music_section", fake_create)
    ws = await hass_ws_client(hass, access_token=token)
    _, events = await connect(ws)
    assert revoked == [{"url": "http://old:8095", "token": "old-clock-token"}] and created == [INSTALLATION]
    assert events[2]["music_assistant"] == {"url": "http://new:8095", "token": "new-clock-token", "sendspin_url": "ws://new:8927/sendspin"}
    assert entry.data["music_assistant"]["url"] == "http://new:8095"


async def test_connect_rechecks_the_owner_after_waiting_for_the_lock(hass, hass_ws_client, monkeypatch):
    entry, token = await paired(hass)
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://ma:8095", "ma-token"))
    import asyncio

    lock = hass.data[DOMAIN]["locks"].setdefault(INSTALLATION, asyncio.Lock())
    await lock.acquire()
    ws = await hass_ws_client(hass, access_token=token)
    await ws.send_json({"id": 1, "type": "helios/connect", "protocol": 2, "installation_id": INSTALLATION, "app_version": "0.9.0", "version_code": 28})
    async with asyncio.timeout(3):
        while not lock._waiters:  # noqa: SLF001 - the handler must really be parked on the lock before the owner changes
            await asyncio.sleep(0.01)
    new_user, _ = await identity.async_create_identity(hass, INSTALLATION)  # re-pair while the connect waits
    hass.config_entries.async_update_entry(entry, data={**entry.data, "user_id": new_user})
    lock.release()
    result = await ws.receive_json()
    assert result["success"] is False and result["error"]["code"] == "unauthorized"
