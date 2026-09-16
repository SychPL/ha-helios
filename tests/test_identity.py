import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_SYSTEM
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios import identity

INSTALLATION = "0f3c1b2a-9d8e-4c7b-a6f5-1e2d3c4b5a69"


async def test_identity_is_a_local_system_user_with_a_ten_year_token(hass):
    user_id, token = await identity.async_create_identity(hass, INSTALLATION)
    user = await hass.auth.async_get_user(user_id)
    assert user.system_generated and user.local_only and not user.is_admin
    assert user.name == "Helios 0f3c1b2a" and [g.id for g in user.groups] == [GROUP_ID_USER]
    refresh = next(iter(user.refresh_tokens.values()))
    assert refresh.token_type == TOKEN_TYPE_SYSTEM and refresh.client_name == "Helios 0f3c1b2a"
    assert refresh.access_token_expiration.days == 3650
    assert hass.auth.async_validate_access_token(token).user.id == user_id  # @callback in 2026.8.3, not a coroutine


async def test_remove_identity_revokes_tokens_first_and_is_idempotent(hass):
    user_id, token = await identity.async_create_identity(hass, INSTALLATION)
    refresh = next(iter((await hass.auth.async_get_user(user_id)).refresh_tokens.values()))
    revoked = []
    hass.auth.async_register_revoke_token_callback(refresh.id, lambda: revoked.append(refresh.id))
    await identity.async_remove_identity(hass, user_id)
    assert revoked == [refresh.id], "the websocket revoke callback must fire (async_remove_user alone skips it)"
    assert await hass.auth.async_get_user(user_id) is None
    assert hass.auth.async_validate_access_token(token) is None
    await identity.async_remove_identity(hass, user_id)
    await identity.async_remove_identity(hass, None)


async def test_identity_creation_cleans_up_when_the_token_step_fails(hass, monkeypatch):
    async def boom(*a, **k):
        raise ValueError("no token")

    monkeypatch.setattr(hass.auth, "async_create_refresh_token", boom)
    with pytest.raises(ValueError):
        await identity.async_create_identity(hass, INSTALLATION)
    assert not [u for u in await hass.auth.async_get_users() if u.name == "Helios 0f3c1b2a"], "no half-made user survives"


async def test_remove_identity_never_touches_a_human_user(hass, hass_admin_user):
    await identity.async_remove_identity(hass, hass_admin_user.id)
    assert await hass.auth.async_get_user(hass_admin_user.id) is not None


def test_music_source_uses_the_first_loaded_entry_and_logs(hass, caplog):
    assert identity.music_source(hass) is None
    assert "Music Assistant" in caplog.text and "WARNING" in caplog.text
    caplog.clear()
    old = MockConfigEntry(domain="music_assistant", data={"url": "http://old:8095", "token": "old-token"})
    old.add_to_hass(hass)
    assert identity.music_source(hass) is None, "not loaded yet"
    new = MockConfigEntry(domain="music_assistant", data={"url": "http://new:8095", "token": "new-token"})
    new.add_to_hass(hass)
    new.mock_state(hass, ConfigEntryState.LOADED)
    assert identity.music_source(hass) == ("http://new:8095", "new-token"), "the first LOADED entry, not the first entry"
    old.mock_state(hass, ConfigEntryState.LOADED)
    caplog.clear()
    assert identity.music_source(hass) == ("http://old:8095", "old-token")
    assert "INFO" in caplog.text and "http://old:8095" in caplog.text and "old-token" not in caplog.text
    tokenless = MockConfigEntry(domain="music_assistant", data={"url": "http://x:8095"})
    for e in (old, new):
        e.mock_state(hass, ConfigEntryState.NOT_LOADED)
    tokenless.add_to_hass(hass)
    tokenless.mock_state(hass, ConfigEntryState.LOADED)
    caplog.clear()
    assert identity.music_source(hass) is None and "WARNING" in caplog.text


async def test_connection_payload_shape(hass):
    payload = identity.connection_payload(
        hass,
        {"installation_id": INSTALLATION, "music_assistant": {"url": "http://ma:8095", "token": "clock-token"}},
        {"diagnostics_url": "", "sendspin_url": ""},
    )
    assert payload == {
        "type": "connection",
        "pipeline": identity.preferred_pipeline(hass),
        "dashboard_path": "helios-clock",
        "music_assistant": {"url": "http://ma:8095", "token": "clock-token", "sendspin_url": "ws://ma:8927/sendspin"},
        "diagnostics_url": None,
    }
    payload = identity.connection_payload(hass, {"installation_id": INSTALLATION}, {"diagnostics_url": "http://pc:8757/x/events", "sendspin_url": "ws://other:8927/sendspin"})
    assert payload["music_assistant"] is None and payload["diagnostics_url"] == "http://pc:8757/x/events"


async def test_client_factory_matches_the_installed_music_assistant_client(hass):  # async: the aiohttp session needs a running loop
    from music_assistant_client import MusicAssistantClient

    client = identity._client(hass, "http://ma:8095", "ma-token")
    assert isinstance(client, MusicAssistantClient) and hasattr(client, "auth")
    assert hasattr(client, "__aenter__") and hasattr(client.auth, "create_token") and hasattr(client.auth, "logout")


async def test_music_section_is_created_and_revoked_through_the_ma_client(hass, monkeypatch):
    created, revoked = [], []

    class FakeAuth:
        def __init__(self, token):
            self.token = token

        async def create_token(self, name):
            created.append((self.token, name))
            return "clock-token"

        async def logout(self):
            revoked.append(self.token)

    class FakeClient:
        def __init__(self, server_url, aiohttp_session, token=None, ssl_context=None, locale=None):  # the 1.4.3 signature
            self.auth = FakeAuth(token)
            self.connected = False

        async def __aenter__(self):
            self.connected = True  # 1.4.3: __aenter__ connects; auth commands need the connection
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(identity, "_client", lambda hass, url, token: FakeClient(url, None, token))
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://ma:8095", "ma-token"))
    section = await identity.async_create_music_section(hass, INSTALLATION)
    assert section == {"url": "http://ma:8095", "token": "clock-token"}
    assert created[0][0] == "ma-token" and created[0][1].startswith("Helios 0f3c1b2a ") and len(created[0][1].split()[-1]) == 6
    await identity.async_revoke_music_section(hass, section)
    assert revoked == ["clock-token"]
    await identity.async_revoke_music_section(hass, None)


async def test_music_section_is_none_without_ma_or_on_errors(hass, monkeypatch):
    assert await identity.async_create_music_section(hass, INSTALLATION) is None
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://ma:8095", "ma-token"))

    class Broken:
        async def __aenter__(self):
            raise OSError("down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(identity, "_client", lambda hass, url, token: Broken())
    assert await identity.async_create_music_section(hass, INSTALLATION) is None


@pytest.mark.parametrize("failure", ["timeout", "cancel"])
async def test_ambiguous_create_token_revokes_only_this_attempt(hass, monkeypatch, failure):
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://ma:8095", "ma-token"))
    tokens, revoked = [SimpleNamespace(token_id="t-foreign", name="Helios 0f3c1b2a a1b2c3")], []

    class Auth:
        async def create_token(self, name):
            tokens.append(SimpleNamespace(token_id="t-ours", name=name))  # the server acted...
            if failure == "timeout":
                raise TimeoutError()  # ...but the answer never came
            raise asyncio.CancelledError()  # ...and the surrounding transaction was cancelled

        async def get_tokens(self):
            return list(tokens)

        async def revoke_token(self, token_id):
            revoked.append(token_id)

    class Client:
        auth = Auth()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(identity, "_client", lambda hass, url, token: Client())
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await identity.async_create_music_section(hass, INSTALLATION)
    else:
        assert await identity.async_create_music_section(hass, INSTALLATION) is None
    assert revoked == ["t-ours"], "the foreign token with the same prefix stays"
