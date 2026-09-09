"""The Zyxel NWA50AX integration."""
import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAILY_INTERVAL,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_ITEM_GROUP_PREFIX,
    CONF_MIKROTIK_ENABLED,
    CONF_MIKROTIK_HOST,
    CONF_MIKROTIK_PASSWORD,
    CONF_MIKROTIK_REFRESH_INTERVAL,
    CONF_MIKROTIK_USERNAME,
    CONF_PASSWORD,
    CONF_SLOW_INTERVAL,
    CONF_USERNAME,
    DATA_ITEM_CLIENTS,
    DATA_ITEMS,
    DEFAULT_DAILY_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_ITEM_GROUPS,
    DEFAULT_MIKROTIK_REFRESH_INTERVAL,
    DEFAULT_SLOW_INTERVAL,
    DOMAIN,
    PRESENCE_GRACE_MIN_SECONDS,
    PRESENCE_GRACE_MULTIPLIER,
)
from .mikrotik_resolver import MikrotikHostnameResolver
from .presence import PresenceTracker
from .zyxel_ssh_api import ZyxelConnectionError, ZyxelSSHAPI

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.BUTTON, Platform.DEVICE_TRACKER]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Zyxel from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    fast_interval = entry.options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)
    slow_interval = entry.options.get(CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL)
    daily_interval = entry.options.get(CONF_DAILY_INTERVAL, DEFAULT_DAILY_INTERVAL)
    group_intervals = {"fast": fast_interval, "slow": slow_interval, "daily": daily_interval}

    # Répartition des données entre groupes - configurable depuis les options
    # ("Répartition des données"). Chaque item absent des options garde
    # l'affectation par défaut (le tri qu'on a défini ensemble).
    item_groups = {
        item: entry.options.get(f"{CONF_ITEM_GROUP_PREFIX}{item}", DEFAULT_ITEM_GROUPS[item])
        for item in DATA_ITEMS
    }

    api = ZyxelSSHAPI(host, username, password)
    api.set_item_groups(item_groups)

    if not await api.async_connect():
        raise ConfigEntryNotReady(f"Cannot connect to {host}")

    # État partagé entre TOUTES les entités, indépendamment de quel coordinator
    # (fast/slow/daily) les alimente par ailleurs - voir entity_helpers.py.
    shared_state = {"device_info": {}, "last_seen": None}

    # Présence WiFi (device_tracker) - cache mémoire séparé, avec délai de
    # grâce anti-flapping (voir presence.py). Le délai se base sur l'intervalle
    # du groupe qui gère RÉELLEMENT "clients" (pas forcément "fast" si
    # l'utilisateur l'a réaffecté ailleurs).
    clients_group = item_groups.get(DATA_ITEM_CLIENTS, DEFAULT_ITEM_GROUPS[DATA_ITEM_CLIENTS])
    presence_tracker = PresenceTracker(
        grace_period=max(
            PRESENCE_GRACE_MULTIPLIER * group_intervals[clients_group], PRESENCE_GRACE_MIN_SECONDS
        )
    )

    def _touch_last_seen() -> None:
        shared_state["last_seen"] = dt_util.now()

    def _make_update_method(group: str):
        """Fabrique la coroutine update_method attendue par DataUpdateCoordinator."""

        async def _update() -> dict:
            try:
                data = await api.async_get_group_data(group)
            except ZyxelConnectionError as err:
                raise UpdateFailed(str(err)) from err
            except Exception as err:
                raise UpdateFailed(f"Erreur communication AP ({group}): {err}") from err

            _touch_last_seen()
            # Piloté par la PRÉSENCE des données, pas par le nom du groupe :
            # "clients"/"device_info" peuvent avoir été réaffectés par
            # l'utilisateur à n'importe quel groupe.
            if "clients" in data:
                presence_tracker.update(data["clients"])
            if data.get("device_info"):
                shared_state["device_info"] = data["device_info"]
            return data

        return _update

    coordinator_fast = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_fast",
        update_method=_make_update_method("fast"),
        update_interval=timedelta(seconds=fast_interval),
    )
    coordinator_slow = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_slow",
        update_method=_make_update_method("slow"),
        update_interval=timedelta(seconds=slow_interval),
    )
    coordinator_daily = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_daily",
        update_method=_make_update_method("daily"),
        update_interval=timedelta(seconds=daily_interval),
    )
    coordinators_by_group = {"fast": coordinator_fast, "slow": coordinator_slow, "daily": coordinator_daily}

    # Coordinator à utiliser par les entités pour CHAQUE item - c'est ce qui
    # rend la réaffectation utilisateur transparente pour sensor.py/switch.py/
    # device_tracker.py : ils ne référencent plus jamais "coordinator_slow" en
    # dur, seulement "le coordinator qui gère actuellement l'item X".
    coordinator_for_item = {item: coordinators_by_group[item_groups[item]] for item in DATA_ITEMS}

    # Premier refresh séquentiel, avec un peu d'attente entre chacun.
    # Le groupe "daily" passe en premier car il porte historiquement
    # device_info (nom de l'appareil dans le device registry HA) et sa
    # commande unique est rapide - même si l'utilisateur l'a réaffecté, ça
    # reste un ordre de démarrage raisonnable.
    await coordinator_daily.async_config_entry_first_refresh()
    await asyncio.sleep(2)
    await coordinator_fast.async_config_entry_first_refresh()
    await asyncio.sleep(2)
    await coordinator_slow.async_config_entry_first_refresh()

    # Résolveur de noms d'appareils (MikroTik), optionnel et totalement
    # découplé : sa propre connexion SSH, son propre cycle, son propre cache.
    # Une panne ici n'affecte jamais la récupération des données de l'AP Zyxel.
    resolver = None
    if entry.options.get(CONF_MIKROTIK_ENABLED, False):
        mikrotik_host = entry.options.get(CONF_MIKROTIK_HOST)
        mikrotik_username = entry.options.get(CONF_MIKROTIK_USERNAME)
        mikrotik_password = entry.options.get(CONF_MIKROTIK_PASSWORD)
        if mikrotik_host and mikrotik_username and mikrotik_password:
            resolver = MikrotikHostnameResolver(
                hass,
                host=mikrotik_host,
                username=mikrotik_username,
                password=mikrotik_password,
                refresh_interval=entry.options.get(
                    CONF_MIKROTIK_REFRESH_INTERVAL, DEFAULT_MIKROTIK_REFRESH_INTERVAL
                ),
            )
            await resolver.async_start()
            api.set_hostname_resolver(resolver.get_hostname)
        else:
            _LOGGER.warning(
                "Résolveur MikroTik activé mais configuration incomplète (host/user/password) - ignoré"
            )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator_fast": coordinator_fast,
        "coordinator_slow": coordinator_slow,
        "coordinator_daily": coordinator_daily,
        "coordinator_for_item": coordinator_for_item,
        "item_groups": item_groups,
        "shared_state": shared_state,
        "presence_tracker": presence_tracker,
        "resolver": resolver,
        "unsub_listeners": [],
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Recharger automatiquement quand les options changent
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        api: ZyxelSSHAPI = data["api"]
        await api.async_disconnect()

        for unsub in data.get("unsub_listeners", []):
            unsub()

        resolver = data.get("resolver")
        if resolver:
            await resolver.async_shutdown()

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
