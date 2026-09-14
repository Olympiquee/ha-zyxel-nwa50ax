"""Config flow for Zyxel integration."""
import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector

from .const import (
    CONF_DAILY_INTERVAL,
    CONF_FAST_INTERVAL,
    CONF_HOST,
    CONF_ITEM_GROUP_PREFIX,
    CONF_MIKROTIK_ENABLED,
    CONF_MIKROTIK_HOST,
    CONF_MIKROTIK_PASSWORD,
    CONF_MIKROTIK_REFRESH_INTERVAL,
    CONF_MIKROTIK_SSH_FINGERPRINT,
    CONF_MIKROTIK_USERNAME,
    CONF_PASSWORD,
    CONF_RESET_MIKROTIK_SSH_FINGERPRINT,
    CONF_RESET_SSH_FINGERPRINT,
    CONF_SLOW_INTERVAL,
    CONF_SSH_FINGERPRINT,
    CONF_UPDATE_INTERVAL,
    CONF_USERNAME,
    DATA_ITEMS,
    DEFAULT_DAILY_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HOST,
    DEFAULT_ITEM_GROUPS,
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
from .ssh_security import SSHHostKeyChangedError
from .zyxel_ssh_api import ZyxelAuthError, ZyxelSSHAPI

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# Libellés affichés dans le menu déroulant "groupe" - "value" doit rester
# fast/slow/daily (c'est ce qui est stocké et relu par __init__.py).
_GROUP_SELECT_OPTIONS = [
    selector.SelectOptionDict(value="fast", label="Rapide"),
    selector.SelectOptionDict(value="slow", label="Lent"),
    selector.SelectOptionDict(value="daily", label="Quotidien"),
]


def _group_selector() -> selector.SelectSelector:
    """Liste déroulante fast/slow/daily réutilisée pour chaque item."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=_GROUP_SELECT_OPTIONS, mode=selector.SelectSelectorMode.DROPDOWN
        )
    )


async def validate_input(hass: HomeAssistant, data: dict) -> dict:
    """Validate that the user input allows us to connect.

    Note : ce test de connexion utilise sa propre instance API jetable, donc
    sa propre empreinte SSH TOFU (jamais persistée) - il ne pollue jamais
    l'empreinte mémorisée par l'entry réelle créée dans __init__.py.
    """
    host = data[CONF_HOST]
    username = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]

    api = ZyxelSSHAPI(host, username, password)

    try:
        connected = await api.async_connect()
        if not connected:
            raise CannotConnect("Cannot connect - check host, username and password")
    except ZyxelAuthError as ex:
        raise InvalidAuth from ex
    except (CannotConnect, SSHHostKeyChangedError):
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
            except InvalidAuth:
                errors["base"] = "invalid_auth"
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

    async def async_step_reconfigure(self, user_input=None):
        """Permet de changer host/username/password sans supprimer/recréer l'intégration."""
        errors = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                return self.async_update_reload_and_abort(
                    reconfigure_entry, data=user_input
                )

        current = reconfigure_entry.data
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST, default=current.get(CONF_HOST, DEFAULT_HOST)): str,
                vol.Required(CONF_USERNAME, default=current.get(CONF_USERNAME, DEFAULT_USERNAME)): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        """Déclenché automatiquement par HA quand l'authentification SSH échoue."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Demande un nouveau mot de passe suite à un échec d'authentification."""
        errors = {}
        reauth_entry = self._get_reauth_entry()

        if user_input is not None:
            new_data = {**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await validate_input(self.hass, new_data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

            if not errors:
                return self.async_update_reload_and_abort(reauth_entry, data=new_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow."""
        return OptionsFlow()


class OptionsFlow(config_entries.OptionsFlow):
    """Options Zyxel : menu à 4 sections.

    Pas de __init__ personnalisé : `self.config_entry` est désormais fourni
    automatiquement par le framework HA (versions récentes de Home Assistant
    Core - le faire manuellement comme avant, via `self.config_entry =
    config_entry` dans __init__, lève une erreur sur ces versions, car
    `config_entry` y est devenu une propriété en lecture seule héritée de la
    classe de base).

    Chaque sous-étape sauvegarde IMMÉDIATEMENT ce qu'elle contient, fusionné
    avec le reste des options existantes (`{**self.config_entry.options,
    **user_input}`) - important car `async_create_entry` sur une OptionsFlow
    REMPLACE tout `entry.options`, il ne fusionne pas tout seul. Sans ce
    merge explicite, sauvegarder une section effacerait les autres.
    """

    def _save(self, user_input: dict) -> config_entries.FlowResult:
        merged = {**self.config_entry.options, **user_input}
        return self.async_create_entry(title="", data=merged)

    async def async_step_init(self, user_input=None):
        """Menu principal des options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["intervals", "data_groups", "mikrotik", "security"],
        )

    async def async_step_intervals(self, user_input=None):
        """Les 3 intervalles de rafraîchissement (fast/slow/daily)."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options

        # Migration best-effort : si l'ancienne clé unique 'update_interval'
        # existe (v1.x) et qu'aucun 'fast_interval' n'a encore été choisi, on
        # la reprend comme valeur de départ du groupe rapide.
        default_fast = options.get(
            CONF_FAST_INTERVAL, options.get(CONF_UPDATE_INTERVAL, DEFAULT_FAST_INTERVAL)
        )
        default_slow = options.get(CONF_SLOW_INTERVAL, DEFAULT_SLOW_INTERVAL)
        default_daily = options.get(CONF_DAILY_INTERVAL, DEFAULT_DAILY_INTERVAL)

        return self.async_show_form(
            step_id="intervals",
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
            }),
        )

    async def async_step_data_groups(self, user_input=None):
        """Répartition de chaque famille de données entre les 3 groupes."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        schema_dict = {}
        for item in DATA_ITEMS:
            key = f"{CONF_ITEM_GROUP_PREFIX}{item}"
            default = options.get(key, DEFAULT_ITEM_GROUPS[item])
            schema_dict[vol.Optional(key, default=default)] = _group_selector()

        return self.async_show_form(
            step_id="data_groups",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_mikrotik(self, user_input=None):
        """Résolveur de noms optionnel via les baux DHCP MikroTik."""
        if user_input is not None:
            return self._save(user_input)

        options = self.config_entry.options
        default_mikrotik_enabled = options.get(CONF_MIKROTIK_ENABLED, False)
        default_mikrotik_host = options.get(CONF_MIKROTIK_HOST, "")
        default_mikrotik_username = options.get(CONF_MIKROTIK_USERNAME, "")
        default_mikrotik_password = options.get(CONF_MIKROTIK_PASSWORD, "")
        default_mikrotik_refresh = options.get(
            CONF_MIKROTIK_REFRESH_INTERVAL, DEFAULT_MIKROTIK_REFRESH_INTERVAL
        )

        return self.async_show_form(
            step_id="mikrotik",
            data_schema=vol.Schema({
                vol.Optional(CONF_MIKROTIK_ENABLED, default=default_mikrotik_enabled): bool,
                vol.Optional(CONF_MIKROTIK_HOST, default=default_mikrotik_host): str,
                vol.Optional(CONF_MIKROTIK_USERNAME, default=default_mikrotik_username): str,
                vol.Optional(CONF_MIKROTIK_PASSWORD, default=default_mikrotik_password): str,
                vol.Optional(CONF_MIKROTIK_REFRESH_INTERVAL, default=default_mikrotik_refresh): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=3600)
                ),
            }),
        )

    async def async_step_security(self, user_input=None):
        """Empreintes SSH mémorisées (TOFU) - état + réinitialisation à la demande.

        Les 2 cases à cocher sont des actions ponctuelles (jamais persistées
        telles quelles) : si cochées à la sauvegarde, l'empreinte
        correspondante est effacée des options, ce qui refait confiance à la
        clé rencontrée à la prochaine connexion et la mémorise à nouveau.
        """
        options = self.config_entry.options

        if user_input is not None:
            merged = {**options}
            if user_input.get(CONF_RESET_SSH_FINGERPRINT):
                merged.pop(CONF_SSH_FINGERPRINT, None)
                _LOGGER.info("Empreinte SSH Zyxel réinitialisée depuis les options")
            if user_input.get(CONF_RESET_MIKROTIK_SSH_FINGERPRINT):
                merged.pop(CONF_MIKROTIK_SSH_FINGERPRINT, None)
                _LOGGER.info("Empreinte SSH MikroTik réinitialisée depuis les options")
            return self.async_create_entry(title="", data=merged)

        zyxel_fp = options.get(CONF_SSH_FINGERPRINT, "Non mémorisée")
        mikrotik_fp = options.get(CONF_MIKROTIK_SSH_FINGERPRINT, "Non mémorisée / désactivée")

        return self.async_show_form(
            step_id="security",
            data_schema=vol.Schema({
                vol.Optional(CONF_RESET_SSH_FINGERPRINT, default=False): bool,
                vol.Optional(CONF_RESET_MIKROTIK_SSH_FINGERPRINT, default=False): bool,
            }),
            description_placeholders={
                "zyxel_fingerprint": zyxel_fp,
                "mikrotik_fingerprint": mikrotik_fp,
            },
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate the credentials were rejected."""
