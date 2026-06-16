"""Constants for the Zyxel integration."""

DOMAIN = "ha_zyxel"

# Configuration
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_UPDATE_INTERVAL = "update_interval"

# Defaults
DEFAULT_HOST = "192.168.1.2"
DEFAULT_USERNAME = "admin"
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_UPDATE_INTERVAL = 60  # Scan interval par défaut en secondes

# Attributes
ATTR_DEVICE_MODEL = "device_model"
ATTR_FIRMWARE_VERSION = "firmware_version"
ATTR_MAC_ADDRESS = "mac_address"
ATTR_SERIAL_NUMBER = "serial_number"
ATTR_UPTIME = "uptime"
