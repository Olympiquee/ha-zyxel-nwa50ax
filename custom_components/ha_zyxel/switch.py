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
        ZyxelRadio24GSwitch(coordinator, api, config_entry),
        ZyxelRadio5GSwitch(coordinator, api, config_entry),
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


class ZyxelRadio24GSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable 2.4GHz radio."""

    _attr_name = "Radio 2.4GHz"
    _attr_icon = "mdi:radio-tower"
    _attr_has_entity_name = True

    def __init__(self, coordinator, api, config_entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._attr_is_on = True  # On suppose qu'elle est active par défaut

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_radio_24g"

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
        """Return true if 2.4GHz radio is active."""
        radio = self.coordinator.data.get("radio", {})
        return radio.get("slot1_active", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the 2.4GHz radio on."""
        _LOGGER.info("Activating 2.4GHz radio")
        try:
            success = await self._api.async_toggle_radio(slot=1, enable=True)
            if success:
                await self.coordinator.async_request_refresh()
                _LOGGER.info("2.4GHz radio activated successfully")
            else:
                _LOGGER.error("Failed to activate 2.4GHz radio")
        except Exception as err:
            _LOGGER.error("Error activating 2.4GHz radio: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the 2.4GHz radio off."""
        _LOGGER.info("Deactivating 2.4GHz radio")
        try:
            success = await self._api.async_toggle_radio(slot=1, enable=False)
            if success:
                await self.coordinator.async_request_refresh()
                _LOGGER.info("2.4GHz radio deactivated successfully")
            else:
                _LOGGER.error("Failed to deactivate 2.4GHz radio")
        except Exception as err:
            _LOGGER.error("Error deactivating 2.4GHz radio: %s", err)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        radio = self.coordinator.data.get("radio", {})
        return {
            "band": radio.get("slot1_band", "Unknown"),
            "ssids": ", ".join(radio.get("slot1_ssids", [])),
            "description": "Contrôle la radio WiFi 2.4GHz (slot1)",
        }


class ZyxelRadio5GSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable 5GHz radio."""

    _attr_name = "Radio 5GHz"
    _attr_icon = "mdi:radio-tower"
    _attr_has_entity_name = True

    def __init__(self, coordinator, api, config_entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._attr_is_on = True  # On suppose qu'elle est active par défaut

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_radio_5g"

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
        """Return true if 5GHz radio is active."""
        radio = self.coordinator.data.get("radio", {})
        return radio.get("slot2_active", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the 5GHz radio on."""
        _LOGGER.info("Activating 5GHz radio")
        try:
            success = await self._api.async_toggle_radio(slot=2, enable=True)
            if success:
                await self.coordinator.async_request_refresh()
                _LOGGER.info("5GHz radio activated successfully")
            else:
                _LOGGER.error("Failed to activate 5GHz radio")
        except Exception as err:
            _LOGGER.error("Error activating 5GHz radio: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the 5GHz radio off."""
        _LOGGER.info("Deactivating 5GHz radio")
        try:
            success = await self._api.async_toggle_radio(slot=2, enable=False)
            if success:
                await self.coordinator.async_request_refresh()
                _LOGGER.info("5GHz radio deactivated successfully")
            else:
                _LOGGER.error("Failed to deactivate 5GHz radio")
        except Exception as err:
            _LOGGER.error("Error deactivating 5GHz radio: %s", err)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        radio = self.coordinator.data.get("radio", {})
        return {
            "band": radio.get("slot2_band", "Unknown"),
            "ssids": ", ".join(radio.get("slot2_ssids", [])),
            "description": "Contrôle la radio WiFi 5GHz (slot2)",
        }
