"""Sensor platform for Zyxel integration - Optimized for NWA50AX V7.10."""
import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTime,
    UnitOfInformation,
    UnitOfDataRate,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Zyxel sensors from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    
    sensors = [
        # Système
        ZyxelUptimeSensor(coordinator, config_entry),
        ZyxelFirmwareSensor(coordinator, config_entry),
        
        # Performance
        ZyxelCPUSensor(coordinator, config_entry),
        ZyxelCPU1MinSensor(coordinator, config_entry),
        ZyxelCPU5MinSensor(coordinator, config_entry),
        ZyxelMemorySensor(coordinator, config_entry),
        
        # Clients WiFi
        ZyxelClientsSensor(coordinator, config_entry),
        ZyxelClients24GHzSensor(coordinator, config_entry),
        ZyxelClients5GHzSensor(coordinator, config_entry),
        
        # Port Ethernet
        ZyxelPortStatusSensor(coordinator, config_entry),
        ZyxelPortTxRateSensor(coordinator, config_entry),
        ZyxelPortRxRateSensor(coordinator, config_entry),
        ZyxelPortTxBytesSensor(coordinator, config_entry),
        ZyxelPortRxBytesSensor(coordinator, config_entry),
        
        # Radio
        ZyxelSlot1StatusSensor(coordinator, config_entry),
        ZyxelSlot2StatusSensor(coordinator, config_entry),
    ]
    
    async_add_entities(sensors)


class ZyxelBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for Zyxel sensors."""

    def __init__(self, coordinator, config_entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = True

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


# ============================================================================
# CAPTEURS SYSTÈME
# ============================================================================

class ZyxelUptimeSensor(ZyxelBaseSensor):
    """Sensor for device uptime."""

    _attr_name = "Uptime"
    _attr_icon = "mdi:clock-outline"
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_uptime"

    @property
    def native_value(self) -> int | None:
        status = self.coordinator.data.get("status", {})
        return status.get("uptime", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional attributes."""
        uptime = self.native_value or 0
        days = uptime // 86400
        hours = (uptime % 86400) // 3600
        minutes = (uptime % 3600) // 60
        
        return {
            "days": days,
            "hours": hours,
            "minutes": minutes,
            "formatted": f"{days}d {hours}h {minutes}m",
        }


class ZyxelFirmwareSensor(ZyxelBaseSensor):
    """Sensor for firmware version."""

    _attr_name = "Firmware"
    _attr_icon = "mdi:update"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_firmware"

    @property
    def native_value(self) -> str | None:
        device_info = self.coordinator.data.get("device_info", {})
        return device_info.get("firmware", "Unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        device_info = self.coordinator.data.get("device_info", {})
        return {
            "model": device_info.get("model", "Unknown"),
            "build_date": device_info.get("build_date", "Unknown"),
        }


# ============================================================================
# CAPTEURS PERFORMANCE
# ============================================================================

class ZyxelCPUSensor(ZyxelBaseSensor):
    """Sensor for current CPU usage."""

    _attr_name = "CPU Usage"
    _attr_icon = "mdi:chip"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_cpu"

    @property
    def native_value(self) -> int | None:
        status = self.coordinator.data.get("status", {})
        cpu_data = status.get("cpu", {})
        return cpu_data.get("current", 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.coordinator.data.get("status", {})
        cpu_data = status.get("cpu", {})
        cores = cpu_data.get("cores", [])
        
        attrs = {}
        for i, core_usage in enumerate(cores):
            attrs[f"core_{i}"] = core_usage
        
        return attrs


class ZyxelCPU1MinSensor(ZyxelBaseSensor):
    """Sensor for CPU average (1 min)."""

    _attr_name = "CPU Usage (1 min avg)"
    _attr_icon = "mdi:chip"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_cpu_1min"

    @property
    def native_value(self) -> int | None:
        status = self.coordinator.data.get("status", {})
        cpu_data = status.get("cpu", {})
        return cpu_data.get("avg_1min", 0)


class ZyxelCPU5MinSensor(ZyxelBaseSensor):
    """Sensor for CPU average (5 min)."""

    _attr_name = "CPU Usage (5 min avg)"
    _attr_icon = "mdi:chip"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_cpu_5min"

    @property
    def native_value(self) -> int | None:
        status = self.coordinator.data.get("status", {})
        cpu_data = status.get("cpu", {})
        return cpu_data.get("avg_5min", 0)


class ZyxelMemorySensor(ZyxelBaseSensor):
    """Sensor for memory usage."""

    _attr_name = "Memory Usage"
    _attr_icon = "mdi:memory"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_memory"

    @property
    def native_value(self) -> int | None:
        status = self.coordinator.data.get("status", {})
        return status.get("memory", 0)


# ============================================================================
# CAPTEURS CLIENTS WIFI
# ============================================================================

class ZyxelClientsSensor(ZyxelBaseSensor):
    """Sensor for total connected clients."""

    _attr_name = "Connected Clients"
    _attr_icon = "mdi:account-multiple"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_clients"

    @property
    def native_value(self) -> int:
        clients = self.coordinator.data.get("clients", [])
        return len(clients)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return detailed client information."""
        clients = self.coordinator.data.get("clients", [])
        
        # Compter par SSID
        ssid_counts = {}
        for client in clients:
            ssid = client.get("ssid", "Unknown")
            ssid_counts[ssid] = ssid_counts.get(ssid, 0) + 1
        
        # Compter par bande
        band_24ghz = sum(1 for c in clients if "2.4" in c.get("band", ""))
        band_5ghz = sum(1 for c in clients if "5" in c.get("band", ""))
        
        # Liste des clients avec infos essentielles
        client_list = []
        for client in clients:
            client_list.append({
                "mac": client.get("mac", "Unknown"),
                "ip": client.get("ip", "Unknown"),
                "ssid": client.get("ssid", "Unknown"),
                "band": client.get("band", "Unknown"),
                "rssi_dbm": client.get("rssi_dbm", 0),
            })
        
        return {
            "clients_2_4ghz": band_24ghz,
            "clients_5ghz": band_5ghz,
            "ssid_counts": ssid_counts,
            "client_list": client_list,
        }


class ZyxelClients24GHzSensor(ZyxelBaseSensor):
    """Sensor for 2.4GHz clients."""

    _attr_name = "Clients (2.4GHz)"
    _attr_icon = "mdi:wifi"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_clients_24ghz"

    @property
    def native_value(self) -> int:
        clients = self.coordinator.data.get("clients", [])
        return sum(1 for c in clients if "2.4" in c.get("band", ""))


class ZyxelClients5GHzSensor(ZyxelBaseSensor):
    """Sensor for 5GHz clients."""

    _attr_name = "Clients (5GHz)"
    _attr_icon = "mdi:wifi"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_clients_5ghz"

    @property
    def native_value(self) -> int:
        clients = self.coordinator.data.get("clients", [])
        return sum(1 for c in clients if "5" in c.get("band", ""))


# ============================================================================
# CAPTEURS PORT ETHERNET
# ============================================================================

class ZyxelPortStatusSensor(ZyxelBaseSensor):
    """Sensor for Ethernet port status."""

    _attr_name = "Port Status"
    _attr_icon = "mdi:ethernet"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_port_status"

    @property
    def native_value(self) -> str:
        network = self.coordinator.data.get("network", {})
        port = network.get("port", {})
        return port.get("status", "Unknown")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        network = self.coordinator.data.get("network", {})
        port = network.get("port", {})
        return {
            "speed": port.get("speed", "Unknown"),
            "uptime": port.get("uptime", "Unknown"),
        }


class ZyxelPortTxRateSensor(ZyxelBaseSensor):
    """Sensor for port TX rate."""

    _attr_name = "Port TX Rate"
    _attr_icon = "mdi:upload"
    _attr_native_unit_of_measurement = "B/s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_port_tx_rate"

    @property
    def native_value(self) -> int:
        network = self.coordinator.data.get("network", {})
        port = network.get("port", {})
        return port.get("tx_rate", 0)


class ZyxelPortRxRateSensor(ZyxelBaseSensor):
    """Sensor for port RX rate."""

    _attr_name = "Port RX Rate"
    _attr_icon = "mdi:download"
    _attr_native_unit_of_measurement = "B/s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_port_rx_rate"

    @property
    def native_value(self) -> int:
        network = self.coordinator.data.get("network", {})
        port = network.get("port", {})
        return port.get("rx_rate", 0)


class ZyxelPortTxBytesSensor(ZyxelBaseSensor):
    """Sensor for port TX bytes."""

    _attr_name = "Port TX Total"
    _attr_icon = "mdi:upload"
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_port_tx_bytes"

    @property
    def native_value(self) -> int:
        network = self.coordinator.data.get("network", {})
        port = network.get("port", {})
        return port.get("tx_bytes", 0)


class ZyxelPortRxBytesSensor(ZyxelBaseSensor):
    """Sensor for port RX bytes."""

    _attr_name = "Port RX Total"
    _attr_icon = "mdi:download"
    _attr_native_unit_of_measurement = "B"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_port_rx_bytes"

    @property
    def native_value(self) -> int:
        network = self.coordinator.data.get("network", {})
        port = network.get("port", {})
        return port.get("rx_bytes", 0)


# ============================================================================
# CAPTEURS RADIO
# ============================================================================

class ZyxelSlot1StatusSensor(ZyxelBaseSensor):
    """Sensor for Slot 1 (2.4GHz) status."""

    _attr_name = "Radio 2.4GHz Status"
    _attr_icon = "mdi:radio-tower"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_slot1_status"

    @property
    def native_value(self) -> str:
        radio = self.coordinator.data.get("radio", {})
        return "Active" if radio.get("slot1_active") else "Inactive"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        radio = self.coordinator.data.get("radio", {})
        return {
            "band": radio.get("slot1_band", "Unknown"),
            "ssids": ", ".join(radio.get("slot1_ssids", [])),
        }


class ZyxelSlot2StatusSensor(ZyxelBaseSensor):
    """Sensor for Slot 2 (5GHz) status."""

    _attr_name = "Radio 5GHz Status"
    _attr_icon = "mdi:radio-tower"

    @property
    def unique_id(self) -> str:
        return f"{self._config_entry.entry_id}_slot2_status"

    @property
    def native_value(self) -> str:
        radio = self.coordinator.data.get("radio", {})
        return "Active" if radio.get("slot2_active") else "Inactive"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        radio = self.coordinator.data.get("radio", {})
        return {
            "band": radio.get("slot2_band", "Unknown"),
            "ssids": ", ".join(radio.get("slot2_ssids", [])),
        }
