"""Per-clock HA identity (system user + system token) and the clock's Music Assistant token (SPEC 0.10 pkt 5, 6)."""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import timedelta

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_SYSTEM
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MA_TIMEOUT_SECONDS, MUSIC_SECTION_REVISION, dashboard_path_for, sendspin_url_for

_LOGGER = logging.getLogger(__name__)
TOKEN_LIFETIME = timedelta(days=3650)


def _label(installation_id: str) -> str:
    return f"Helios {installation_id[:8]}"


async def async_create_identity(hass: HomeAssistant, installation_id: str) -> tuple[str, str]:
    """A non-admin, LAN-only system user with one never-expiring system refresh token; returns (user_id, access token JWT)."""
    user = await hass.auth.async_create_system_user(_label(installation_id), group_ids=[GROUP_ID_USER], local_only=True)
    try:
        refresh = await hass.auth.async_create_refresh_token(
            user, client_name=_label(installation_id), token_type=TOKEN_TYPE_SYSTEM, access_token_expiration=TOKEN_LIFETIME
        )
        return user.id, hass.auth.async_create_access_token(refresh)
    except BaseException:
        await hass.auth.async_remove_user(user)  # never leave a half-made identity behind
        raise


async def async_remove_identity(hass: HomeAssistant, user_id: str | None) -> None:
    """Removes the clock's system user; refresh tokens first so open websockets are closed (async_remove_user skips those callbacks). Never a human user."""
    if not user_id:
        return
    user = await hass.auth.async_get_user(user_id)
    if user is None or not user.system_generated:
        return
    for token in list(user.refresh_tokens.values()):
        hass.auth.async_remove_refresh_token(token)
    await hass.auth.async_remove_user(user)


def music_source(hass: HomeAssistant) -> tuple[str, str] | None:
    """url and token of the first LOADED core music_assistant entry (SPEC 0.10 pkt 6.1); None with one warning when there is none."""
    loaded = [e for e in hass.config_entries.async_entries("music_assistant") if e.state is ConfigEntryState.LOADED]
    if not loaded:
        _LOGGER.warning("No loaded Music Assistant integration - the clock gets no music")
        return None
    entry = loaded[0]
    if len(loaded) > 1:
        _LOGGER.info("Several Music Assistant entries, Helios uses %s", entry.data.get("url"))
    if not (entry.data.get("url") and entry.data.get("token")):
        _LOGGER.warning("Wpis Music Assistant bez tokena (schemat < 28) - zegar bez muzyki")
        return None
    return entry.data["url"], entry.data["token"]


async def async_public_music_url(hass: HomeAssistant, url: str) -> str:
    """The address the clock can reach (SPEC 0.10 pkt 6.1).

    The core entry of an add-on install stores the supervisor-internal hostname (http://<slug>:8094), which no device on
    the LAN can resolve; the server itself knows its LAN address, so ask it and fall back to the stored url.
    """
    try:
        from music_assistant_client.auth_helpers import get_server_info  # noqa: PLC0415

        async with asyncio.timeout(MA_TIMEOUT_SECONDS):
            info = await get_server_info(url, aiohttp_session=async_get_clientsession(hass))
        public = getattr(info, "base_url", None) or getattr(info, "internal_url", None)
        if public:
            return str(public).rstrip("/")
        _LOGGER.warning("Music Assistant did not report its own address - the clock gets %s", url)
    except Exception as err:  # noqa: BLE001 - the stored url is the fallback
        _LOGGER.warning("Could not read the Music Assistant address (%s): %s", url, type(err).__name__)
    return url


def _client(hass: HomeAssistant, url: str, token: str):
    from music_assistant_client import MusicAssistantClient  # noqa: PLC0415 - optional: installed through after_dependencies

    return MusicAssistantClient(url, async_get_clientsession(hass), token)  # TLS verified like everywhere else (SPEC 0.10 pkt 9)


def _is_system(user) -> bool:
    return "system" in str(getattr(user, "role", "")).lower()


async def _clock_user(client) -> str | None:
    """A regular MA account for the clock's token.

    An add-on install authenticates as MA's Home Assistant system user, and MA refuses system-user tokens on its LAN
    webserver ("Home Assistant system user not allowed on regular webserver", MA 2.10.3), so the token has to be minted
    for a real user. None = mint for whoever we are (a non add-on install already authenticates as a person).
    """
    try:
        users = [u for u in await client.auth.list_users() if not _is_system(u)]
    except Exception as err:  # noqa: BLE001 - not an admin, or an MA without user management
        _LOGGER.debug("Could not list Music Assistant users: %s", type(err).__name__)
        return None
    if not users:
        return None
    chosen = next((u for u in users if "admin" in str(getattr(u, "role", "")).lower()), users[0])
    return getattr(chosen, "user_id", None)


async def async_create_music_section(hass: HomeAssistant, installation_id: str, options: dict | None = None) -> dict | None:
    """The clock's Music Assistant access: the token pasted in the options, or one minted with the core entry's token.

    None (with one warning) when MA is absent or when MA refuses to mint a token the clock could use.
    """
    options = options or {}
    manual = (options.get("music_token") or "").strip()
    source = music_source(hass) if not manual else quiet_music_source(hass)
    override = (options.get("music_url") or "").strip()
    if manual:
        public = override or (await async_public_music_url(hass, source[0]) if source else "")
        if not public:
            _LOGGER.warning("A Music Assistant token is set but the server address is unknown - fill it in the integration options")
            return None
        return {"url": public, "source_url": source[0] if source else "", "token": manual, "minted": MUSIC_SECTION_REVISION, "manual": True}
    if source is None:
        return None
    url, token = source
    public = override or await async_public_music_url(hass, url)  # the clock talks to this one, HA keeps using the entry's url
    name = f"{_label(installation_id)} {secrets.token_hex(3)}"
    sent = False
    user_id = None
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, url, token) as client:
            user_id = await _clock_user(client)
            sent = True  # from here on the server may have acted even if we never see the answer
            clock_token = await client.auth.create_token(name, user_id=user_id) if user_id else await client.auth.create_token(name)
    except ImportError:
        _LOGGER.warning("music_assistant_client nie jest zainstalowany - zegar bez muzyki")
        return None
    except (Exception, asyncio.CancelledError) as err:  # CancelledError: the pairing transaction timed out around us
        if type(err).__name__ == "InsufficientPermissions":
            # MA add-on: Home Assistant authenticates as a system user, which may neither mint a token for a person nor
            # use its own token on the LAN webserver. Nothing the integration can do - the user pastes a token once.
            _LOGGER.warning(
                "Music Assistant will not let the integration mint a token for the clock (the add-on sees Home Assistant as a system user). "
                "Go to Settings -> Devices and services -> Helios -> Configure and paste a Music Assistant token (Music Assistant -> Settings -> Tokens)."
            )
        else:
            _LOGGER.warning("Could not create a Music Assistant token for %s: %s", _label(installation_id), type(err).__name__)
        if sent:
            await _revoke_by_name(hass, url, token, name, user_id)  # bounded (MA_TIMEOUT_SECONDS), safe after a delivered cancel
        if isinstance(err, asyncio.CancelledError):
            raise
        return None
    section = {"url": public, "source_url": url, "token": clock_token, "minted": MUSIC_SECTION_REVISION}
    refused = await _token_refused(hass, section)
    if refused:
        _LOGGER.warning("Music Assistant refuses the clock's token at %s (%s) - the clock gets no music", public, refused)
        await async_revoke_music_section(hass, section)
        return None
    return section


async def _token_refused(hass: HomeAssistant, section: dict) -> str | None:
    """Connects to the clock's address exactly as the clock will; returns a reason only when MA rejects the token itself.

    A connection error says nothing about the clock (HA may simply not reach that address), so the section survives it.
    """
    from music_assistant_models.errors import AuthenticationFailed, AuthenticationRequired, InvalidToken  # noqa: PLC0415

    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, section["url"], section["token"]) as client:
            await client.auth.get_current_user()
    except (AuthenticationFailed, AuthenticationRequired, InvalidToken) as err:
        return str(err)[:120]
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not verify the Music Assistant token at %s: %s", section["url"], type(err).__name__)
    return None


async def _revoke_by_name(hass: HomeAssistant, url: str, token: str, name: str, user_id: str | None = None) -> None:
    """After an ambiguous create_token (timeout after the server acted) the token of this attempt is found by its unique name and revoked."""
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, url, token) as client:
            items = await client.auth.get_tokens(user_id) if user_id else await client.auth.get_tokens()
            for item in items:
                if item.name == name:
                    await client.auth.revoke_token(item.token_id)
    except Exception as err:  # noqa: BLE001 - best effort; the token may simply never have been created
        _LOGGER.debug("Could not revoke the Music Assistant token named %s: %s", name, type(err).__name__)


def quiet_music_source(hass: HomeAssistant) -> tuple[str, str] | None:
    """music_source without the warning: with a pasted token the core entry is only a source of the address."""
    for entry in hass.config_entries.async_entries("music_assistant"):
        if entry.state is ConfigEntryState.LOADED and entry.data.get("url"):
            return entry.data["url"], entry.data.get("token") or ""
    return None


async def async_revoke_music_section(hass: HomeAssistant, section: dict | None) -> None:
    """logout() with the clock's own token revokes exactly that token; best effort. A token the user pasted is never revoked."""
    if not section or not section.get("token") or section.get("manual"):
        return
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, section["url"], section["token"]) as client:
            await client.auth.logout()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not revoke the clock's Music Assistant token: %s", type(err).__name__)


MUSIC_OPTION_KEYS = ("music_token", "music_url", "sendspin_url")


def inherited_music_options(hass: HomeAssistant) -> dict:
    """The Music Assistant options of a clock that has a pasted token, for a clock being paired now.

    With the MA add-on the integration cannot mint a clock token at all (SPEC 0.10 pkt 6.2), so without this every
    further clock paired silently without music. The pasted token is the user's own and is never revoked, so sharing
    it between clocks changes nothing about its lifetime.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if (entry.options.get("music_token") or "").strip():
            return {key: entry.options.get(key, "") for key in MUSIC_OPTION_KEYS}
    return {}


def preferred_pipeline(hass: HomeAssistant) -> str | None:
    from homeassistant.components.assist_pipeline import async_get_pipeline  # noqa: PLC0415

    try:
        return async_get_pipeline(hass).id
    except Exception:  # noqa: BLE001 - no preferred pipeline / assist_pipeline not ready
        return None


def connection_payload(hass: HomeAssistant, entry_data: dict, options: dict) -> dict:
    section = entry_data.get("music_assistant")
    music = None
    if section:
        url = (options.get("music_url") or section["url"]).rstrip("/")  # the option wins when the server reports an address the clock cannot use
        music = {"url": url, "token": section["token"], "sendspin_url": options.get("sendspin_url") or sendspin_url_for(url)}
    return {"type": "connection", "pipeline": preferred_pipeline(hass), "dashboard_path": dashboard_path_for(entry_data), "music_assistant": music, "diagnostics_url": options.get("diagnostics_url") or None}
