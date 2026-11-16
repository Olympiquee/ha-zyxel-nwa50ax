"""Utilitaires partagés entre les entités (sensor/switch/button) de l'intégration."""
from dataclasses import dataclass
from typing import Any, Optional

from homeassistant.core import HomeAssistant

from .const import DOMAIN

# Fallback model of config entries created before the model was detected
# (all of them were NWA50AX). Keeps their device name "Zyxel NWA50AX".
LEGACY_MODEL = "NWA50AX"

# SSID profile name controlled by the dedicated "Guest SSID" switch. The
# switch sends this exact name to the AP, so it is matched case-sensitively.
GUEST_SSID_PROFILE = "Guest"


def get_shared_state(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Retourne le dict d'état partagé (device_info, last_seen) de cette entry.

    Ce dict est mis à jour par les coordinators dans __init__.py et lu par
    toutes les entités, indépendamment du coordinator (fast/slow/daily)
    auquel elles sont elles-mêmes abonnées pour leur propre état.
    """
    return hass.data[DOMAIN][entry_id]["shared_state"]


def device_info_from(
    entry_id: str, configured_name: Optional[str], device_data: dict[str, Any]
) -> dict[str, Any]:
    """Build the HA device_info dict (pure function, see build_device_info).

    - Entries with a configured name (created by the current config flow)
      use that name, and the model/firmware read from 'show version'.
    - Entries without one (created before multi-model support) keep the
      historical behaviour exactly: "Zyxel <model>", NWA50AX as fallback.
    """
    if configured_name:
        return {
            "identifiers": {(DOMAIN, entry_id)},
            "name": configured_name,
            "manufacturer": "Zyxel",
            "model": device_data.get("model"),
            "sw_version": device_data.get("firmware", "Unknown"),
        }
    return {
        "identifiers": {(DOMAIN, entry_id)},
        "name": f"Zyxel {device_data.get('model', LEGACY_MODEL)}",
        "manufacturer": "Zyxel",
        "model": device_data.get("model", LEGACY_MODEL),
        "sw_version": device_data.get("firmware", "Unknown"),
    }


def build_device_info(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Construit le dict device_info HA à partir de l'état partagé.

    Centralisé ici pour éviter que chaque entité (sensor/switch/button)
    duplique la même logique de lecture du modèle/firmware, comme c'était le
    cas dans la v1 de l'intégration (device_info() était copié-collé dans
    quasiment chaque classe d'entité).
    """
    shared = get_shared_state(hass, entry_id)
    return device_info_from(entry_id, shared.get("device_name"), shared.get("device_info", {}))


# ----------------------------------------------------------------------------
# Radio modules
# ----------------------------------------------------------------------------

@dataclass(frozen=True)
class RadioSlotDescription:
    """Unique-id suffixes and names of the entities of one radio module."""

    slot: int
    label: str                 # "2.4GHz", "5GHz", "6GHz", or "slot 3" if unknown
    switch_key: str            # unique_id suffix of the on/off switch
    switch_name: str
    sensor_key: str            # unique_id suffix of the status sensor
    sensor_name: str


# Historical entities of the two radios of the NWA50AX - must never change.
_LEGACY_SLOTS = {
    1: RadioSlotDescription(1, "2.4GHz", "radio_24g", "Radio 2.4GHz", "slot1_status", "Radio 2.4GHz Status"),
    2: RadioSlotDescription(2, "5GHz", "radio_5g", "Radio 5GHz", "slot2_status", "Radio 5GHz Status"),
}


def band_label(band: Optional[str]) -> Optional[str]:
    """'2.4G' -> '2.4GHz', '6G' -> '6GHz'; None for an unknown band."""
    if not band or band == "Unknown":
        return None
    value = band.strip().upper().removesuffix("HZ").removesuffix("G")
    return f"{value}GHz" if value else None


def describe_radio_slot(slot: int, band: Optional[str]) -> RadioSlotDescription:
    """Entity ids/names for a radio slot.

    Slots 1 and 2 always keep their historical unique_ids and names. Further
    slots follow the same scheme, keyed by band (slot 3 = 6 GHz on the
    NWA130BE -> "radio_6g" / "Radio 6GHz"), or by slot number if the band is
    unknown.
    """
    if slot in _LEGACY_SLOTS:
        return _LEGACY_SLOTS[slot]
    label = band_label(band)
    if label:
        key = label.removesuffix("GHz").replace(".", "").lower()
        return RadioSlotDescription(
            slot, label, f"radio_{key}g", f"Radio {label}", f"slot{slot}_status", f"Radio {label} Status"
        )
    return RadioSlotDescription(
        slot, f"slot {slot}", f"radio_slot{slot}", f"Radio slot {slot}",
        f"slot{slot}_status", f"Radio slot {slot} Status",
    )


def radio_slot_descriptions(radio: dict[str, Any]) -> list[RadioSlotDescription]:
    """Entities to create for the radios reported by 'show wlan all'.

    Slots 1 and 2 are always included (existing installations must never
    lose these entities, even if a parse failed at startup); further slots
    only when the AP reports them.
    """
    slots = sorted({1, 2} | set(radio.get("slots") or []))
    return [describe_radio_slot(slot, radio.get(f"slot{slot}_band")) for slot in slots]


def has_band(radio: dict[str, Any], label: str) -> bool:
    """True if one of the AP's radio slots works on band `label` (e.g. "6GHz")."""
    return any(
        band_label(radio.get(f"slot{slot}_band")) == label for slot in radio.get("slots") or []
    )


def plan_ssid_switches(ssid_profiles: list[str]) -> tuple[bool, list[str]]:
    """Decide which SSID switches to create.

    Returns (create_guest_switch, profiles_with_a_schedule_switch). The Guest
    switch sends `wlan-ssid-profile Guest` + `write`, which would permanently
    create a "Guest" profile if none existed, so it is only created when a
    profile named exactly "Guest" exists. Every other profile - including a
    differently-cased "guest" - gets a normal schedule switch, so no profile
    is ever left without a switch.
    """
    has_guest = GUEST_SSID_PROFILE in ssid_profiles
    others = [name for name in ssid_profiles if name != GUEST_SSID_PROFILE]
    return has_guest, others
