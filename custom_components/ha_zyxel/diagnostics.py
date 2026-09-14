"""Diagnostics support for the Zyxel integration.

Volontairement dénué de tout secret ou identifiant personnel (mot de passe,
MAC, IP client, nom de SSID) - juste de quoi comprendre l'état de santé de
l'intégration : modèle/firmware, intervalles configurés, répartition des
données, dernière communication, compteur d'échecs/backoff.
"""
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api = entry_data["api"]
    shared_state = entry_data["shared_state"]
    fast = entry_data["coordinator_fast"]
    slow = entry_data["coordinator_slow"]
    daily = entry_data["coordinator_daily"]

    device_info = shared_state.get("device_info", {})
    last_seen = shared_state.get("last_seen")

    return {
        "device": {
            "model": device_info.get("model"),
            "firmware": device_info.get("firmware"),
        },
        "config": {
            "fast_interval_seconds": fast.update_interval.total_seconds() if fast.update_interval else None,
            "slow_interval_seconds": slow.update_interval.total_seconds() if slow.update_interval else None,
            "daily_interval_seconds": daily.update_interval.total_seconds() if daily.update_interval else None,
            "item_groups": entry_data.get("item_groups"),
            "mikrotik_resolver_enabled": entry_data.get("resolver") is not None,
        },
        "status": {
            "last_seen": last_seen.isoformat() if last_seen else None,
            **api.get_diagnostics_snapshot(),
        },
        "coordinators": {
            "fast_last_update_success": fast.last_update_success,
            "slow_last_update_success": slow.last_update_success,
            "daily_last_update_success": daily.last_update_success,
        },
    }
