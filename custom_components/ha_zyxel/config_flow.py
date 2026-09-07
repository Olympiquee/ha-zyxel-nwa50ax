"""Config flow for Zyxel integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import (
    CONF_DAILY_INTERVAL,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_MIKROTIK_ENABLED,
    CONF_MIKROTIK_HOST,
    CONF_MIKROTIK_PASSWORD,
    CONF_MIKROTIK_REFRESH_INTERVAL,
    CONF_MIKROTIK_USERNAME,
    CONF_PASSWORD,
    CONF_SLOW_INTERVAL,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DEFAULT_DAILY_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_MIKROTIK_REFRESH_INTERVAL,
    DEFAULT_SLOW_INTERVAL,
    DEFAULT_USERNAME,
    DOMAIN,
    MAX_DAILY_INTERVAL,
    MAX_FAST_INTERVAL,
    MAX_SLOW_INTERVAL,
    MIN_DAILY_INTERVAL,
    MIN_FAST_INTERVAL,
    MIN_SLOW_INTERVAL,
)
from .zyxel_ssh_api import ZyxelSSHAPI

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def validate_input(hass: HomeAssistant, data: dict) -> dict:
    """Validate that the user input allows us to connect."""
    host = data[CONF_HOST]
    username = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]

    api = ZyxelSSHAPI(host, username, password)

    try:
        connected = await api.async_connect()
        if not connected:
            raise CannotConnect("Cannot connect - check host, username and password")
    except CannotConnect:
        raise
    except Exception as ex:
        _LOGGER.error("Unable to connect to Zyxel device: %s", ex)
        raise CannotConnect from ex

    return {"title": f"Zyxel NWA50AX ({host})"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zyxel devices."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return OptionsFlow(config_entry)


class OptionsFlow(config_entries.OptionsFlow):
    """Handle options for Zyxel : 3 groupes de rafraîchissement + résolveur MikroTik."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options

        # Migration best-effort : si l'ancienne clé unique 'update_interval'
        # existe (v1.x) et qu'aucun 'fast_interval' n'a encore été choisi, on
        # la reprend comme valeur de départ du groupe rapide.
        default_fast = options.get(
            CONF_FAST_INTERVAL, options.get(CONF_UPDATE_INTERVAL, DEFAULT_FAST_INTERVAL)
        )
        default_slow = options.get(CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL)
        default_daily = options.get(CONF_DAILY_INTERVAL, DEFAULT_DAILY_INTERVAL)

        default_mikrotik_enabled = options.get(CONF_MIKROTIK_ENABLED, False)
        default_mikrotik_host = options.get(CONF_MIKROTIK_HOST, "")
        default_mikrotik_username = options.get(CONF_MIKROTIK_USERNAME, "")
        default_mikrotik_password = options.get(CONF_MIKROTIK_PASSWORD, "")
        default_mikrotik_refresh = options.get(
            CONF_MIKROTIK_REFRESH_INTERVAL, DEFAULT_MIKROTIK_REFRESH_INTERVAL
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(CONF_FAST_INTERVAL, default=default_fast): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_FAST_INTERVAL, max=MAX_FAST_INTERVAL)
                ),
                vol.Optional(CONF_SLOW_INTERVAL, default=default_slow): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_SLOW_INTERVAL, max=MAX_SLOW_INTERVAL)
                ),
                vol.Optional(CONF_DAILY_INTERVAL, default=default_daily): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_DAILY_INTERVAL, max=MAX_DAILY_INTERVAL)
                ),
                vol.Optional(CONF_MIKROTIK_ENABLED, default=default_mikrotik_enabled): bool,
                vol.Optional(CONF_MIKROTIK_HOST, default=default_mikrotik_host): str,
                vol.Optional(CONF_MIKROTIK_USERNAME, default=default_mikrotik_username): str,
                vol.Optional(CONF_MIKROTIK_PASSWORD, default=default_mikrotik_password): str,
                vol.Optional(CONF_MIKROTIK_REFRESH_INTERVAL, default=default_mikrotik_refresh): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=3600)
                ),
            }),
            description_placeholders={
                "fast_hint": "Radios + clients connectés",
                "slow_hint": "CPU/RAM/interfaces/port + schedules SSID",
                "daily_hint": "Modèle/firmware",
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
