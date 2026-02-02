"""Switch platform for Zyxel integration - Guest SSID control."""
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up Zyxel switches from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]
    
    switches = [
        ZyxelGuestSSIDSwitch(coordinator, api, config_entry),
    ]
    
    async_add_entities(switches)


class ZyxelGuestSSIDSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable Guest SSID."""

    _attr_name = "Guest SSID"
    _attr_icon = "mdi:wifi"
    _attr_has_entity_name = True

    def __init__(self, coordinator, api, config_entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._attr_is_on = False

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_guest_ssid"

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

    @property
    def is_on(self) -> bool:
        """Return true if Guest SSID is enabled (schedule disabled)."""
        # On considère que le SSID est "ON" si le schedule est désactivé
        # (c'est-à-dire que le SSID est toujours actif)
        # Note: On ne peut pas vraiment détecter l'état depuis les commandes SSH
        # donc on garde un état interne qui persiste via l'attribut
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the Guest SSID on (disable schedule = always active)."""
        _LOGGER.info("Enabling Guest SSID (disabling schedule)")
        try:
            success = await self._api.async_toggle_guest_ssid(enable=True)
            if success:
                self._attr_is_on = True
                self.async_write_ha_state()
                _LOGGER.info("Guest SSID enabled successfully")
            else:
                _LOGGER.error("Failed to enable Guest SSID")
        except Exception as err:
            _LOGGER.error("Error enabling Guest SSID: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the Guest SSID off (enable schedule = follow configured hours)."""
        _LOGGER.info("Disabling Guest SSID (enabling schedule)")
        try:
            success = await self._api.async_toggle_guest_ssid(enable=False)
            if success:
                self._attr_is_on = False
                self.async_write_ha_state()
                _LOGGER.info("Guest SSID disabled successfully (following schedule)")
            else:
                _LOGGER.error("Failed to disable Guest SSID")
        except Exception as err:
            _LOGGER.error("Error disabling Guest SSID: %s", err)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "description": "ON = SSID toujours actif | OFF = Suit le planning configuré",
            "schedule_info": "Quand OFF, le SSID Guest suit le planning défini dans l'interface web",
        }
