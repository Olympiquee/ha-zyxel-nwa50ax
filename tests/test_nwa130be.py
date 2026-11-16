"""Tests for the NWA130BE (Wi-Fi 7, tri-band) and for multi-model support.

Parser expectations below are read from the anonymised captures in
tests/fixtures/nwa130be_v730/ (firmware V7.30(ACIL.4)). The entity tests
check that a two-radio NWA50AX keeps exactly the unique_ids and names it had
before multi-radio support, and that a tri-band AP gets the 6 GHz entities.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_zyxel import sensor as sensor_platform
from ha_zyxel import switch as switch_platform
from ha_zyxel.config_flow import default_device_name, resolve_device_name
from ha_zyxel.const import DATA_ITEMS, DOMAIN
from ha_zyxel.entity_helpers import (
    describe_radio_slot,
    device_info_from,
    has_band,
    plan_ssid_switches,
    radio_slot_descriptions,
)
from ha_zyxel.zyxel_ssh_api import ZyxelSSHAPI, find_cli_prompt, ssids_from_radio

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
V730 = "nwa130be_v730"


def load(version: str, name: str) -> str:
    return (FIXTURES / version / name).read_text()


@pytest.fixture
def api():
    return ZyxelSSHAPI("10.0.20.2", "admin", "dummy-password")


# ---------------------------------------------------------------------------
# NWA130BE V7.30 parsers
# ---------------------------------------------------------------------------

def test_v730_version(api):
    info = api._parse_version(load(V730, "show_version.txt"))
    assert info["model"] == "NWA130BE"
    assert info["firmware"] == "V7.30(ACIL.4)"
    assert info["build_date"] == "2026-03-29 03:04:22"


def test_v730_uptime_with_days(api):
    # "system uptime: 10 days 04:44:18"
    assert api._parse_uptime(load(V730, "show_system_uptime.txt")) == 10 * 86400 + 4 * 3600 + 44 * 60 + 18


def test_v730_cpu(api):
    cpu = api._parse_cpu(load(V730, "show_cpu_all.txt"))
    assert cpu["cores"] == [5, 1, 3, 1]
    assert cpu["current"] == (5 + 1 + 3 + 1) // 4
    assert cpu["avg_1min"] == (4 + 2 + 3 + 1) // 4
    assert cpu["avg_5min"] == (4 + 2 + 3 + 1) // 4


def test_v730_memory(api):
    assert api._parse_memory(load(V730, "show_mem_status.txt")) == 41


def test_v730_wlan_three_slots(api):
    radio = api._parse_wlan(load(V730, "show_wlan_all.txt"))
    assert radio["slots"] == [1, 2, 3]
    assert [radio[f"slot{n}_band"] for n in (1, 2, 3)] == ["2.4G", "5G", "6G"]
    assert [radio[f"slot{n}_active"] for n in (1, 2, 3)] == [True, True, True]
    for n in (1, 2, 3):
        # Only SSID_profile_1 is set; SSID_profile_N_band lines (including
        # "2.4G/5G/6G") and WDS_mlo_* lines must not be taken as profiles.
        assert radio[f"slot{n}_ssids"] == ["Wiz_SSID_1"]
    assert [radio[f"slot{n}_profile"] for n in (1, 2, 3)] == ["Wiz_Radio_24G", "Wiz_Radio_5G", "Wiz_Radio_6G"]
    assert [radio[f"slot{n}_output_power"] for n in (1, 2, 3)] == ["20dBm", "30dBm", "30dBm"]
    assert ssids_from_radio(radio) == ["Wiz_SSID_1"]


def test_v730_wlan_slot2_block_ends_at_slot3(api):
    # Former bug: the slot2 block ran to the end of the output, so on a
    # tri-band AP it also collected slot3's fields.
    output = load(V730, "show_wlan_all.txt").replace(
        "SSID_profile_1: Wiz_SSID_1\n SSID_profile_2: \n SSID_profile_3: \n SSID_profile_4: \n"
        " SSID_profile_5: \n SSID_profile_6: \n SSID_profile_7: \n SSID_profile_8: \n SLOT_3",
        "SSID_profile_1: Wiz_SSID_1\n SSID_profile_2: Only6G\n SSID_profile_3: \n SSID_profile_4: \n"
        " SSID_profile_5: \n SSID_profile_6: \n SSID_profile_7: \n SSID_profile_8: \n SLOT_3",
    )
    radio = api._parse_wlan(output)
    assert radio["slot2_ssids"] == ["Wiz_SSID_1"]
    assert radio["slot3_ssids"] == ["Wiz_SSID_1", "Only6G"]
    assert ssids_from_radio(radio) == ["Only6G", "Wiz_SSID_1"]


def test_v730_radio_slot_active_per_slot(api):
    output = load(V730, "show_wlan_all.txt")
    # Deactivate slot3 only: slot1/slot2 must still read as active.
    head, tail = output.split("slot: slot3\n")
    output = head + "slot: slot3\n" + tail.replace("Activate: yes", "Activate: no")
    assert api._parse_radio_slot_active(output, 1) is True
    assert api._parse_radio_slot_active(output, 2) is True
    assert api._parse_radio_slot_active(output, 3) is False
    assert api._parse_radio_slot_active(output, 4) is None


def test_wlan_tolerates_crlf(api):
    output = load(V730, "show_wlan_all.txt").replace("\n", "\r\n")
    radio = api._parse_wlan(output)
    assert radio["slots"] == [1, 2, 3]
    assert radio["slot3_band"] == "6G"
    assert radio["slot3_ssids"] == ["Wiz_SSID_1"]


def test_v730_clients_per_band(api):
    clients = api._parse_clients(load(V730, "show_wireless_hal_station_info.txt"))
    assert len(clients) == 22  # index 0..21
    bands = [c["band"] for c in clients]
    assert bands.count("2.4GHz") == 9
    assert bands.count("5GHz") == 11
    assert bands.count("6GHz") == 2
    assert sum(1 for c in clients if "6" in c["band"]) == 2
    # Band counters used by the sensors: 2.4 + 5 + 6 = all clients.
    count = {t: sum(1 for c in clients if t in c["band"]) for t in ("2.4", "5", "6")}
    assert sum(count.values()) == len(clients)
    assert [c["mlo"] for c in clients].count(True) == 1


def test_v730_client_with_empty_fields(api):
    # index 21 has empty TxRate/RxRate/Capability.
    clients = api._parse_clients(load(V730, "show_wireless_hal_station_info.txt"))
    last = clients[-1]
    assert last["mac"] == "02:00:00:00:00:16"
    assert "tx_rate" not in last
    assert "rx_rate" not in last
    assert "capability" not in last  # must not swallow "DOT11 features: N/A"
    assert last["band"] == "2.4GHz"
    assert last["rssi_dbm"] == -59
    six = clients[2]
    assert six["band"] == "6GHz"
    assert six["capability"] == "802.11be"
    assert six["mlo"] is True
    assert six["tx_rate"] == 4563


def test_v730_port_status_uplink(api):
    port = api._parse_port_status(load(V730, "show_port_status.txt"))
    assert port["port_number"] == 1
    assert port["status"] == "1000M/Full"
    assert port["speed"] == "1000M"
    assert port["tx_rate"] == 8841
    assert port["rx_rate"] == 10703
    assert port["uptime"] == "244:42:42"
    assert port["tx_bytes"] == 15469546293
    assert port["rx_bytes"] == 102269335855


def test_v730_port_table_two_ports(api):
    ports = api._parse_port_table(load(V730, "show_port_status.txt"))
    assert [p["port_number"] for p in ports] == [1, 2]
    assert ports[1]["status"] == "Down"
    assert ports[1]["speed"] is None
    assert ports[1]["tx_bytes"] == 0


def test_port_uplink_is_first_port_with_link(api):
    output = load(V730, "show_port_status.txt")
    lines = output.splitlines()
    # Swap the roles: port 1 down, port 2 up -> port 2 is the uplink.
    lines[2], lines[3] = (
        lines[2].replace("1    1000M/Full", "1    Down      ", 1),
        lines[3].replace("2    Down      ", "2    2500M/Full", 1),
    )
    port = api._parse_port_status("\n".join(lines))
    assert port["port_number"] == 2
    assert port["status"] == "2500M/Full"
    # No link anywhere -> port 1.
    down = output.replace("1000M/Full", "Down      ")
    assert api._parse_port_status(down)["port_number"] == 1


def test_v712_port_status_still_port_1(api):
    port = api._parse_port_status(load("v712", "show_port_status.txt"))
    assert port["port_number"] == 1
    assert len(api._parse_port_table(load("v712", "show_port_status.txt"))) == 1


def test_v730_interfaces(api):
    network = api._parse_interfaces(load(V730, "show_interface_all.txt"))
    assert network["ip_address"] == "10.0.20.23"
    assert network["netmask"] == "255.255.255.0"
    names = {i["name"] for i in network["interfaces"]}
    assert {"lan", "wlan-1-1", "wlan-2-1", "wlan-3-1"} <= names


@pytest.mark.parametrize("name,expected", [
    ("show_wlan_ssid_profile_wiz_ssid_1.txt", False),
    ("show_wlan_ssid_profile_default.txt", False),
])
def test_v730_ssid_schedule_mode(api, name, expected):
    assert api._parse_ssid_schedule_mode(load(V730, name)) is expected


# ---------------------------------------------------------------------------
# CLI prompt handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", ["Router>", "Router#", "Router(config)#", "NWA-Office>"])
def test_clean_output_strips_any_prompt(api, prompt):
    body = load(V730, "show_version.txt").strip()
    raw = f"{prompt} show version\r\n{body.replace(chr(10), chr(13) + chr(10))}\r\n{prompt} "
    assert api._clean_output(raw, "show version").replace("\r", "") == body


@pytest.mark.parametrize("banner,expected", [
    ("Welcome\r\nRouter> ", "Router>"),
    ("Welcome\r\nRouter# ", "Router#"),
    ("Router(config)# ", "Router(config)#"),
    ("no prompt here", None),
])
def test_find_cli_prompt(banner, expected):
    assert find_cli_prompt(banner) == expected


@pytest.mark.parametrize("version", ["v712", V730])
def test_clean_output_keeps_every_fixture_line(api, version):
    # The prompt pattern must never drop a line of real command output.
    for path in sorted((FIXTURES / version).glob("*.txt")):
        text = path.read_text()
        assert api._clean_output(text, "show something") == text.strip(), path.name


# ---------------------------------------------------------------------------
# Guest switch condition
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profiles,guest,schedules", [
    (["Guest", "Home", "IoT", "Studio"], True, ["Home", "IoT", "Studio"]),  # NWA50AX fixture
    (["Wiz_SSID_1"], False, ["Wiz_SSID_1"]),                                 # NWA130BE fixture
    (["Home", "guest"], False, ["Home", "guest"]),  # lower-case profile keeps its schedule switch
    ([], False, []),
])
def test_plan_ssid_switches(profiles, guest, schedules):
    assert plan_ssid_switches(profiles) == (guest, schedules)


# ---------------------------------------------------------------------------
# Radio entity descriptions / unique_id backward compatibility
# ---------------------------------------------------------------------------

def test_radio_descriptions_legacy_literals():
    s1, s2 = describe_radio_slot(1, "2.4G"), describe_radio_slot(2, "5G")
    assert (s1.switch_key, s1.switch_name, s1.sensor_key, s1.sensor_name) == (
        "radio_24g", "Radio 2.4GHz", "slot1_status", "Radio 2.4GHz Status")
    assert (s2.switch_key, s2.switch_name, s2.sensor_key, s2.sensor_name) == (
        "radio_5g", "Radio 5GHz", "slot2_status", "Radio 5GHz Status")
    # Unknown band never changes slot1/slot2.
    assert describe_radio_slot(1, "Unknown") == s1
    assert describe_radio_slot(2, None) == s2


def test_radio_description_6ghz_and_unknown():
    s3 = describe_radio_slot(3, "6G")
    assert (s3.switch_key, s3.switch_name, s3.sensor_key, s3.sensor_name) == (
        "radio_6g", "Radio 6GHz", "slot3_status", "Radio 6GHz Status")
    unknown = describe_radio_slot(3, "Unknown")
    assert (unknown.switch_key, unknown.sensor_key) == ("radio_slot3", "slot3_status")


def test_radio_slots_always_include_1_and_2(api):
    assert [d.slot for d in radio_slot_descriptions({})] == [1, 2]
    garbage = api._parse_wlan("garbage")
    assert [d.slot for d in radio_slot_descriptions(garbage)] == [1, 2]
    v712 = api._parse_wlan(load("v712", "show_wlan_all.txt"))
    assert [d.slot for d in radio_slot_descriptions(v712)] == [1, 2]
    assert not has_band(v712, "6GHz")
    v730 = api._parse_wlan(load(V730, "show_wlan_all.txt"))
    assert [d.slot for d in radio_slot_descriptions(v730)] == [1, 2, 3]
    assert has_band(v730, "6GHz")


def _setup_platforms(api: ZyxelSSHAPI, wlan_output: str, ssid_profiles: list[str]):
    """Run the sensor and switch platform setup against mocked HA objects."""
    entry = SimpleNamespace(entry_id="ENTRY")
    radio_coordinator = MagicMock()
    radio_coordinator.data = {"radio": api._parse_wlan(wlan_output)}
    other = MagicMock()
    other.data = {}
    fake_api = MagicMock()
    fake_api.async_get_ssid_list = AsyncMock(return_value=ssid_profiles)
    hass = SimpleNamespace(data={DOMAIN: {"ENTRY": {
        "coordinator_fast": other,
        "coordinator_slow": other,
        "coordinator_daily": other,
        "coordinator_for_item": {item: (radio_coordinator if item == "radio" else other) for item in DATA_ITEMS},
        "api": fake_api,
        "radio_lock": asyncio.Lock(),
    }}})
    added: list = []

    async def run():
        await sensor_platform.async_setup_entry(hass, entry, added.extend)
        await switch_platform.async_setup_entry(hass, entry, added.extend)

    asyncio.run(run())
    return {e.unique_id: e.name for e in added}


# unique_id -> name of every sensor/switch the NWA50AX had before this change
# (literal copy of the former sensor.py / switch.py values).
LEGACY_NWA50AX_ENTITIES = {
    "ENTRY_last_seen": "Last Seen",
    "ENTRY_uptime": "Uptime",
    "ENTRY_firmware": "Firmware",
    "ENTRY_cpu": "CPU Usage",
    "ENTRY_cpu_1min": "CPU Usage (1 min avg)",
    "ENTRY_cpu_5min": "CPU Usage (5 min avg)",
    "ENTRY_memory": "Memory Usage",
    "ENTRY_clients": "Connected Clients",
    "ENTRY_clients_24ghz": "Clients (2.4GHz)",
    "ENTRY_clients_5ghz": "Clients (5GHz)",
    "ENTRY_port_status": "Port Status",
    "ENTRY_port_tx_rate": "Port TX Rate",
    "ENTRY_port_rx_rate": "Port RX Rate",
    "ENTRY_port_tx_bytes": "Port TX Total",
    "ENTRY_port_rx_bytes": "Port RX Total",
    "ENTRY_slot1_status": "Radio 2.4GHz Status",
    "ENTRY_slot2_status": "Radio 5GHz Status",
    "ENTRY_guest_ssid": "Guest SSID",
    "ENTRY_radio_24g": "Radio 2.4GHz",
    "ENTRY_radio_5g": "Radio 5GHz",
    "zyxel_ENTRY_ssid_schedule_home": "SSID Home Schedule",
    "zyxel_ENTRY_ssid_schedule_iot": "SSID IoT Schedule",
    "zyxel_ENTRY_ssid_schedule_studio": "SSID Studio Schedule",
}


def test_nwa50ax_entities_unchanged(api):
    wlan = load("v712", "show_wlan_all.txt")
    entities = _setup_platforms(api, wlan, ssids_from_radio(api._parse_wlan(wlan)))
    assert entities == LEGACY_NWA50AX_ENTITIES


def test_nwa130be_entities(api):
    wlan = load(V730, "show_wlan_all.txt")
    entities = _setup_platforms(api, wlan, ssids_from_radio(api._parse_wlan(wlan)))
    expected = {k: v for k, v in LEGACY_NWA50AX_ENTITIES.items()
                if k not in ("ENTRY_guest_ssid",) and "ssid_schedule" not in k}
    expected.update({
        "ENTRY_clients_6ghz": "Clients (6GHz)",
        "ENTRY_slot3_status": "Radio 6GHz Status",
        "ENTRY_radio_6g": "Radio 6GHz",
        "zyxel_ENTRY_ssid_schedule_wiz_ssid_1": "SSID Wiz_SSID_1 Schedule",
    })
    assert entities == expected


def test_clients_sensor_attributes_6ghz(api):
    clients = api._parse_clients(load(V730, "show_wireless_hal_station_info.txt"))
    coordinator = MagicMock()
    coordinator.data = {"clients": clients}
    entry = SimpleNamespace(entry_id="ENTRY")
    attrs = sensor_platform.ZyxelClientsSensor(coordinator, entry, include_6ghz=True).extra_state_attributes
    assert (attrs["clients_2_4ghz"], attrs["clients_5ghz"], attrs["clients_6ghz"]) == (9, 11, 2)
    assert len(attrs["client_list"]) == 22
    legacy = sensor_platform.ZyxelClientsSensor(coordinator, entry).extra_state_attributes
    assert list(legacy) == ["clients_2_4ghz", "clients_5ghz", "ssid_counts", "client_list"]


# ---------------------------------------------------------------------------
# Device name / model
# ---------------------------------------------------------------------------

def test_device_info_legacy_entry_keeps_name():
    nwa50ax = {"model": "NWA50AX", "firmware": "V7.12(ABYW.0)"}
    assert device_info_from("E", None, nwa50ax)["name"] == "Zyxel NWA50AX"
    assert device_info_from("E", None, {})["name"] == "Zyxel NWA50AX"
    assert device_info_from("E", None, {})["model"] == "NWA50AX"


def test_device_info_named_entry():
    info = device_info_from("E", "Zyxel Wohnzimmer", {"model": "NWA130BE", "firmware": "V7.30(ACIL.4)"})
    assert info["name"] == "Zyxel Wohnzimmer"
    assert info["model"] == "NWA130BE"
    assert info["sw_version"] == "V7.30(ACIL.4)"


def test_default_device_name():
    assert default_device_name("NWA130BE") == "Zyxel NWA130BE"
    assert default_device_name(None) == "Zyxel AP"
    assert resolve_device_name("  ", "NWA130BE") == "Zyxel NWA130BE"
    assert resolve_device_name(" Zyxel Wohnzimmer ", "NWA130BE") == "Zyxel Wohnzimmer"


# ---------------------------------------------------------------------------
# tools/make_fixtures.py (synthetic input only)
# ---------------------------------------------------------------------------

def _load_make_fixtures():
    spec = importlib.util.spec_from_file_location("make_fixtures", ROOT / "tools" / "make_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_fixtures_keeps_table_columns_and_placeholder():
    mf = _load_make_fixtures()
    capture = (
        "Router> ! ===== show interface all =====\n"
        "Router> show interface all\n"
        "No. Name            Status              IP Address      Mask            IP Assignment\n"
        "2   lan             Up                  192.0.2.123     255.255.255.0   Static        \n"
        "Router> ! ===== show wlan-ssid-profile all =====\n"
        "Router> show wlan-ssid-profile all\n"
        "ssid profile: A\n  SSID: MyPrivateNet\n"
        "ssid profile: B\n  SSID: Unconfigured\n"
        "  IPv4: 192.0.2.123\n"
        "Router> exit\n"
    )
    text = mf.Anonymiser(mf.normalize(capture)).apply()
    blocks = dict(mf.parse_blocks(text))
    row = blocks["show interface all"][1]
    header = blocks["show interface all"][0]
    assert row.index("255.255.255.0") == header.index("Mask")
    assert "10.0.20.2       255.255.255.0" in row
    profiles = blocks["show wlan-ssid-profile all"]
    assert "  SSID: Home" in profiles
    assert "  SSID: Unconfigured" in profiles
    assert "  IPv4: 10.0.20.2" in profiles  # key: value lines are not padded


def test_translations_have_no_markup_characters():
    """The HA frontend parses '<...>' in translations as tags (UNCLOSED_TAG)."""
    import json

    base = Path(__file__).parent.parent / "custom_components" / "ha_zyxel"

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, str):
            yield node

    for path in [base / "strings.json", *sorted((base / "translations").glob("*.json"))]:
        for text in walk(json.loads(path.read_text(encoding="utf-8"))):
            assert "<" not in text and ">" not in text, f"{path.name}: {text!r}"
