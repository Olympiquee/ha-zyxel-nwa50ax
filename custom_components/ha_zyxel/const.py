"""Constants for the Zyxel integration."""

DOMAIN = "ha_zyxel"

# Configuration
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PORT = "port"

# Defaults
DEFAULT_HOST = "192.168.1.2"
DEFAULT_USERNAME = "admin"
DEFAULT_PORT = 22
DEFAULT_SCAN_INTERVAL = 60  # Plus lent pour SSH (60 secondes)

# Attributes
ATTR_DEVICE_MODEL = "device_model"
ATTR_FIRMWARE_VERSION = "firmware_version"
ATTR_MAC_ADDRESS = "mac_address"
ATTR_SERIAL_NUMBER = "serial_number"
ATTR_UPTIME = "uptime"
