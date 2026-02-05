"""Button platform for Zyxel integration - Optimized for NWA50AX V7.10."""
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zyxel buttons from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]
    
    buttons = [
        ZyxelRebootButton(coordinator, api, config_entry),
    ]
    
    async_add_entities(buttons)


class ZyxelRebootButton(CoordinatorEntity, ButtonEntity):
    """Button to reboot the Zyxel device."""

    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"
    _attr_has_entity_name = True

    def __init__(self, coordinator, api, config_entry: ConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_reboot"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        device_data = self.coordinator.data.get("device_info", {})
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": f"Zyxel {device_data.get('model', 'NWA50AX')}",
            "manufacturer": "Zyxel",
            "model": device_data.get("model", "NWA50AX"),
            "sw_version": device_data.get("firmware", "Unknown"),
        }

    async def async_press(self) -> None:
        """Handle the button press."""
        _LOGGER.info("Rebooting Zyxel device")
        try:
            success = await self._api.async_reboot()
            if success:
                _LOGGER.info("Reboot command sent successfully")
            else:
                _LOGGER.error("Failed to send reboot command")
        except Exception as err:
            _LOGGER.error("Error rebooting device: %s", err)
