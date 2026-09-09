"""Constants for the Zyxel integration."""

DOMAIN = "ha_zyxel"

# Configuration - Connexion AP Zyxel
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"

# Configuration - Groupes de rafraîchissement
CONF_FAST_INTERVAL = "fast_interval"
CONF_SLOW_INTERVAL = "slow_interval"
CONF_DAILY_INTERVAL = "daily_interval"

# Ancienne clé (v1.x), conservée uniquement pour migration best-effort
CONF_UPDATE_INTERVAL = "update_interval"

# Configuration - Résolveur de noms via MikroTik (optionnel, découplé du flux Zyxel)
CONF_MIKROTIK_ENABLED = "mikrotik_enabled"
CONF_MIKROTIK_HOST = "mikrotik_host"
CONF_MIKROTIK_USERNAME = "mikrotik_username"
CONF_MIKROTIK_PASSWORD = "mikrotik_password"
CONF_MIKROTIK_REFRESH_INTERVAL = "mikrotik_refresh_interval"

# Defaults - Connexion
DEFAULT_HOST = "192.168.1.2"
DEFAULT_USERNAME = "admin"

# Defaults - Groupes de rafraîchissement
DEFAULT_FAST_INTERVAL = 120        # 2 minutes - radios + clients connectés
DEFAULT_SLOW_INTERVAL = 3600       # 1 heure - CPU/RAM/interfaces/ports/schedules SSID
DEFAULT_DAILY_INTERVAL = 86400     # 1 jour - version/firmware/modèle

# Bornes de configuration (UI)
MIN_FAST_INTERVAL = 30
MAX_FAST_INTERVAL = 900
MIN_SLOW_INTERVAL = 300
MAX_SLOW_INTERVAL = 21600
MIN_DAILY_INTERVAL = 3600
MAX_DAILY_INTERVAL = 604800

# Defaults - MikroTik resolver
DEFAULT_MIKROTIK_PORT = 22
DEFAULT_MIKROTIK_REFRESH_INTERVAL = 300  # 5 minutes
MIKROTIK_HOSTNAME_CACHE_TTL = 900        # 15 minutes - durée de vie d'une entrée en cache

# Priorités de la file d'attente SSH vers l'AP Zyxel.
# Plus la valeur est basse, plus l'opération est traitée en premier.
# Un seul worker dépile cette file : deux opérations ne peuvent jamais
# s'exécuter en même temps, quel que soit le nombre de coordinators.
PRIORITY_WRITE = 0    # Changements de config (radio, schedule SSID, reboot)
PRIORITY_MANUAL = 1   # Rafraîchissement manuel demandé via un bouton
PRIORITY_FAST = 2     # Cycle automatique du groupe rapide
PRIORITY_SLOW = 3     # Cycle automatique du groupe lent
PRIORITY_DAILY = 4    # Cycle automatique du groupe quotidien
PRIORITY_ADHOC = 9    # Commande ponctuelle via async_execute_command()

# Backoff en cas d'échecs de connexion consécutifs (secondes)
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 300

# Détection de présence WiFi (device_tracker) - délai de grâce anti-flapping
# avant de considérer un appareil comme "absent" après sa disparition du
# dernier cycle rapide. Un multiple de l'intervalle rapide, avec un plancher,
# pour absorber les micro-déconnexions WiFi des appareils en veille.
PRESENCE_GRACE_MULTIPLIER = 3
PRESENCE_GRACE_MIN_SECONDS = 300

# ----------------------------------------------------------------------------
# Répartition des données entre groupes de rafraîchissement (configurable)
# ----------------------------------------------------------------------------
# Chaque "item" ci-dessous correspond à une famille de données récupérées en
# une ou plusieurs commandes SSH. L'utilisateur peut réaffecter n'importe quel
# item à n'importe quel groupe depuis les options de l'intégration (menu
# "Répartition des données") - la répartition par défaut ci-dessous reproduit
# exactement le tri qu'on a défini ensemble.
DATA_ITEM_RADIO = "radio"
DATA_ITEM_CLIENTS = "clients"
DATA_ITEM_CPU = "cpu"
DATA_ITEM_MEMORY = "memory"
DATA_ITEM_UPTIME = "uptime"
DATA_ITEM_INTERFACES = "interfaces"
DATA_ITEM_PORT = "port"
DATA_ITEM_SSID_SCHEDULES = "ssid_schedules"
DATA_ITEM_DEVICE_INFO = "device_info"

DATA_ITEMS = [
    DATA_ITEM_RADIO,
    DATA_ITEM_CLIENTS,
    DATA_ITEM_CPU,
    DATA_ITEM_MEMORY,
    DATA_ITEM_UPTIME,
    DATA_ITEM_INTERFACES,
    DATA_ITEM_PORT,
    DATA_ITEM_SSID_SCHEDULES,
    DATA_ITEM_DEVICE_INFO,
]

REFRESH_GROUPS = ["fast", "slow", "daily"]

# Répartition par défaut = le tri qu'on a défini ensemble.
DEFAULT_ITEM_GROUPS: dict[str, str] = {
    DATA_ITEM_RADIO: "fast",
    DATA_ITEM_CLIENTS: "fast",
    DATA_ITEM_CPU: "slow",
    DATA_ITEM_MEMORY: "slow",
    DATA_ITEM_UPTIME: "slow",
    DATA_ITEM_INTERFACES: "slow",
    DATA_ITEM_PORT: "slow",
    DATA_ITEM_SSID_SCHEDULES: "slow",
    DATA_ITEM_DEVICE_INFO: "daily",
}

# Préfixe des clés d'options HA, une par item : "item_group_radio", etc.
CONF_ITEM_GROUP_PREFIX = "item_group_"

# Attributes (device_info)
ATTR_DEVICE_MODEL = "device_model"
ATTR_FIRMWARE_VERSION = "firmware_version"
ATTR_MAC_ADDRESS = "mac_address"
ATTR_SERIAL_NUMBER = "serial_number"
ATTR_UPTIME = "uptime"
