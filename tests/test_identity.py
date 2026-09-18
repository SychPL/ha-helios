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
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://d5369777-music-assistant:8094", "ma-token"))
    monkeypatch.setattr(identity, "async_public_music_url", lambda hass, url: _url("http://192.168.1.212:8095"))
    section = await identity.async_create_music_section(hass, INSTALLATION)
    # the clock gets the address the server reports, HA keeps the entry url for its own calls (SPEC 0.10 pkt 6.1)
    assert section == {"url": "http://192.168.1.212:8095", "source_url": "http://d5369777-music-assistant:8094", "token": "clock-token"}
    assert created[0][0] == "ma-token" and created[0][1].startswith("Helios 0f3c1b2a ") and len(created[0][1].split()[-1]) == 6
    await identity.async_revoke_music_section(hass, section)
    assert revoked == ["clock-token"]
    await identity.async_revoke_music_section(hass, None)


async def _url(value):
    return value


async def test_public_music_url_prefers_what_the_server_reports(hass, monkeypatch):
    from types import SimpleNamespace

    async def info(url, aiohttp_session=None, ssl_context=None):
        return SimpleNamespace(base_url="http://192.168.1.212:8095/", internal_url="http://x", external_url=None)

    import music_assistant_client.auth_helpers as helpers

    monkeypatch.setattr(helpers, "get_server_info", info)
    assert await identity.async_public_music_url(hass, "http://d5369777-music-assistant:8094") == "http://192.168.1.212:8095"

    async def broken(url, aiohttp_session=None, ssl_context=None):
        raise OSError("down")

    monkeypatch.setattr(helpers, "get_server_info", broken)
    assert await identity.async_public_music_url(hass, "http://ma:8095") == "http://ma:8095", "the stored url is the fallback"


async def test_connection_payload_honours_the_music_url_override(hass):
    data = {"installation_id": INSTALLATION, "music_assistant": {"url": "http://d5369777-music-assistant:8094", "source_url": "http://d5369777-music-assistant:8094", "token": "clock-token"}}
    auto = identity.connection_payload(hass, data, {})
    assert auto["music_assistant"]["url"] == "http://d5369777-music-assistant:8094"
    assert auto["music_assistant"]["sendspin_url"] == "ws://d5369777-music-assistant:8927/sendspin"
    override = identity.connection_payload(hass, data, {"music_url": "http://192.168.1.212:8095/"})
    assert override["music_assistant"]["url"] == "http://192.168.1.212:8095"
    assert override["music_assistant"]["sendspin_url"] == "ws://192.168.1.212:8927/sendspin"


class _FakeAuth:
    """Mirrors the pieces of music_assistant_client 1.4.3 the integration uses."""

    def __init__(self, log, users, refuse_token=None, token="clock-token"):
        self.log, self.users, self.refuse_token, self.token = log, users, refuse_token, token
        self.tokens = []

    async def list_users(self):
        if self.users is None:
            raise RuntimeError("not an admin")
        return self.users

    async def create_token(self, name, user_id=None):
        self.log.append(("create", name, user_id))
        self.tokens.append(SimpleNamespace(token_id="t1", name=name))
        return self.token

    async def get_tokens(self, user_id=None):
        return list(self.tokens)

    async def revoke_token(self, token_id):
        self.log.append(("revoke_token", token_id))

    async def get_current_user(self):
        if self.refuse_token:
            raise self.refuse_token
        return SimpleNamespace(username="mateusz")

    async def logout(self):
        self.log.append(("logout", self.token))


def _fake_client(log, users, refuse_token=None):
    class Client:
        def __init__(self, url, token):
            self.url, self.auth = url, _FakeAuth(log, users, refuse_token)

        async def __aenter__(self):
            log.append(("connect", self.url))
            return self

        async def __aexit__(self, *a):
            return False

    return lambda hass, url, token: Client(url, token)


async def test_the_clock_token_is_minted_for_a_regular_user_not_the_ha_system_user(hass, monkeypatch):
    """MA refuses Home Assistant system-user tokens on its LAN webserver, so the token must belong to a person (MA 2.10.3)."""
    log = []
    users = [SimpleNamespace(user_id="sys", role="system"), SimpleNamespace(user_id="u-mateusz", role="admin")]
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://d5369777-music-assistant:8094", "ma-token"))
    monkeypatch.setattr(identity, "async_public_music_url", lambda hass, url: _url("http://192.168.1.212:8095"))
    monkeypatch.setattr(identity, "_client", _fake_client(log, users))
    section = await identity.async_create_music_section(hass, INSTALLATION)
    assert section["url"] == "http://192.168.1.212:8095" and section["token"] == "clock-token"
    assert ("create", section and log[1][1], "u-mateusz") in log, log
    assert ("connect", "http://192.168.1.212:8095") in log, "the token is verified from the clock's address"


async def test_a_refused_token_is_revoked_and_leaves_no_section(hass, monkeypatch, caplog):
    from music_assistant_models.errors import AuthenticationFailed

    log = []
    monkeypatch.setattr(identity, "music_source", lambda hass: ("http://ma:8094", "ma-token"))
    monkeypatch.setattr(identity, "async_public_music_url", lambda hass, url: _url("http://192.168.1.212:8095"))
    monkeypatch.setattr(identity, "_client", _fake_client(log, None, AuthenticationFailed("Home Assistant system user not allowed on regular webserver")))
    assert await identity.async_create_music_section(hass, INSTALLATION) is None
    assert ("create", log[1][1], None) in log, "no user list: mint for whoever we are"
    assert any(step[0] == "logout" for step in log), "a token the clock cannot use is revoked"
    assert "odrzuca token zegara" in caplog.text


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
