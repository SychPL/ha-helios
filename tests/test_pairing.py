import asyncio
import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios import identity
from custom_components.helios.const import DOMAIN, PAIRING_MAX_ATTEMPTS

INSTALLATION = "0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69"
BODY = {"installation_id": INSTALLATION, "code": None, "app_version": "0.9.0", "version_code": 28}


@pytest.fixture
def expected_lingering_timers(request):
    """The server-side close leaves aiohttp's websocket heartbeat timer behind in that one test; nothing of ours lingers."""
    return "closes_the_socket" in request.node.name


async def start_flow(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] == "progress"
    return result["flow_id"], result["description_placeholders"]["code"]


async def frontend(hass, flow_id):
    """When the progress task ends, HA only moves the flow to progress_done (data_entry_flow.py: the done callback calls the
    private single-step _async_configure); the HA frontend then continues to `finish`. Tests play the frontend."""
    from homeassistant.data_entry_flow import FlowResultType

    for _ in range(600):
        flow = hass.config_entries.flow._progress.get(flow_id)  # noqa: SLF001
        if flow is None:
            return
        if flow.cur_step and flow.cur_step["type"] is FlowResultType.SHOW_PROGRESS_DONE:
            try:
                await hass.config_entries.flow.async_configure(flow_id)
            except Exception:  # noqa: BLE001 - aborted meanwhile
                pass
            return
        await asyncio.sleep(0.05)


async def post(hass, client, flow_id, code, **overrides):
    """POST with the frontend running alongside (a wrong code never reaches progress_done, the task simply ends)."""
    body = {**BODY, "code": code, **overrides}
    ui = asyncio.ensure_future(frontend(hass, flow_id))
    try:
        return await client.post("/api/helios/pair", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    finally:
        ui.cancel()


async def test_get_is_a_bare_capability_probe(hass, hass_client_no_auth):
    flow_id, _ = await start_flow(hass)
    client = await hass_client_no_auth()
    response = await client.get("/api/helios/pair")
    assert response.status == 200 and await response.json() == {"protocol": 2}


async def test_pairing_creates_entry_identity_and_a_usable_token(hass, hass_client_no_auth, hass_ws_client):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    response = await post(hass, client, flow_id, code)
    assert response.status == 200
    body = await response.json()
    assert set(body) == {"protocol", "token", "pipeline", "dashboard_path"} and body["protocol"] == 2 and body["dashboard_path"] == "helios-clock"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == INSTALLATION and entry.state is ConfigEntryState.LOADED
    user = await hass.auth.async_get_user(entry.data["user_id"])
    assert user.system_generated and not user.is_admin
    ws = await hass_ws_client(hass, access_token=body["token"])
    await ws.send_json({"id": 1, "type": "helios/connect", "protocol": 2, "installation_id": INSTALLATION, "app_version": "0.9.0", "version_code": 28})
    assert (await ws.receive_json())["success"] is True
    assert (await post(hass, client, flow_id, code)).status == 401, "a code is single use"


async def test_wrong_codes_are_401_and_the_source_is_blocked_after_five(hass, hass_client_no_auth, caplog, monkeypatch):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    registry = hass.data[DOMAIN]["pairing"]
    claims = []
    real_claim = registry.claim
    monkeypatch.setattr(registry, "claim", lambda c, source: (claims.append(c), real_claim(c, source))[1])
    for _ in range(PAIRING_MAX_ATTEMPTS):
        assert (await post(hass, client, flow_id, "000000")).status == 401
    assert (await post(hass, client, flow_id, code)).status == 401, "blocked source, right code"
    assert claims == ["000000"] * PAIRING_MAX_ATTEMPTS, "the sixth request never reaches claim()"
    assert registry.pending(code), "the code itself survives"
    assert code not in caplog.text and "odrzucone parowanie" in caplog.text


async def test_chunked_and_oversized_bodies(hass, hass_client_no_auth, monkeypatch):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    registry = hass.data[DOMAIN]["pairing"]
    payload = json.dumps({**BODY, "code": code}).encode()

    async def chunks():
        for i in range(0, len(payload), 7):
            yield payload[i : i + 7]

    ui = asyncio.ensure_future(frontend(hass, flow_id))
    response = await client.post("/api/helios/pair", data=chunks(), headers={"Content-Type": "application/json"})  # aiohttp sends this chunked
    ui.cancel()
    assert response.status == 200, "a chunked body is read to EOF before parsing"

    def boom(*a, **k):
        raise AssertionError("claim() must not run for an oversized body")

    monkeypatch.setattr(registry, "claim", boom)

    async def big():
        yield b'{"installation_id":"' + b"a" * 5000 + b'"}'

    assert (await client.post("/api/helios/pair", data=big(), headers={"Content-Type": "application/json"})).status == 400


async def test_bad_bodies_are_400(hass, hass_client_no_auth):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    assert (await client.post("/api/helios/pair", data=b"{", headers={"Content-Type": "application/json"})).status == 400
    assert (await client.post("/api/helios/pair", data=b"x" * 5000, headers={"Content-Type": "application/json"})).status == 400
    assert (await post(hass, client, flow_id, "12345")).status == 400
    assert (await client.post("/api/helios/pair", data=b"[]", headers={"Content-Type": "application/json"})).status == 400


async def test_repairing_replaces_identity_only_after_success(hass, hass_client_no_auth):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    first = await (await post(hass, client, flow_id, code)).json()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    old_user = entry.data["user_id"]
    flow_id2, code2 = await start_flow(hass)
    second = await (await post(hass, client, flow_id2, code2)).json()
    assert second["token"] != first["token"]
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1 and entry.data["user_id"] != old_user
    await hass.async_block_till_done()  # _retire runs as a task after the answer
    assert await hass.auth.async_get_user(old_user) is None
    assert hass.auth.async_validate_access_token(second["token"]) is not None


async def test_failed_reload_restores_the_old_identity(hass, hass_client_no_auth, monkeypatch):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    first = await (await post(hass, client, flow_id, code)).json()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    old_user, old_data = entry.data["user_id"], dict(entry.data)
    flow_id2, code2 = await start_flow(hass)
    real_reload = hass.config_entries.async_reload
    calls = []

    async def failing_reload(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:
            return False
        return await real_reload(entry_id)

    monkeypatch.setattr(hass.config_entries, "async_reload", failing_reload)
    assert (await post(hass, client, flow_id2, code2)).status == 503
    assert calls == [entry.entry_id, entry.entry_id], "the restoring reload is awaited inside the transaction"
    assert entry.data == old_data and entry.state is ConfigEntryState.LOADED
    assert await hass.auth.async_get_user(old_user) is not None
    assert hass.auth.async_validate_access_token(first["token"]) is not None
    users = [u for u in await hass.auth.async_get_users() if u.system_generated and u.name.startswith("Helios")]
    assert [u.id for u in users] == [old_user], "the new identity of the failed attempt is gone"


async def test_setup_failure_of_the_new_data_restores_a_working_old_entry(hass, hass_client_no_auth, monkeypatch):
    """The realistic failure: the reload really unloads the entry and async_setup_entry raises for the new data."""
    import custom_components.helios as component

    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    first = await (await post(hass, client, flow_id, code)).json()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    old_user, old_data = entry.data["user_id"], dict(entry.data)
    flow_id2, code2 = await start_flow(hass)
    real_setup = component.async_setup_entry

    async def setup_once_broken(hass_, entry_):
        if entry_.data["user_id"] != old_user:
            raise RuntimeError("new data cannot load")
        return await real_setup(hass_, entry_)

    monkeypatch.setattr(component, "async_setup_entry", setup_once_broken)
    assert (await post(hass, client, flow_id2, code2)).status == 503
    await hass.async_block_till_done()
    assert entry.data == old_data and entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN]["entries"].get(entry.entry_id) is not None, "a live coordinator again"
    assert hass.auth.async_validate_access_token(first["token"]) is not None


async def test_hanging_restore_reload_is_bounded(hass, hass_client_no_auth, monkeypatch, caplog):
    from custom_components.helios import http as pair_http

    monkeypatch.setattr(pair_http, "PAIRING_TIMEOUT_SECONDS", 1.0)  # like 30 > 10: the restore bound fires first, inside the budget
    monkeypatch.setattr(pair_http, "ROLLBACK_TIMEOUT_SECONDS", 0.3)
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    await post(hass, client, flow_id, code)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    flow_id2, code2 = await start_flow(hass)
    calls = []

    async def reload(entry_id):
        calls.append(entry_id)
        if len(calls) == 1:
            return False
        await asyncio.sleep(3600)

    monkeypatch.setattr(hass.config_entries, "async_reload", reload)
    started = asyncio.get_running_loop().time()
    assert (await post(hass, client, flow_id2, code2)).status == 503
    assert asyncio.get_running_loop().time() - started < 2.5, "restore (0.3) inside the budget (1.0) plus rollback (0.3); in production 30 + 10 stays far under the clock's 60 s"
    assert "przywrócenie poprzedniego wpisu nie powiodło się" in caplog.text


async def test_cleanup_timeout_after_the_commit_point_keeps_the_new_identity(hass, hass_client_no_auth, monkeypatch):
    from custom_components.helios import http as pair_http

    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    await post(hass, client, flow_id, code)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    old_user = entry.data["user_id"]
    flow_id2, code2 = await start_flow(hass)

    async def stuck_remove(hass_, user_id):
        await asyncio.sleep(3600)

    revoked = []

    async def fake_revoke(hass_, section):
        revoked.append(section)

    monkeypatch.setattr(pair_http.identity, "async_remove_identity", stuck_remove)
    monkeypatch.setattr(pair_http.identity, "async_revoke_music_section", fake_revoke)
    monkeypatch.setattr(pair_http, "RETIRE_TIMEOUT_SECONDS", 0.3)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "music_assistant": {"url": "http://ma:8095", "token": "old-clock-token"}})
    started = asyncio.get_running_loop().time()
    response = await post(hass, client, flow_id2, code2)
    assert response.status == 200
    body = await response.json()
    await asyncio.sleep(0.5)
    await hass.async_block_till_done()
    assert asyncio.get_running_loop().time() - started < 1.5, "one shared budget (0.3 s here), not one per step"
    assert entry.data["user_id"] != old_user and entry.state is ConfigEntryState.LOADED
    assert hass.auth.async_validate_access_token(body["token"]) is not None, "past the commit point nothing rolls back"
    assert {"url": "http://ma:8095", "token": "old-clock-token"} in revoked, "the MA step runs even though the user step hung"


async def test_late_entry_creation_after_timeout_is_undone(hass, hass_client_no_auth, monkeypatch):
    from custom_components.helios import http as pair_http

    monkeypatch.setattr(pair_http, "FLOW_TIMEOUT_SECONDS", 0.2)
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    real_finish = hass.config_entries.flow.async_finish_flow

    async def slow_finish(flow, result):
        await asyncio.sleep(0.5)
        return await real_finish(flow, result)

    monkeypatch.setattr(hass.config_entries.flow, "async_finish_flow", slow_finish)  # ConfigEntriesFlowManager.async_finish_flow(flow, result), config_entries.py:1672 in 2026.8.3
    assert (await post(hass, client, flow_id, code)).status == 409
    await asyncio.sleep(0.6)
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert not [u for u in await hass.auth.async_get_users() if u.system_generated and u.name.startswith("Helios")]
    assert len(hass.data[DOMAIN]["cancelled_users"]) <= 1, "either consumed by the self-removing setup or left for the TTL purge; never an entry"


async def test_rollback_continues_when_one_step_fails(hass, hass_client_no_auth, monkeypatch, caplog):
    from custom_components.helios import http as pair_http

    monkeypatch.setattr(pair_http, "FLOW_TIMEOUT_SECONDS", 0.2)
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()

    async def never_finish(flow, result):
        await asyncio.sleep(3600)

    async def broken_remove(hass_, user_id):
        raise RuntimeError("store down")

    revoked = []

    async def fake_revoke(hass_, section):
        revoked.append(section)

    monkeypatch.setattr(hass.config_entries.flow, "async_finish_flow", never_finish)
    monkeypatch.setattr(pair_http.identity, "async_remove_identity", broken_remove)
    monkeypatch.setattr(pair_http.identity, "async_revoke_music_section", fake_revoke)
    monkeypatch.setattr(pair_http.identity, "async_create_music_section", lambda hass_, i: _coro({"url": "http://ma:8095", "token": "clock-token"}))
    assert (await post(hass, client, flow_id, code)).status == 409, "a controlled answer, never a 500"
    assert revoked == [{"url": "http://ma:8095", "token": "clock-token"}], "the later step still ran"
    assert "użytkownik" in caplog.text and "store down" not in caplog.text


async def _coro(value):
    return value


async def test_rollback_runs_all_steps_when_one_hangs(hass, hass_client_no_auth, monkeypatch):
    from custom_components.helios import http as pair_http

    monkeypatch.setattr(pair_http, "FLOW_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(pair_http, "ROLLBACK_TIMEOUT_SECONDS", 0.3)
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    gate = asyncio.Event()

    async def gated_finish(flow, result):
        await gate.wait()

    async def hung_remove(hass_, user_id):
        await asyncio.sleep(3600)

    revoked = []

    async def fake_revoke(hass_, section):
        revoked.append(section)

    monkeypatch.setattr(hass.config_entries.flow, "async_finish_flow", gated_finish)
    monkeypatch.setattr(pair_http.identity, "async_remove_identity", hung_remove)
    monkeypatch.setattr(pair_http.identity, "async_revoke_music_section", fake_revoke)
    monkeypatch.setattr(pair_http.identity, "async_create_music_section", lambda hass_, i: _coro({"url": "http://ma:8095", "token": "clock-token"}))
    started = asyncio.get_running_loop().time()
    assert (await post(hass, client, flow_id, code)).status == 409
    assert asyncio.get_running_loop().time() - started < 1.5
    assert revoked == [{"url": "http://ma:8095", "token": "clock-token"}], "the MA step ran although the user step hung"
    gate.set()


async def test_a_finish_completing_after_the_marker_ttl_still_cannot_keep_an_entry(hass, hass_client_no_auth, monkeypatch):
    from custom_components.helios import http as pair_http

    monkeypatch.setattr(pair_http, "FLOW_TIMEOUT_SECONDS", 0.2)
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    gate = asyncio.Event()
    real_finish = hass.config_entries.flow.async_finish_flow

    async def gated_finish(flow, result):
        await gate.wait()
        return await real_finish(flow, result)

    monkeypatch.setattr(hass.config_entries.flow, "async_finish_flow", gated_finish)
    assert (await post(hass, client, flow_id, code)).status == 409
    hass.data[DOMAIN]["cancelled_users"].clear()  # the TTL purge ran "an hour later"
    gate.set()  # ...and only now the flow manager finishes the entry
    await asyncio.sleep(0.2)
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == [], "an entry whose user is gone removes itself in async_setup_entry"


async def test_timeout_keeps_a_marker_until_the_finish_settles_and_stale_markers_are_purged(hass, hass_client_no_auth, monkeypatch):
    from custom_components.helios import http as pair_http

    monkeypatch.setattr(pair_http, "FLOW_TIMEOUT_SECONDS", 0.2)
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    gate = asyncio.Event()  # a controlled gate: the test decides when the manager may finish (a sleep would outlive the test)
    real_finish = hass.config_entries.flow.async_finish_flow

    async def gated_finish(flow, result):
        await gate.wait()
        return await real_finish(flow, result)

    monkeypatch.setattr(hass.config_entries.flow, "async_finish_flow", gated_finish)
    assert (await post(hass, client, flow_id, code)).status == 409
    markers = hass.data[DOMAIN]["cancelled_users"]
    assert len(markers) == 1 and hass.config_entries.async_entries(DOMAIN) == [], "the marker guards against a finish that may still complete"
    assert not [u for u in await hass.auth.async_get_users() if u.system_generated and u.name.startswith("Helios")]
    markers["stale-user"] = __import__("time").monotonic() - 3601
    assert (await post(hass, client, flow_id, "000000")).status == 401  # every POST purges aged markers first, the live one stays
    assert "stale-user" not in markers and len(markers) == 1
    gate.set()
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == [], "an aborted flow creates nothing; a finished one would remove itself in async_setup_entry"


async def test_concurrent_pairing_of_the_same_clock_is_409_without_claiming(hass, hass_client_no_auth):
    flow_id, code = await start_flow(hass)
    flow_id2, code2 = await start_flow(hass)
    client = await hass_client_no_auth()
    hass.data[DOMAIN]["pairing_active"].add(INSTALLATION)
    assert (await post(hass, client, flow_id2, code2)).status == 409
    assert hass.data[DOMAIN]["pairing"].pending(code2)
    hass.data[DOMAIN]["pairing_active"].discard(INSTALLATION)


async def test_removing_the_entry_removes_identity_and_closes_the_socket(hass, hass_client_no_auth, hass_ws_client):
    flow_id, code = await start_flow(hass)
    client = await hass_client_no_auth()
    body = await (await post(hass, client, flow_id, code)).json()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    ws = await hass_ws_client(hass, access_token=body["token"])
    await ws.send_json({"id": 1, "type": "helios/connect", "protocol": 2, "installation_id": INSTALLATION, "app_version": "0.9.0", "version_code": 28})
    assert (await ws.receive_json())["success"] is True
    assert (await ws.receive_json())["event"]["type"] == "connected"  # the live channel is what the removal must tear down
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.auth.async_get_user(entry.data["user_id"]) is None
    async with asyncio.timeout(3):
        while True:  # appearance, connection and removed may still be queued before the close
            msg = await ws.receive()
            if msg.type.name in ("CLOSE", "CLOSED", "CLOSING"):
                break
    await ws.close()
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(entry.entry_id) if hass.config_entries.async_get_entry(entry.entry_id) else None


async def test_legacy_entry_keeps_its_human_user(hass, hass_admin_user):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=INSTALLATION, data={"installation_id": INSTALLATION, "user_id": hass_admin_user.id, "app_version": "0.8.18", "version_code": 27})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    assert await hass.auth.async_get_user(hass_admin_user.id) is not None
