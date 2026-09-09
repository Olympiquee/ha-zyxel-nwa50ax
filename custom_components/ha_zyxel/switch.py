"""Switch platform for Zyxel integration - Guest SSID, schedules SSID and Radio control."""
import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DATA_ITEM_RADIO, DATA_ITEM_SSID_SCHEDULES, DOMAIN
from .entity_helpers import build_device_info
from .zyxel_ssh_api import ZyxelSSHAPI

_LOGGER = logging.getLogger(__name__)
_radio_locks: dict[str, asyncio.Lock] = {}


class ZyxelSSIDScheduleSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control a (non-Guest) SSID schedule (enable/disable auto on/off).

    Rattaché au coordinator "lent" (le planning d'un SSID change rarement),
    mais après une action manuelle, l'état vérifié est poussé immédiatement
    via `async_set_updated_data` - pas besoin d'attendre le prochain cycle
    lent pour voir le vrai état confirmé.
    """

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api: ZyxelSSHAPI,
        config_entry: ConfigEntry,
        ssid_name: str,
    ) -> None:
        """Initialize the SSID schedule switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._ssid_name = ssid_name
        self._attr_unique_id = f"zyxel_{config_entry.entry_id}_ssid_schedule_{ssid_name.lower()}"
        self._attr_name = f"SSID {ssid_name} Schedule"
        self._attr_icon = "mdi:calendar-clock"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """Return true if SSID schedule is enabled. None si pas encore connu."""
        return self.coordinator.data.get("ssid_schedules", {}).get(self._ssid_name)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        return {
            "ssid_name": self._ssid_name,
            "description": f"Contrôle le schedule du SSID {self._ssid_name}",
            "note": "ON = Schedule actif (auto on/off), OFF = Always-on (24/7)",
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable SSID schedule (auto on/off)."""
        _LOGGER.info("Enabling schedule for SSID %s", self._ssid_name)
        success = await self._api.async_toggle_ssid_schedule(self._ssid_name, enable=True)
        if success:
            self._push_confirmed_state(True)
            _LOGGER.info("SSID %s schedule enabled successfully", self._ssid_name)
        else:
            _LOGGER.error("Failed to enable schedule for SSID %s", self._ssid_name)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable SSID schedule (always-on)."""
        _LOGGER.info("Disabling schedule for SSID %s (always-on)", self._ssid_name)
        success = await self._api.async_toggle_ssid_schedule(self._ssid_name, enable=False)
        if success:
            self._push_confirmed_state(False)
            _LOGGER.info("SSID %s schedule disabled successfully (always-on)", self._ssid_name)
        else:
            _LOGGER.error("Failed to disable schedule for SSID %s", self._ssid_name)

    def _push_confirmed_state(self, state: bool) -> None:
        """Injecte l'état confirmé dans le coordinator lent, sans attendre son cycle.

        `async_set_updated_data` (plutôt qu'une simple mutation + write_ha_state)
        notifie aussi les autres entités qui liraient éventuellement la même
        clé, et réarme le minuteur du coordinator lent à partir de maintenant.
        """
        self.coordinator.data.setdefault("ssid_schedules", {})[self._ssid_name] = state
        self.coordinator.async_set_updated_data(self.coordinator.data)


class ZyxelGuestSSIDSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable Guest SSID (toujours actif vs planning).

    Réutilise le même mécanisme générique que ZyxelSSIDScheduleSwitch
    (async_toggle_ssid_schedule), avec persistance NVRAM (persist=True) pour
    conserver le comportement historique de cette intégration sur le SSID
    Guest. Attention à l'inversion de sens : ON pour ce switch = SSID
    toujours actif = schedule DÉSACTIVÉ (enable=False côté API).
    """

    _attr_name = "Guest SSID"
    _attr_icon = "mdi:wifi"
    _attr_has_entity_name = True

    def __init__(self, coordinator, api, config_entry: ConfigEntry) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_guest_ssid"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """Return true if Guest SSID is enabled (schedule désactivé)."""
        schedule_enabled = self.coordinator.data.get("ssid_schedules", {}).get("Guest")
        if schedule_enabled is None:
            return None
        return not schedule_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the Guest SSID on (disable schedule = always active)."""
        _LOGGER.info("Enabling Guest SSID (disabling schedule)")
        try:
            success = await self._api.async_toggle_ssid_schedule("Guest", enable=False, persist=True)
            if success:
                self._push_confirmed_state(schedule_enabled=False)
                _LOGGER.info("Guest SSID enabled successfully")
            else:
                _LOGGER.error("Failed to enable Guest SSID")
        except Exception as err:
            _LOGGER.error("Error enabling Guest SSID: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the Guest SSID off (enable schedule = follow configured hours)."""
        _LOGGER.info("Disabling Guest SSID (enabling schedule)")
        try:
            success = await self._api.async_toggle_ssid_schedule("Guest", enable=True, persist=True)
            if success:
                self._push_confirmed_state(schedule_enabled=True)
                _LOGGER.info("Guest SSID disabled successfully (following schedule)")
            else:
                _LOGGER.error("Failed to disable Guest SSID")
        except Exception as err:
            _LOGGER.error("Error disabling Guest SSID: %s", err)

    def _push_confirmed_state(self, schedule_enabled: bool) -> None:
        self.coordinator.data.setdefault("ssid_schedules", {})["Guest"] = schedule_enabled
        self.coordinator.async_set_updated_data(self.coordinator.data)

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

        lock_key = f"{config_entry.entry_id}_radio"
        if lock_key not in _radio_locks:
            _radio_locks[lock_key] = asyncio.Lock()
        self._lock = _radio_locks[lock_key]

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_radio_24g"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool:
        """Return true if 2.4GHz radio is active."""
        radio = self.coordinator.data.get("radio", {})
        return radio.get("slot1_active", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the 2.4GHz radio on."""
        if self._lock.locked():
            _LOGGER.warning("A radio toggle is already in progress, please wait")
            return

        _LOGGER.info("Activating 2.4GHz radio (this may take up to 3 minutes)")
        async with self._lock:
            try:
                success = await self._api.async_toggle_radio(slot=1, enable=True)
                if success:
                    self._push_confirmed_state(True)
                    _LOGGER.info("2.4GHz radio activated successfully")
                else:
                    _LOGGER.error("Failed to activate 2.4GHz radio after all attempts")
            except Exception as err:
                _LOGGER.error("Error activating 2.4GHz radio: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the 2.4GHz radio off."""
        if self._lock.locked():
            _LOGGER.warning("A radio toggle is already in progress, please wait")
            return

        _LOGGER.info("Deactivating 2.4GHz radio")
        async with self._lock:
            try:
                success = await self._api.async_toggle_radio(slot=1, enable=False)
                if success:
                    self._push_confirmed_state(False)
                    _LOGGER.info("2.4GHz radio deactivated successfully")
                else:
                    _LOGGER.error("Failed to deactivate 2.4GHz radio after all attempts")
            except Exception as err:
                _LOGGER.error("Error deactivating 2.4GHz radio: %s", err)

    def _push_confirmed_state(self, state: bool) -> None:
        self.coordinator.data.setdefault("radio", {})["slot1_active"] = state
        self.coordinator.async_set_updated_data(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        radio = self.coordinator.data.get("radio", {})
        return {
            "band": radio.get("slot1_band", "Unknown"),
            "ssids": ", ".join(radio.get("slot1_ssids", [])),
            "description": "Contrôle la radio WiFi 2.4GHz (slot1)",
            "note": "Désactivation ~5-10s (session unique), Activation ~40-60s (redémarrage matériel)",
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

        lock_key = f"{config_entry.entry_id}_radio"
        if lock_key not in _radio_locks:
            _radio_locks[lock_key] = asyncio.Lock()
        self._lock = _radio_locks[lock_key]

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_radio_5g"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool:
        """Return true if 5GHz radio is active."""
        radio = self.coordinator.data.get("radio", {})
        return radio.get("slot2_active", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the 5GHz radio on."""
        if self._lock.locked():
            _LOGGER.warning("A radio toggle is already in progress, please wait")
            return

        _LOGGER.info("Activating 5GHz radio (this may take up to 3 minutes)")
        async with self._lock:
            try:
                success = await self._api.async_toggle_radio(slot=2, enable=True)
                if success:
                    self._push_confirmed_state(True)
                    _LOGGER.info("5GHz radio activated successfully")
                else:
                    _LOGGER.error("Failed to activate 5GHz radio after all attempts")
            except Exception as err:
                _LOGGER.error("Error activating 5GHz radio: %s", err)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the 5GHz radio off."""
        if self._lock.locked():
            _LOGGER.warning("A radio toggle is already in progress, please wait")
            return

        _LOGGER.info("Deactivating 5GHz radio")
        async with self._lock:
            try:
                success = await self._api.async_toggle_radio(slot=2, enable=False)
                if success:
                    self._push_confirmed_state(False)
                    _LOGGER.info("5GHz radio deactivated successfully")
                else:
                    _LOGGER.error("Failed to deactivate 5GHz radio after all attempts")
            except Exception as err:
                _LOGGER.error("Error deactivating 5GHz radio: %s", err)

    def _push_confirmed_state(self, state: bool) -> None:
        self.coordinator.data.setdefault("radio", {})["slot2_active"] = state
        self.coordinator.async_set_updated_data(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        radio = self.coordinator.data.get("radio", {})
        return {
            "band": radio.get("slot2_band", "Unknown"),
            "ssids": ", ".join(radio.get("slot2_ssids", [])),
            "description": "Contrôle la radio WiFi 5GHz (slot2)",
            "note": "Désactivation ~5-10s (session unique), Activation ~40-60s (redémarrage matériel)",
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zyxel switches."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    by_item = entry_data["coordinator_for_item"]
    radio_coordinator = by_item[DATA_ITEM_RADIO]
    ssid_coordinator = by_item[DATA_ITEM_SSID_SCHEDULES]
    api = entry_data["api"]

    entities = [
        ZyxelGuestSSIDSwitch(ssid_coordinator, api, entry),
        ZyxelRadio24GSwitch(radio_coordinator, api, entry),
        ZyxelRadio5GSwitch(radio_coordinator, api, entry),
    ]

    # Auto-détection des SSIDs (depuis le cache déjà alimenté par le premier
    # refresh de l'item "radio" effectué dans __init__.py avant l'appel à
    # cette fonction, quel que soit le groupe auquel il est affecté)
    try:
        ssid_list = await api.async_get_ssid_list()
        _LOGGER.info("Creating SSID schedule switches for: %s", ssid_list)

        for ssid_name in ssid_list:
            if ssid_name.lower() == "guest":
                continue  # déjà géré par ZyxelGuestSSIDSwitch
            entities.append(ZyxelSSIDScheduleSwitch(ssid_coordinator, api, entry, ssid_name))

    except Exception as err:
        _LOGGER.error("Failed to auto-detect SSIDs, skipping schedule switches: %s", err)

    async_add_entities(entities)
