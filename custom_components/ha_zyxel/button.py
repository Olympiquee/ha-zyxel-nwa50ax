"""Button platform for Zyxel integration - Optimized for NWA50AX V7.10."""
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .config_baseline import ConfigBaseline
from .const import DATA_ITEM_RADIO, DOMAIN, WRITE_CONFIRM_WINDOW_SECONDS
from .entity_helpers import build_device_info
from .zyxel_ssh_api import ZyxelConnectionError, ZyxelSSHAPI

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zyxel buttons from a config entry."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]

    buttons = [
        ZyxelRebootButton(entry_data["coordinator_fast"], entry_data["api"], config_entry),
        ZyxelGroupRefreshButton(
            entry_data["coordinator_fast"], entry_data["api"], config_entry,
            group="fast", label="Refresh (rapide)",
        ),
        ZyxelGroupRefreshButton(
            entry_data["coordinator_slow"], entry_data["api"], config_entry,
            group="slow", label="Refresh (lent)",
        ),
        ZyxelGroupRefreshButton(
            entry_data["coordinator_daily"], entry_data["api"], config_entry,
            group="daily", label="Refresh (quotidien)",
        ),
        ZyxelWriteConfigButton(
            entry_data["coordinator_fast"], config_entry,
            entry_data["config_baseline"],
            entry_data["coordinator_for_item"][DATA_ITEM_RADIO],
        ),
    ]

    async_add_entities(buttons)


class ZyxelRebootButton(CoordinatorEntity, ButtonEntity):
    """Button to reboot the Zyxel device."""

    _attr_name = "Reboot"
    _attr_icon = "mdi:restart"
    _attr_has_entity_name = True

    def __init__(self, coordinator: DataUpdateCoordinator, api: ZyxelSSHAPI, config_entry: ConfigEntry) -> None:
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
        return build_device_info(self.hass, self._config_entry.entry_id)

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


class ZyxelGroupRefreshButton(CoordinatorEntity, ButtonEntity):
    """Bouton de rafraîchissement manuel pour un groupe (fast/slow/daily).

    Passe par `async_get_group_data(group, manual=True)` : la demande prend la
    priorité sur les cycles automatiques (y compris ceux du même groupe) et
    n'est jamais bloquée par le backoff appliqué aux cycles automatiques.
    """

    _attr_icon = "mdi:refresh"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api: ZyxelSSHAPI,
        config_entry: ConfigEntry,
        group: str,
        label: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._api = api
        self._config_entry = config_entry
        self._group = group
        self._attr_name = label

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_refresh_{self._group}"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    async def async_press(self) -> None:
        """Handle the button press - rafraîchissement manuel prioritaire."""
        _LOGGER.info("Rafraîchissement manuel du groupe '%s'", self._group)
        try:
            data = await self._api.async_get_group_data(self._group, manual=True)
            # Pousse directement le résultat dans le coordinator : notifie les
            # entités et réarme le minuteur du cycle automatique à partir de
            # maintenant (pas de double lecture rapprochée).
            self.coordinator.async_set_updated_data(data)
            _LOGGER.info("Rafraîchissement manuel du groupe '%s' terminé", self._group)
        except ZyxelConnectionError as err:
            _LOGGER.error("Rafraîchissement manuel '%s' échoué: %s", self._group, err)
        except Exception as err:
            _LOGGER.error("Erreur lors du rafraîchissement manuel '%s': %s", self._group, err)


class ZyxelWriteConfigButton(CoordinatorEntity, ButtonEntity):
    """Enregistre la config en cours dans la mémoire de l'AP (`write`), avec confirmation.

    - 1er appui : une notification liste ce que le `write` rendrait permanent
      (comparaison avec la configuration de référence) ; rien n'est écrit.
    - 2e appui dans les 60 s : le `write` est réellement envoyé.

    Pourquoi cette précaution : `write` fige TOUTE la configuration en cours,
    y compris les changements temporaires (radio coupée, planning désactivé),
    et peut geler l'AP plusieurs dizaines de secondes. Voir config_baseline.py.
    """

    _attr_name = "Save config (write)"
    _attr_icon = "mdi:content-save-alert-outline"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry: ConfigEntry,
        config_baseline: ConfigBaseline,
        radio_coordinator: DataUpdateCoordinator,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._config_baseline = config_baseline
        self._radio_coordinator = radio_coordinator

    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{self._config_entry.entry_id}_write_config"

    @property
    def device_info(self) -> dict[str, Any]:
        return build_device_info(self.hass, self._config_entry.entry_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "confirm_window_seconds": WRITE_CONFIRM_WINDOW_SECONDS,
            "confirm_until": self._config_baseline.confirm_until,
            "reference_captured_at": self._config_baseline.reference_captured_at,
            "last_write": self._config_baseline.last_result,
        }

    async def async_press(self) -> None:
        """1er appui : aperçu + armement. 2e appui (sous 60 s) : écriture."""
        radio = (self._radio_coordinator.data or {}).get("radio", {})
        try:
            outcome = await self._config_baseline.async_press(radio)
            _LOGGER.info("Bouton Write : %s", outcome)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Erreur du bouton Write: %s", err)
        self.async_write_ha_state()
