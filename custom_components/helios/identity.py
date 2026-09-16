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

from .const import DASHBOARD_PATH, MA_TIMEOUT_SECONDS, sendspin_url_for

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


def _client(hass: HomeAssistant, url: str, token: str):
    from music_assistant_client import MusicAssistantClient  # noqa: PLC0415 - optional: installed through after_dependencies

    return MusicAssistantClient(url, async_get_clientsession(hass), token)  # TLS verified like everywhere else (SPEC 0.10 pkt 9)


async def async_create_music_section(hass: HomeAssistant, installation_id: str) -> dict | None:
    """A clock-specific MA token minted with the core integration's token; None (with one warning) when MA is absent or fails."""
    source = music_source(hass)
    if source is None:
        return None
    url, token = source
    name = f"{_label(installation_id)} {secrets.token_hex(3)}"
    sent = False
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, url, token) as client:
            sent = True  # from here on the server may have acted even if we never see the answer
            clock_token = await client.auth.create_token(name)
        return {"url": url, "token": clock_token}
    except ImportError:
        _LOGGER.warning("music_assistant_client nie jest zainstalowany - zegar bez muzyki")
    except (Exception, asyncio.CancelledError) as err:  # CancelledError: the pairing transaction timed out around us
        _LOGGER.warning("Nie udało się utworzyć tokena MA dla %s: %s", _label(installation_id), type(err).__name__)
        if sent:
            await _revoke_by_name(hass, url, token, name)  # bounded (MA_TIMEOUT_SECONDS), safe after a delivered cancel
        if isinstance(err, asyncio.CancelledError):
            raise
    return None


async def _revoke_by_name(hass: HomeAssistant, url: str, token: str, name: str) -> None:
    """After an ambiguous create_token (timeout after the server acted) the token of this attempt is found by its unique name and revoked."""
    try:
        async with asyncio.timeout(MA_TIMEOUT_SECONDS), _client(hass, url, token) as client:
            for item in await client.auth.get_tokens():
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
        music = {"url": section["url"], "token": section["token"], "sendspin_url": options.get("sendspin_url") or sendspin_url_for(section["url"])}
    return {"type": "connection", "pipeline": preferred_pipeline(hass), "dashboard_path": DASHBOARD_PATH, "music_assistant": music, "diagnostics_url": options.get("diagnostics_url") or None}
