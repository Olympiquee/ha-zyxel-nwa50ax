"""Device tracker platform for Zyxel integration.

Une entité par appareil WiFi connu (identifié par sa MAC), créée dynamiquement
au fil des découvertes (pas seulement au démarrage). La présence vient de
`PresenceTracker` (délai de grâce anti-flapping, voir presence.py) - PAS
directement de la présence brute dans le dernier cycle. Le nom affiché vient
du hostname résolu (MikroTik si configuré), avec la MAC en repli si le nom
n'est pas encore connu.

Ces entités ne sont volontairement PAS rattachées au device_info de l'AP
Zyxel dans le registre HA : elles représentent l'appareil suivi (le
téléphone, l'ordinateur...), pas l'AP lui-même - c'est la même convention que
les intégrations de routeur intégrées à Home Assistant (ex: ASUSWRT, Netgear).
"""
import logging

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import ScannerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DATA_ITEM_CLIENTS, DOMAIN
from .presence import PresenceTracker

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zyxel device trackers, découverts dynamiquement au fil des cycles."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator_for_item"][DATA_ITEM_CLIENTS]
    presence: PresenceTracker = entry_data["presence_tracker"]

    tracked_macs: set[str] = set()

    @callback
    def _async_discover_new_devices() -> None:
        """Ajoute une entité pour chaque nouvelle MAC jamais vue jusqu'ici."""
        new_macs = [mac for mac in presence.known_macs() if mac not in tracked_macs]
        if not new_macs:
            return
        tracked_macs.update(new_macs)
        _LOGGER.info("Zyxel: %d nouvel(aux) appareil(s) WiFi détecté(s)", len(new_macs))
        async_add_entities(
            [ZyxelScannerEntity(coordinator, entry, presence, mac) for mac in new_macs]
        )

    entry_data["unsub_listeners"].append(
        coordinator.async_add_listener(_async_discover_new_devices)
    )

    # Découverte immédiate des appareils déjà connus (le premier cycle rapide
    # a déjà tourné dans __init__.py avant que cette fonction ne soit appelée).
    _async_discover_new_devices()


class ZyxelScannerEntity(CoordinatorEntity, ScannerEntity):
    """Un appareil WiFi suivi, identifié par sa MAC."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        config_entry: ConfigEntry,
        presence: PresenceTracker,
        mac: str,
    ) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._presence = presence
        self._mac = mac

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_tracker_{self._mac.replace(':', '')}"

    @property
    def name(self) -> str:
        """Nom résolu (MikroTik), désambiguïsé en cas de collision - voir
        `PresenceTracker.get_display_name`. Repli sur la MAC si non résolu.

        Se met à jour tout seul si le nom se résout après coup (ex: le
        résolveur MikroTik alimente son cache après la création de l'entité).
        """
        return self._presence.get_display_name(self._mac)

    @property
    def available(self) -> bool:
        """Toujours disponible : le délai de grâce gère déjà la dégradation.

        Volontairement PAS lié à `coordinator.last_update_success` - sinon un
        AP momentanément injoignable ferait clignoter l'entité en
        "indisponible" au lieu de simplement passer en "absent" une fois le
        délai de grâce dépassé, ce qui casserait l'anti-flapping recherché.
        """
        return True

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        return self._presence.is_connected(self._mac)

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def ip_address(self) -> str | None:
        return self._presence.get_info(self._mac).get("ip")

    @property
    def hostname(self) -> str | None:
        return self._presence.get_info(self._mac).get("hostname")

    @property
    def extra_state_attributes(self) -> dict:
        info = self._presence.get_info(self._mac)
        return {
            "ssid": info.get("ssid"),
            "band": info.get("band"),
            "rssi_dbm": info.get("rssi_dbm"),
        }
