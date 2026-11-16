"""Switch platform for Zyxel integration - Guest SSID, schedules SSID and Radio control."""
import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import (
    CONF_INVERTED_SSIDS,
    CONF_USE_REAL_SSID_NAMES,
    DATA_ITEM_RADIO,
    DATA_ITEM_SSID_SCHEDULES,
    DEFAULT_USE_REAL_SSID_NAMES,
    DOMAIN,
)
from .entity_helpers import build_device_info
from .ssid_names import (
    effective_state,
    guest_switch_label,
    inverted_profiles,
    schedule_switch_label,
    schedule_wanted,
    semantic_note,
)
from .zyxel_ssh_api import ZyxelSSHAPI

_LOGGER = logging.getLogger(__name__)


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
        use_real_names: bool = DEFAULT_USE_REAL_SSID_NAMES,
        inverted: bool = False,
    ) -> None:
        """Initialize the SSID schedule switch.

        `ssid_name` est le nom du PROFIL côté AP (ex. "Home") : c'est lui qui
        sert dans les commandes CLI et dans l'identifiant unique (qui ne change
        donc jamais). Seul le nom AFFICHÉ peut montrer le vrai SSID diffusé.
        """
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._ssid_name = ssid_name
        self._use_real_names = use_real_names
        self._inverted = inverted
        self._names_cache: dict[str, str] = {}
        self._attr_unique_id = f"zyxel_{config_entry.entry_id}_ssid_schedule_{ssid_name.lower()}"
        self._attr_icon = "mdi:calendar-clock"

    def _known_names(self) -> dict[str, str]:
        """Profil -> SSID diffusé, avec mémoire de la dernière valeur connue.

        Si un cycle lent rate la lecture d'un profil, on garde le dernier nom
        connu plutôt que de faire "clignoter" le nom du switch entre vrai SSID
        et nom de profil.
        """
        current = (self.coordinator.data or {}).get("ssid_names")
        if current:
            self._names_cache.update(current)
        return self._names_cache

    @property
    def name(self) -> str:
        return schedule_switch_label(
            self._ssid_name, self._known_names(), self._use_real_names, self._inverted
        )

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """État affiché : planning actif (normal) ou SSID toujours actif (inversé).

        None si l'état du planning n'est pas encore connu.
        """
        schedule = self.coordinator.data.get("ssid_schedules", {}).get(self._ssid_name)
        return effective_state(schedule, self._inverted)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "ssid_name": self._ssid_name,
            "description": f"Contrôle le schedule du SSID {self._ssid_name}",
            "note": semantic_note(self._inverted),
            "reversed": self._inverted,
        }
        broadcast = self._known_names().get(self._ssid_name)
        if broadcast:
            attrs["broadcast_ssid"] = broadcast
        return attrs

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch sur ON : planning actif (normal) ou SSID toujours actif (inversé)."""
        await self._apply(switch_on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch sur OFF : SSID toujours actif (normal) ou planning actif (inversé)."""
        await self._apply(switch_on=False)

    async def _apply(self, switch_on: bool) -> None:
        # Ce qu'il faut réellement faire sur l'AP dépend du sens du switch.
        enable = schedule_wanted(switch_on, self._inverted)
        _LOGGER.info(
            "SSID %s : switch %s (inversé=%s) -> planning %s",
            self._ssid_name, "ON" if switch_on else "OFF", self._inverted,
            "actif" if enable else "désactivé (always-on)",
        )
        success = await self._api.async_toggle_ssid_schedule(self._ssid_name, enable=enable)
        if success:
            self._push_confirmed_state(enable)
        else:
            _LOGGER.error("Failed to change schedule for SSID %s", self._ssid_name)

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

    _attr_icon = "mdi:wifi"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        api,
        config_entry: ConfigEntry,
        use_real_names: bool = DEFAULT_USE_REAL_SSID_NAMES,
        inverted: bool = True,
    ) -> None:
        """Initialize the switch.

        `inverted=True` par défaut : c'est le comportement historique du
        switch Guest (ON = SSID toujours actif).
        """
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._use_real_names = use_real_names
        self._inverted = inverted
        self._names_cache: dict[str, str] = {}

    def _known_names(self) -> dict[str, str]:
        """Profil -> SSID diffusé (dernière valeur connue conservée)."""
        current = (self.coordinator.data or {}).get("ssid_names")
        if current:
            self._names_cache.update(current)
        return self._names_cache

    @property
    def name(self) -> str:
        return guest_switch_label(
            "Guest", self._known_names(), self._use_real_names, self._inverted
        )

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_guest_ssid"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """État affiché : SSID toujours actif (inversé, défaut) ou planning actif.

        None si l'état du planning n'est pas encore connu.
        """
        schedule = self.coordinator.data.get("ssid_schedules", {}).get("Guest")
        return effective_state(schedule, self._inverted)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch sur ON : SSID toujours actif (inversé, défaut) ou planning actif."""
        await self._apply(switch_on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch sur OFF : planning actif (inversé, défaut) ou SSID toujours actif."""
        await self._apply(switch_on=False)

    async def _apply(self, switch_on: bool) -> None:
        enable = schedule_wanted(switch_on, self._inverted)
        _LOGGER.info(
            "Guest SSID : switch %s (inversé=%s) -> planning %s",
            "ON" if switch_on else "OFF", self._inverted,
            "actif" if enable else "désactivé (always-on)",
        )
        try:
            success = await self._api.async_toggle_ssid_schedule("Guest", enable=enable, persist=True)
            if success:
                self._push_confirmed_state(schedule_enabled=enable)
            else:
                _LOGGER.error("Failed to change Guest SSID schedule")
        except Exception as err:
            _LOGGER.error("Error changing Guest SSID schedule: %s", err)

    def _push_confirmed_state(self, schedule_enabled: bool) -> None:
        self.coordinator.data.setdefault("ssid_schedules", {})["Guest"] = schedule_enabled
        self.coordinator.async_set_updated_data(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        attrs = {
            "description": semantic_note(self._inverted),
            "schedule_info": "Le planning suivi est celui défini dans l'interface web de l'AP",
            "reversed": self._inverted,
        }
        broadcast = self._known_names().get("Guest")
        if broadcast:
            attrs["broadcast_ssid"] = broadcast
        return attrs


class ZyxelRadio24GSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to enable/disable 2.4GHz radio."""

    _attr_name = "Radio 2.4GHz"
    _attr_icon = "mdi:radio-tower"
    _attr_has_entity_name = True

    def __init__(self, coordinator, api, config_entry: ConfigEntry, radio_lock: asyncio.Lock) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._lock = radio_lock

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_radio_24g"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """Return true if 2.4GHz radio is active. None si pas encore déterminé."""
        radio = self.coordinator.data.get("radio", {})
        return radio.get("slot1_active")

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

    def __init__(self, coordinator, api, config_entry: ConfigEntry, radio_lock: asyncio.Lock) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._lock = radio_lock

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_radio_5g"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def is_on(self) -> bool | None:
        """Return true if 5GHz radio is active. None si pas encore déterminé."""
        radio = self.coordinator.data.get("radio", {})
        return radio.get("slot2_active")

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
    radio_lock = entry_data["radio_lock"]
    use_real_names = entry.options.get(CONF_USE_REAL_SSID_NAMES, DEFAULT_USE_REAL_SSID_NAMES)
    # Clé absente = jamais configuré -> défaut (seul Guest inversé, comme avant).
    inverted = inverted_profiles(entry.options.get(CONF_INVERTED_SSIDS))

    entities = [
        ZyxelGuestSSIDSwitch(ssid_coordinator, api, entry, use_real_names, "Guest" in inverted),
        ZyxelRadio24GSwitch(radio_coordinator, api, entry, radio_lock),
        ZyxelRadio5GSwitch(radio_coordinator, api, entry, radio_lock),
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
            entities.append(
                ZyxelSSIDScheduleSwitch(
                    ssid_coordinator, api, entry, ssid_name, use_real_names, ssid_name in inverted
                )
            )

    except Exception as err:
        _LOGGER.error("Failed to auto-detect SSIDs, skipping schedule switches: %s", err)

    async_add_entities(entities)
