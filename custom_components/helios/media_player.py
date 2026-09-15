"""The clock's own output volume as a media_player, so Assist intents ("głośniej", "ustaw głośność na 30 %") can target it by area."""

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
    """Device volume only: not a music source. Music Assistant exposes its own player for playback."""

    _attr_name = "Głośnik zegara"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_supported_features = MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_STEP
    _attr_icon = "mdi:speaker"

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "volume_percent")

    @property
    def state(self) -> MediaPlayerState | None:
        if not self.available:
            return None
        voice = self.coordinator.value("voice_state")
        return MediaPlayerState.PLAYING if voice == "responding" else MediaPlayerState.IDLE

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
