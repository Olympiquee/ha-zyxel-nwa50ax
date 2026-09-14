"""Regression tests for zyxel_ssh_api.py parsers, using real CLI captures.

See tests/fixtures/<firmware>/ for the raw captures this file exercises.
Every assertion here was verified against the actual fixture content before
being committed - these numbers are not guesses, they're what the real AP
returned (see fixture files for the exact source).
"""
from pathlib import Path

import pytest

from ha_zyxel.zyxel_ssh_api import ZyxelSSHAPI

FIXTURES = Path(__file__).parent / "fixtures"


def load(version: str, name: str) -> str:
    return (FIXTURES / version / name).read_text()


@pytest.fixture
def api():
    return ZyxelSSHAPI("10.0.20.2", "admin", "dummy-password")


# ---------------------------------------------------------------------------
# Firmware 7.12 - captures réelles (voir tests/fixtures/v712/)
# ---------------------------------------------------------------------------

def test_v712_version(api):
    info = api._parse_version(load("v712", "show_version.txt"))
    assert info["model"] == "NWA50AX"
    assert info["firmware"] == "V7.12(ABYW.0)"
    assert info["build_date"] == "2026-07-05 07:50:35"


def test_v712_uptime_without_days_prefix(api):
    # Moins de 24h d'uptime -> pas de préfixe "X days", juste HH:MM:SS.
    # Exerce spécifiquement le chemin de repli du parser.
    seconds = api._parse_uptime(load("v712", "show_system_uptime.txt"))
    assert seconds == 4 * 3600 + 39 * 60 + 44


def test_v712_cpu_four_cores(api):
    # Ce modèle/firmware a 4 coeurs, pas 2 comme le laissait supposer la doc
    # de référence - confirme que l'agrégation est bien générique.
    cpu = api._parse_cpu(load("v712", "show_cpu_all.txt"))
    assert len(cpu["cores"]) == 4
    assert cpu["current"] == (1 + 2 + 2 + 2) // 4
    assert cpu["avg_1min"] == (3 + 4 + 5 + 2) // 4
    assert cpu["avg_5min"] == (3 + 3 + 4 + 3) // 4


def test_v712_memory_no_space_before_percent(api):
    # "memory usage: 53%" (pas d'espace avant le %) - confirme la tolérance.
    assert api._parse_memory(load("v712", "show_mem_status.txt")) == 53


def test_v712_wlan_four_ssids_including_new_one(api):
    # 4 SSIDs configurés (Home/Guest/IoT/Studio) au lieu des 3 attendus par
    # la doc de référence - confirme l'extraction générique par slot.
    # Couvre aussi un bug latent trouvé en écrivant ce test : les profils
    # SSID vides en fin de liste (SSID_profile_5 à 8) polluaient la capture
    # du profil précédent avant la correction du regex (voir le commentaire
    # dans _parse_wlan).
    radio = api._parse_wlan(load("v712", "show_wlan_all.txt"))
    assert radio["slot1_active"] is True
    assert radio["slot2_active"] is True
    assert set(radio["slot1_ssids"]) == {"Home", "Guest", "IoT", "Studio"}
    assert set(radio["slot2_ssids"]) == {"Home", "Guest", "IoT"}
    assert radio["slot1_band"] == "2.4G"
    assert radio["slot2_band"] == "5G"


def test_v712_clients_band_format_has_hz_suffix(api):
    # Point de fragilité identifié : le firmware 7.12 renvoie "2.4GHz"/"5GHz"
    # (pas "2.4G"/"5G" comme documenté ailleurs dans le CLI). Ce test fige le
    # format réellement observé, pour être alerté si un futur firmware change
    # encore ce champ.
    clients = api._parse_clients(load("v712", "show_wireless_hal_station_info.txt"))
    assert len(clients) == 4
    bands = {c["band"] for c in clients}
    assert bands == {"2.4GHz", "5GHz"}

    first = clients[0]
    assert first["mac"] == "AA:BB:CC:00:00:01"
    assert first["ip"] == "10.0.20.208"
    assert first["ssid"] == "MyHome"


def test_v712_interfaces(api):
    network = api._parse_interfaces(load("v712", "show_interface_all.txt"))
    assert network["ip_address"] == "10.0.20.2"
    assert network["netmask"] == "255.255.255.0"
    names = {i["name"] for i in network["interfaces"]}
    assert "lan" in names
    assert "wlan-1-1" in names


def test_v712_port_status(api):
    port = api._parse_port_status(load("v712", "show_port_status.txt"))
    assert port["status"] == "1000M/Full"
    assert port["speed"] == "1000M"
    assert port["tx_rate"] == 11010
    assert port["rx_rate"] == 6586
    assert port["uptime"] == "04:38:31"
    assert port["tx_bytes"] == 287578429
    assert port["rx_bytes"] == 2055519298


@pytest.mark.parametrize("name,expected", [
    ("show_wlan_ssid_profile_home.txt", True),
    ("show_wlan_ssid_profile_guest.txt", True),
    ("show_wlan_ssid_profile_iot.txt", False),
    ("show_wlan_ssid_profile_studio.txt", False),
])
def test_v712_ssid_schedule_mode(api, name, expected):
    assert api._parse_ssid_schedule_mode(load("v712", name)) is expected


# ---------------------------------------------------------------------------
# Sémantique None (revue de code #3) : un format non reconnu doit renvoyer
# None, jamais 0/False, pour ne jamais se confondre avec une vraie mesure.
# ---------------------------------------------------------------------------

def test_unrecognized_format_returns_none_not_zero(api):
    garbage = "n'importe quoi qui ne matche aucun format connu"

    assert api._parse_memory(garbage) is None
    assert api._parse_uptime(garbage) is None

    cpu = api._parse_cpu(garbage)
    assert cpu["current"] is None
    assert cpu["avg_1min"] is None
    assert cpu["avg_5min"] is None

    radio = api._parse_wlan(garbage)
    assert radio["slot1_active"] is None
    assert radio["slot2_active"] is None

    port = api._parse_port_status(garbage)
    assert port["tx_rate"] is None
    assert port["status"] is None


# ---------------------------------------------------------------------------
# Tolérance des regex (revue de code #4)
# ---------------------------------------------------------------------------

def test_regex_tolerant_to_case_and_spacing(api):
    # Le format réel observé n'a jamais changé entre 7.10 et 7.12, mais les
    # regex sont désormais tolérantes par précaution face à un futur firmware.
    assert api._parse_memory("Memory Usage:53 %") == 53
    assert api._parse_memory("MEMORY   USAGE : 53%") == 53
