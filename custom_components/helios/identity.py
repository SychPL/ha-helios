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

from .const import DASHBOARD_PATH, MA_TIMEOUT_SECONDS, MUSIC_SECTION_REVISION, sendspin_url_for

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
        _LOGGER.warning("Brak załadowanej integracji Music Assistant - zegar bez muzyki")
        return None
    entry = loaded[0]
    if len(loaded) > 1:
        _LOGGER.info("Kilka wpisów Music Assistant, Helios używa %s", entry.data.get("url"))
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
        _LOGGER.warning("Music Assistant nie podał swojego adresu - zegar dostanie %s", url)
    except Exception as err:  # noqa: BLE001 - the stored url is the fallback
        _LOGGER.warning("Nie udało się odczytać adresu Music Assistant (%s): %s", url, type(err).__name__)
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
        _LOGGER.debug("Nie udało się pobrać użytkowników MA: %s", type(err).__name__)
        return None
    if not users:
        return None
    chosen = next((u for u in users if "admin" in str(getattr(u, "role", "")).lower()), users[0])
    return getattr(chosen, "user_id", None)


async def async_create_music_section(hass: HomeAssistant, installation_id: str) -> dict | None:
    """A clock-specific MA token minted with the core integration's token; None (with one warning) when MA is absent or fails."""
    source = music_source(hass)
    if source is None:
        return None
    url, token = source
    public = await async_public_music_url(hass, url)  # the clock talks to this one, HA keeps using the entry's url
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
        _LOGGER.warning("Nie udało się utworzyć tokena MA dla %s: %s", _label(installation_id), type(err).__name__)
        if sent:
            await _revoke_by_name(hass, url, token, name, user_id)  # bounded (MA_TIMEOUT_SECONDS), safe after a delivered cancel
        if isinstance(err, asyncio.CancelledError):
            raise
        return None
    section = {"url": public, "source_url": url, "token": clock_token, "minted": MUSIC_SECTION_REVISION}
    refused = await _token_refused(hass, section)
    if refused:
        _LOGGER.warning("Music Assistant odrzuca token zegara pod adresem %s (%s) - zegar bez muzyki", public, refused)
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
        _LOGGER.debug("Nie udało się sprawdzić tokena MA pod %s: %s", section["url"], type(err).__name__)
    return None


async def _revoke_by_name(hass: HomeAssistant, url: str, token: str, name: str, user_id: str | None = None) -> None:
    """After an ambiguous create_token (timeout after the server acted) the token of this attempt is found by its unique name and revoked."""
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, url, token) as client:
            items = await client.auth.get_tokens(user_id) if user_id else await client.auth.get_tokens()
            for item in items:
                if item.name == name:
                    await client.auth.revoke_token(item.token_id)
    except Exception:  # noqa: BLE001
        pass


async def async_revoke_music_section(hass: HomeAssistant, section: dict | None) -> None:
    """logout() with the clock's own token revokes exactly that token; best effort."""
    if not section or not section.get("token"):
        return
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, section["url"], section["token"]) as client:
            await client.auth.logout()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Nie udało się unieważnić tokena MA zegara: %s", type(err).__name__)


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
    return {"type": "connection", "pipeline": preferred_pipeline(hass), "dashboard_path": DASHBOARD_PATH, "music_assistant": music, "diagnostics_url": options.get("diagnostics_url") or None}
