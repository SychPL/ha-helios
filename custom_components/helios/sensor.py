"""Version, voice state and dock version sensors."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import LIGHT_LUX, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HeliosEntity

# key (doubles as the translation key: strings.json -> entity.sensor), category, icon, device class, unit
SENSORS = (
    ("app_version", EntityCategory.DIAGNOSTIC, "mdi:cellphone-arrow-down", None, None),
    ("voice_state", None, "mdi:microphone", None, None),
    ("pad_version", EntityCategory.DIAGNOSTIC, "mdi:chip", None, None),
    # The clock already reads the room's light level for its own night mode; sharing the number costs nothing
    # and turns the clock into a light sensor for the rest of the house.
    ("lux", None, None, SensorDeviceClass.ILLUMINANCE, LIGHT_LUX),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = hass.data[DOMAIN]["entries"][entry.entry_id]
    async_add_entities(HeliosSensor(coordinator, *sensor) for sensor in SENSORS)


class HeliosSensor(HeliosEntity, SensorEntity):
    def __init__(self, coordinator, key, category, icon, device_class=None, unit=None) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._attr_entity_category = category
        self._attr_icon = icon
        if device_class is not None:
            self._attr_device_class = device_class
            self._attr_native_unit_of_measurement = unit
            self._attr_state_class = SensorStateClass.MEASUREMENT  # gives it long-term statistics

    @property
    def native_value(self):
        return self.value
