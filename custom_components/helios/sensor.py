"""Version, voice state and dock version sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeliosEntity

SENSORS = (  # the key doubles as the translation key (strings.json -> entity.sensor)
    ("app_version", EntityCategory.DIAGNOSTIC, "mdi:cellphone-arrow-down"),
    ("voice_state", None, "mdi:microphone"),
    ("pad_version", EntityCategory.DIAGNOSTIC, "mdi:chip"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN]["entries"][entry.entry_id]
    async_add_entities(HeliosSensor(coordinator, key, category, icon) for key, category, icon in SENSORS)


class HeliosSensor(HeliosEntity, SensorEntity):
    def __init__(self, coordinator, key, category, icon) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_entity_category = category
        self._attr_icon = icon

    @property
    def native_value(self):
        return self.value
