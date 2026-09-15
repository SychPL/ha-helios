"""The clock as a media_player: its own output volume plus play/pause/stop of the local music session, so Assist intents ("głośniej", "wyłącz muzykę") can target it by area."""

from __future__ import annotations

from homeassistant.components.media_player import MediaPlayerDeviceClass, MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeliosEntity

VOLUME_STEP = 0.1


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([HeliosSpeaker(hass.data[DOMAIN]["entries"][entry.entry_id])])


class HeliosSpeaker(HeliosEntity, MediaPlayerEntity):
    """Device volume plus transport of the clock's local session (0.8.7+). Not a music source: Music Assistant exposes its own player for browsing."""

    _attr_name = "Głośnik zegara"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.TURN_OFF
    )
    _attr_icon = "mdi:speaker"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "volume_percent")

    @property
    def state(self) -> MediaPlayerState | None:
        if not self.available:
            return None
        music = self.coordinator.value("music_state")
        if music == "playing":
            return MediaPlayerState.PLAYING
        if music == "paused":
            return MediaPlayerState.PAUSED
        voice = self.coordinator.value("voice_state")
        return MediaPlayerState.PLAYING if voice == "responding" else MediaPlayerState.IDLE

    async def async_media_play(self) -> None:
        await self.coordinator.async_command("music.play", {})

    async def async_media_pause(self) -> None:
        await self.coordinator.async_command("music.pause", {})

    async def async_media_stop(self) -> None:
        await self.coordinator.async_command("music.stop", {})

    async def async_turn_off(self) -> None:
        """"Wyłącz muzykę" lands here through the Assist media intents: stop the local session, the clock itself stays on."""
        await self.coordinator.async_command("music.stop", {})

    @property
    def volume_level(self) -> float | None:
        percent = self.value
        return None if percent is None else max(0.0, min(1.0, int(percent) / 100))

    async def async_set_volume_level(self, volume: float) -> None:
        await self.coordinator.async_command("audio.set_device_volume", {"percent": int(round(max(0.0, min(1.0, volume)) * 100))})

    async def async_volume_up(self) -> None:
        await self.async_set_volume_level((self.volume_level or 0.0) + VOLUME_STEP)

    async def async_volume_down(self) -> None:
        await self.async_set_volume_level((self.volume_level or 0.0) - VOLUME_STEP)
