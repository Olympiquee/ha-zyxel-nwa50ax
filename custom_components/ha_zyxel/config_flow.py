"""Config flow for Zyxel integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DEFAULT_HOST, DEFAULT_USERNAME, DEFAULT_UPDATE_INTERVAL, CONF_UPDATE_INTERVAL, DOMAIN
from .zyxel_api import ZyxelAPI

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

    # Sanitize host
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"https://{host}"

    api = ZyxelAPI(host, username, password)

    try:
        login_success = await api.async_login()
        if not login_success:
            raise CannotConnect("Login failed - check credentials")
        
        # Get device info to confirm connection
        device_data = await api.async_get_data()
        
        await api.async_logout()
        
    except Exception as ex:
        _LOGGER.error("Unable to connect to Zyxel device: %s", ex)
        raise CannotConnect from ex

    return {"title": f"Zyxel {device_data.get('device_info', {}).get('model', 'Device')} ({host})"}


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zyxel devices."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            
            # Sanitize entry
            if not host.startswith("http://") and not host.startswith("https://"):
                host = f"https://{host}"
                user_input[CONF_HOST] = host

            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
                
                # Try HTTP if HTTPS failed
                if "https://" in host:
                    _LOGGER.info("HTTPS failed, trying HTTP...")
                    user_input[CONF_HOST] = host.replace("https://", "http://")
                    try:
                        info = await validate_input(self.hass, user_input)
                        errors = {}
                    except CannotConnect:
                        errors["base"] = "cannot_connect"
                    except Exception:  # pylint: disable=broad-except
                        _LOGGER.exception("Unexpected exception")
                        errors["base"] = "unknown"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                # Check if already configured
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
    """Handle options for Zyxel."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL,
            DEFAULT_UPDATE_INTERVAL
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    CONF_UPDATE_INTERVAL,
                    default=current_interval
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=10, max=3600)  # 10s à 60 minutes
                ),
            }),
            description_placeholders={
                "current": str(current_interval),
                "min": "10 secondes",
                "max": "60 minutes (3600s)",
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""
