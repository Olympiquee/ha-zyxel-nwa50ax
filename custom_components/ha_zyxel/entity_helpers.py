"""Utilitaires partagés entre les entités (sensor/switch/button) de l'intégration."""
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN


def get_shared_state(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Retourne le dict d'état partagé (device_info, last_seen) de cette entry.

    Ce dict est mis à jour par les coordinators dans __init__.py et lu par
    toutes les entités, indépendamment du coordinator (fast/slow/daily)
    auquel elles sont elles-mêmes abonnées pour leur propre état.
    """
    return hass.data[DOMAIN][entry_id]["shared_state"]


def build_device_info(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Construit le dict device_info HA à partir de l'état partagé.

    Centralisé ici pour éviter que chaque entité (sensor/switch/button)
    duplique la même logique de lecture du modèle/firmware, comme c'était le
    cas dans la v1 de l'intégration (device_info() était copié-collé dans
    quasiment chaque classe d'entité).
    """
    device_data = get_shared_state(hass, entry_id).get("device_info", {})
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": f"Zyxel {device_data.get('model', 'NWA50AX')}",
        "manufacturer": "Zyxel",
        "model": device_data.get("model", "NWA50AX"),
        "sw_version": device_data.get("firmware", "Unknown"),
    }
